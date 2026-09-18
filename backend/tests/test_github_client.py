import base64

import httpx
import pytest
from app.clients.github import GitHubClient, GitHubClientError, GitHubRateLimitError


def test_parses_github_trending_html() -> None:
    html = """
    <article class="Box-row"><h2><a href=" /owner/repo ">repo</a></h2></article>
    <article class="Box-row"><h2><a href="/second/project">project</a></h2></article>
    """
    client = GitHubClient(httpx.MockTransport(lambda _: httpx.Response(200, text=html)))

    try:
        assert client.trending("weekly") == ["owner/repo", "second/project"]
    finally:
        client.close()


def test_search_follows_pages_and_deduplicates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        items = (
            [{"full_name": "owner/one"}, {"full_name": "owner/two"}]
            if page == 1
            else [{"full_name": "owner/two"}, {"full_name": "owner/three"}]
        )
        return httpx.Response(200, json={"total_count": 4, "items": items})

    client = GitHubClient(httpx.MockTransport(handler))
    try:
        result = client.search("language:Python", per_page=2, max_pages=2)
    finally:
        client.close()

    assert result == ["owner/one", "owner/two", "owner/three"]


def test_reuses_etag_payload_after_not_modified() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 2:
            assert request.headers["if-none-match"] == '"repo-v1"'
            return httpx.Response(304)
        return httpx.Response(
            200,
            headers={"etag": '"repo-v1"'},
            json={
                "id": 1,
                "full_name": "owner/repo",
                "html_url": "https://github.com/owner/repo",
                "stargazers_count": 100,
                "forks_count": 10,
                "open_issues_count": 2,
            },
        )

    client = GitHubClient(httpx.MockTransport(handler))
    try:
        first = client.repository("owner/repo")
        second = client.repository("owner/repo")
    finally:
        client.close()

    assert requests == 2
    assert first == second


def test_prefers_chinese_readme_variant_and_decodes_content() -> None:
    encoded = base64.b64encode("# 中文 README\n\n项目介绍".encode()).decode()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/contents"):
            return httpx.Response(
                200,
                json=[
                    {"name": "README.md", "type": "file"},
                    {"name": "README.zh-CN.md", "type": "file"},
                ],
            )
        assert request.url.path.endswith("/contents/README.zh-CN.md")
        return httpx.Response(
            200,
            json={
                "path": "README.zh-CN.md",
                "encoding": "base64",
                "content": encoded,
                "html_url": "https://github.com/owner/repo/blob/main/README.zh-CN.md",
            },
        )

    client = GitHubClient(httpx.MockTransport(handler))
    try:
        readme = client.readme("owner/repo")
    finally:
        client.close()

    assert readme.path == "README.zh-CN.md"
    assert readme.content == "# 中文 README\n\n项目介绍"
    assert readme.html_url.endswith("README.zh-CN.md")


def test_readme_falls_back_to_default_endpoint() -> None:
    encoded = base64.b64encode(b"# Default README").decode()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/contents"):
            return httpx.Response(200, json=[{"name": "src", "type": "dir"}])
        assert request.url.path.endswith("/readme")
        return httpx.Response(
            200,
            json={"path": "README.md", "encoding": "base64", "content": encoded},
        )

    client = GitHubClient(httpx.MockTransport(handler))
    try:
        readme = client.readme("owner/repo")
    finally:
        client.close()

    assert readme.path == "README.md"
    assert readme.content == "# Default README"


def test_readme_uses_persisted_validators_after_client_restart() -> None:
    encoded = base64.b64encode("# 中文 README".encode()).decode()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/contents"):
            if request.headers.get("if-none-match") == '"root-v1"':
                return httpx.Response(304, headers={"etag": '"root-v1"'})
            return httpx.Response(
                200,
                headers={"etag": '"root-v1"'},
                json=[{"name": "README.zh-CN.md", "type": "file"}],
            )
        assert request.url.path.endswith("/contents/README.zh-CN.md")
        if request.headers.get("if-none-match") == '"readme-v1"':
            return httpx.Response(304, headers={"etag": '"readme-v1"'})
        return httpx.Response(
            200,
            headers={"etag": '"readme-v1"'},
            json={"path": "README.zh-CN.md", "encoding": "base64", "content": encoded},
        )

    first_client = GitHubClient(httpx.MockTransport(handler))
    try:
        first = first_client.readme_conditional("owner/repo")
    finally:
        first_client.close()

    restarted_client = GitHubClient(httpx.MockTransport(handler))
    try:
        second = restarted_client.readme_conditional(
            "owner/repo",
            root_etag=first.root_etag,
            root_entries=first.root_entries,
            readme_etag=first.etag,
            cached_path=first.path,
            cached_endpoint=first.readme_endpoint,
        )
    finally:
        restarted_client.close()

    assert first.content == "# 中文 README"
    assert second.not_modified is True
    assert second.path == first.path
    assert second.root_etag == first.root_etag
    assert second.etag == first.etag
    assert len(requests) == 4
    assert requests[2].headers["if-none-match"] == '"root-v1"'
    assert requests[3].headers["if-none-match"] == '"readme-v1"'


