"""Tiny helpers for push-monitor URLs."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def with_param(url: str, key: str, value: str) -> str:
    """Return `url` with `key=value` set, replacing any existing value and
    adding '?' or '&' as the URL actually needs."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != key]
    query.append((key, value))
    return urlunsplit(parts._replace(query=urlencode(query)))
