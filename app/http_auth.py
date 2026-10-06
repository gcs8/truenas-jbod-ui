from __future__ import annotations

import base64
import binascii
import hmac
import ipaddress
import re
from collections.abc import Collection
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


# Host checks for writes when no public origin is configured (#779).
#
# Without a public origin, the accepted origin comes from the Host header, so a
# DNS-rebinding name that resolves to this server presents a matching Host and
# Origin. Rebinding needs an attacker-controlled host name: a browser never
# sends an IP literal as Host for a name. Writes are therefore accepted for an
# IP address (which also covers a specific bind address, always an IP),
# localhost, or a name the operator lists in ADMIN_ALLOWED_HOSTS.
_HOST_LABEL = re.compile(r"(?!-)[a-z0-9_-]{1,63}(?<!-)")
_HOST_HEADER = re.compile(r"(?P<host>\[[^\]]*\]|[^:\[\]]*)(?::(?P<port>[0-9]{1,5}))?")


def _host_name(text: str) -> str | None:
    """A canonical IP address, or a lower-case DNS name without its trailing dot."""
    if text.startswith("[") and text.endswith("]"):
        try:
            return str(ipaddress.IPv6Address(text[1:-1]))
        except ValueError:
            return None
    try:
        return str(ipaddress.IPv4Address(text))
    except ValueError:
        pass
    name = text.lower().removesuffix(".")
    if len(name) > 253 or not all(_HOST_LABEL.fullmatch(label) for label in name.split(".")):
        return None
    return name


def parse_host_header(value: str | None) -> str | None:
    """The host name in a ``Host`` header, without its port; None when malformed."""
    match = _HOST_HEADER.fullmatch(str(value or ""))
    if match is None or (match["port"] is not None and int(match["port"]) > 65535):
        return None
    return _host_name(match["host"])


def parse_allowed_hosts(value: str | None) -> frozenset[str]:
    """Normalize a comma-separated ADMIN_ALLOWED_HOSTS value; reject anything but names and IPs."""
    names: set[str] = set()
    for entry in str(value or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            name: str | None = str(ipaddress.ip_address(entry))
        except ValueError:
            name = _host_name(entry)
        if name is None:
            raise ValueError(
                f"{entry!r} is not a host name or IP address. List names separated by commas, "
                "without a scheme, port or path."
            )
        names.add(name)
    return frozenset(names)


def _is_ip_address(name: str) -> bool:
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


def request_host_allows_writes(request: Request, allowed_hosts: Collection[str]) -> bool:
    """Whether a write may trust the request's own Host; exactly one readable Host is required."""
    hosts = request.headers.getlist("host")
    name = parse_host_header(hosts[0]) if len(hosts) == 1 else None
    if name is None:
        return False
    return name == "localhost" or _is_ip_address(name) or name in allowed_hosts


def unlisted_host_rejection_detail(request: Request, *, service: str, origin_setting: str, container: str) -> str:
    hosts = request.headers.getlist("host")
    name = parse_host_header(hosts[0]) if len(hosts) == 1 else None
    accepted = (
        f"{service} only accepts changes from an IP address, localhost or a name listed in "
        "ADMIN_ALLOWED_HOSTS."
    )
    if name is None:
        return (
            f"{accepted} This request's Host header could not be read. Open the page by IP address, "
            f"or set {origin_setting} in .env and recreate the {container} container."
        )
    return (
        f"{accepted} This page was opened at {name}. Add {name} to ADMIN_ALLOWED_HOSTS in .env, "
        f"or set {origin_setting}, then recreate the {container} container."
    )
