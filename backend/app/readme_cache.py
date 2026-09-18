"""Shared names for the short lived README response cache."""


def readme_cache_key(repository_id: int) -> str:
    return f"readme:v2:{repository_id}"
