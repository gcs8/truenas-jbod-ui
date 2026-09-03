from __future__ import annotations

import re
import secrets
from contextlib import contextmanager
from contextvars import ContextVar, Token
from collections.abc import Iterator, Mapping

REQUEST_ID_HEADER = "X-Request-ID"
REQUEST_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
_parent_request_id: ContextVar[str | None] = ContextVar("parent_request_id", default=None)


def generate_request_id() -> str:
    return secrets.token_hex(16)


def validate_request_id(value: str | None) -> str | None:
    candidate = str(value or "")
    if REQUEST_ID_PATTERN.fullmatch(candidate) is None:
        return None
    return candidate


def current_request_id() -> str | None:
    return _request_id.get()


def current_parent_request_id() -> str | None:
    return _parent_request_id.get()


@contextmanager
def request_context(request_id: str, parent_request_id: str | None = None) -> Iterator[None]:
    request_token: Token[str | None] = _request_id.set(request_id)
    parent_token: Token[str | None] = _parent_request_id.set(parent_request_id)
    try:
        yield
    finally:
        _parent_request_id.reset(parent_token)
        _request_id.reset(request_token)


def request_id_headers(headers: Mapping[str, str] | None = None) -> dict[str, str]:
    propagated = {
        key: value
        for key, value in (headers or {}).items()
        if key.lower() != REQUEST_ID_HEADER.lower()
    }
    request_id = current_request_id()
    if request_id is not None:
        propagated[REQUEST_ID_HEADER] = request_id
    return propagated