def test_new_chinese_readme_path_does_not_reuse_old_file_etag() -> None:
    encoded = base64.b64encode("# 新中文 README".encode()).decode()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/contents"):
            assert request.headers["if-none-match"] == '"root-v1"'
            return httpx.Response(
                200,
                headers={"etag": '"root-v2"'},
                json=[
                    {"name": "README.md", "type": "file"},
                    {"name": "README.zh-CN.md", "type": "file"},
                ],
            )
        assert request.url.path.endswith("/contents/README.zh-CN.md")
        assert "if-none-match" not in request.headers
        return httpx.Response(
            200,
            headers={"etag": '"zh-v1"'},
            json={"path": "README.zh-CN.md", "encoding": "base64", "content": encoded},
        )

    client = GitHubClient(httpx.MockTransport(handler))
    try:
        readme = client.readme_conditional(
            "owner/repo",
            root_etag='"root-v1"',
            root_entries=[{"name": "README.md", "type": "file"}],
            readme_etag='"english-v1"',
            cached_path="README.md",
            cached_endpoint="contents:README.md",
        )
    finally:
        client.close()

    assert readme.path == "README.zh-CN.md"
    assert readme.content == "# 新中文 README"
    assert readme.root_etag == '"root-v2"'
    assert readme.etag == '"zh-v1"'


def test_default_readme_validator_remains_bound_to_default_endpoint() -> None:
    encoded = base64.b64encode(b"# Default README").decode()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if request.url.path.endswith("/contents"):
            if calls == 1:
                return httpx.Response(200, headers={"etag": '"root-v1"'}, json=[])
            assert request.headers["if-none-match"] == '"root-v1"'
            return httpx.Response(304, headers={"etag": '"root-v1"'})
        assert request.url.path.endswith("/readme")
        if calls == 2:
            assert "if-none-match" not in request.headers
            return httpx.Response(
                200,
                headers={"etag": '"default-v1"'},
                json={"path": "README.md", "encoding": "base64", "content": encoded},
            )
        assert request.headers["if-none-match"] == '"default-v1"'
        return httpx.Response(304, headers={"etag": '"default-v1"'})

    first_client = GitHubClient(httpx.MockTransport(handler))
    try:
        first = first_client.readme_conditional("owner/repo")
    finally:
        first_client.close()
    restarted_client = GitHubClient(httpx.MockTransport(handler))
    try:
        second = restarted_client.readme_conditional(
            "owner/repo",
            root_etag=first.root_etag,
            root_entries=first.root_entries,
            readme_etag=first.etag,
            cached_path=first.path,
            cached_endpoint=first.readme_endpoint,
        )
    finally:
        restarted_client.close()

    assert calls == 4
    assert second.not_modified is True
    assert second.path == "README.md"


def test_deleted_chinese_readme_falls_back_without_old_file_validator() -> None:
    encoded = base64.b64encode(b"# English README").decode()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/contents"):
            assert request.headers["if-none-match"] == '"root-with-zh"'
            return httpx.Response(
                200,
                headers={"etag": '"root-without-zh"'},
                json=[{"name": "README.md", "type": "file"}],
            )
        assert request.url.path.endswith("/readme")
        assert "if-none-match" not in request.headers
        return httpx.Response(
            200,
            headers={"etag": '"english-v1"'},
            json={"path": "README.md", "encoding": "base64", "content": encoded},
        )

    client = GitHubClient(httpx.MockTransport(handler))
    try:
        readme = client.readme_conditional(
            "owner/repo",
            root_etag='"root-with-zh"',
            root_entries=[{"name": "README.zh-CN.md", "type": "file"}],
            readme_etag='"chinese-v1"',
            cached_path="README.zh-CN.md",
            cached_endpoint="contents:README.zh-CN.md",
        )
    finally:
        client.close()

    assert readme.not_modified is False
    assert readme.path == "README.md"
    assert readme.content == "# English README"


def test_readme_raises_for_missing_content() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/contents"):
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={"message": "Not Found"})

    client = GitHubClient(httpx.MockTransport(handler))
    try:
        with pytest.raises(GitHubClientError, match="GitHub request failed: 404"):
            client.readme("owner/missing")
    finally:
        client.close()


@pytest.mark.parametrize("status_code", [403, 429])
def test_converts_rate_limit_responses(status_code: int) -> None:
    client = GitHubClient(
        httpx.MockTransport(lambda _: httpx.Response(
            status_code, json={"message": "API rate limit exceeded"},
        ))
    )
    try:
        with pytest.raises(GitHubRateLimitError, match="rate limit"):
            client.search("stars:>100")
    finally:
        client.close()


def test_rate_limit_retry_after_and_reserve_are_observed() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={
                "retry-after": "37",
                "x-ratelimit-remaining": "4",
                "x-ratelimit-reset": "2000000000",
            },
            json={"message": "secondary rate limit"},
        )

    client = GitHubClient(httpx.MockTransport(handler))
    try:
        with pytest.raises(GitHubRateLimitError) as error:
            client.readme("owner/repo")
        assert error.value.retry_after == 37
        assert error.value.secondary is True
        assert client.has_quota(5) is False
        assert client.has_quota(4) is True
    finally:
        client.close()
