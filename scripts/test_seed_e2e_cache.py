import json
import os
from urllib.parse import urlsplit

from redis import Redis


def main() -> None:
    redis_url = os.environ.get("TEST_REDIS_URL", "")
    parsed = urlsplit(redis_url)
    database = int((parsed.path or "/0").lstrip("/"))
    if parsed.hostname not in {"127.0.0.1", "localhost"} or database <= 0:
        raise RuntimeError("TEST_REDIS_URL must target a non-default loopback Redis database")

    readme = {
        "repository": "fastapi/fastapi",
        "path": "README.md",
        "content": (
            "# FastAPI\n\n"
            "FastAPI is a modern Python web framework for building APIs.\n\n"
            "```python\nfrom fastapi import FastAPI\napp = FastAPI()\n```\n"
        ),
        "html_url": "https://github.com/fastapi/fastapi/blob/master/README.md",
    }
    client = Redis.from_url(redis_url, decode_responses=True)
    try:
        client.set(
            "readme:v1:fastapi/fastapi",
            json.dumps(readme, ensure_ascii=False),
            ex=3_600,
        )
    finally:
        client.close()


if __name__ == "__main__":
    main()
