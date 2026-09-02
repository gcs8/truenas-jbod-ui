from __future__ import annotations

import base64
import binascii
import hmac
from urllib.parse import urlsplit

from pydantic import SecretStr
from starlette.requests import Request


def basic_auth_matches(
    authorization: str | None,
    username: str | None,
    password: SecretStr | None,
) -> bool:
    if not authorization:
        return False
    scheme, separator, encoded = authorization.partition(" ")
    if separator != " " or scheme.lower() != "basic" or not encoded:
        return False
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return False
    supplied_username, separator, supplied_password = decoded.partition(b":")
    if separator != b":" or password is None:
        return False
    expected_username = str(username or "").encode("utf-8")
    expected_password = password.get_secret_value().encode("utf-8")
    username_matches = hmac.compare_digest(supplied_username, expected_username)
    password_matches = hmac.compare_digest(supplied_password, expected_password)
    return bool(username_matches & password_matches)


def origin_identity(value: str | None) -> tuple[str, str, int] | None:
    candidate = str(value or "").strip()
    if not candidate:
        return None
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    if (
        scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    if port is None:
        port = 443 if scheme == "https" else 80
    return scheme, parsed.hostname.lower(), port


def configured_origin_identity(value: str | None) -> tuple[str, str, int] | None:
    candidate = str(value or "").strip()
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return None
    if parsed.path or parsed.query or parsed.fragment or "?" in candidate or "#" in candidate:
        return None
    return origin_identity(candidate)


def request_origin_allowed(request: Request, public_origin: str | None) -> bool:
    supplied_origins = request.headers.getlist("origin")
    supplied_referers = request.headers.getlist("referer")
    supplied_origins.extend(supplied_referers)
    if not supplied_origins:
        return True
    configured_origin = configured_origin_identity(public_origin)
    return configured_origin is not None and all(
        origin_identity(candidate) == configured_origin
        for candidate in supplied_origins
    )
