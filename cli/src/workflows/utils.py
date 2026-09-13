"""Helpers shared across the workflows layer."""


def strip_url_scheme(url: str) -> str:
    """Strip ``https://`` / ``http://`` prefixes for fnmatch comparisons."""
    for prefix in ("https://", "http://"):
        if url.startswith(prefix):
            return url[len(prefix) :]
    return url
