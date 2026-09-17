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
