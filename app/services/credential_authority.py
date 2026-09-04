from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal
from urllib.parse import urlsplit


@dataclass(frozen=True)
class CredentialAuthority:
    platform: str
    transport: Literal["api", "bmc", "ssh"]
    endpoint: tuple[object, ...]
    port: int | None
    username: str
    verify_tls: bool | None = None
    tls_ca_bundle_path: str = ""
    tls_server_name: str = ""
    strict_host_key_checking: bool | None = None


def api_credential_authority(
    *,
    platform: str, host: str | None,
    username: str | None,
    verify_tls: bool,
    tls_ca_bundle_path: str | None,
    tls_server_name: str | None,
) -> CredentialAuthority | None:
    endpoint = _endpoint_identity(host, include_resource=True)
    if endpoint is None:
        return None
    return CredentialAuthority(
        platform=_text(platform).lower(),
        transport="api",
        endpoint=endpoint,
        port=None,
        username=_text(username),
        verify_tls=bool(verify_tls),
        tls_ca_bundle_path=_text(tls_ca_bundle_path),
        tls_server_name=_text(tls_server_name),
    )


def bmc_credential_authority(
    *,
    platform: str, host: str | None,
    username: str | None,
    verify_tls: bool,
) -> CredentialAuthority | None:
    normalized_host = _text(host)
    if normalized_host and "://" not in normalized_host:
        normalized_host = f"https://{normalized_host}"
    endpoint = _endpoint_identity(normalized_host, include_resource=True)
    if endpoint is None:
        return None
    return CredentialAuthority(
        platform=_text(platform).lower(),
        transport="bmc",
        endpoint=endpoint,
        port=None,
        username=_text(username),
        verify_tls=bool(verify_tls),
    )


def ssh_credential_authorities(
    *,
    platform: str,
    hosts: Iterable[str | None],
    port: int,
    username: str | None,
    strict_host_key_checking: bool,
) -> frozenset[CredentialAuthority]:
    authorities: set[CredentialAuthority] = set()
    for host in hosts:
        endpoint = _endpoint_identity(host, include_resource=False)
        if endpoint is None:
            continue
        authorities.add(
            CredentialAuthority(
                platform=_text(platform).lower(),
                transport="ssh",
                endpoint=endpoint,
                port=int(port),
                username=_text(username),
                strict_host_key_checking=bool(strict_host_key_checking),
            )
        )
    return frozenset(authorities)


def same_credential_authority(
    left: CredentialAuthority | None,
    right: CredentialAuthority | None,
) -> bool:
    return left is not None and right is not None and left == right


def same_credential_authorities(
    left: Iterable[CredentialAuthority],
    right: Iterable[CredentialAuthority],
) -> bool:
    left_set = frozenset(left)
    right_set = frozenset(right)
    return (
        bool(left_set)
        and len(left_set) == len(right_set)
        and all(any(same_credential_authority(candidate, saved) for saved in right_set) for candidate in left_set)
    )


def credential_authorities_are_approved(
    candidates: Iterable[CredentialAuthority],
    approved: Iterable[CredentialAuthority],
) -> bool:
    candidate_set = frozenset(candidates)
    approved_set = frozenset(approved)
    return bool(candidate_set) and all(
        any(same_credential_authority(candidate, saved) for saved in approved_set) for candidate in candidate_set
    )


def _text(value: str | None) -> str:
    return str(value or "").strip()


def _endpoint_identity(
    value: str | None,
    *,
    include_resource: bool,
) -> tuple[object, ...] | None:
    normalized = _text(value)
    if not normalized:
        return None
    candidate = normalized if "://" in normalized else f"//{normalized}"
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError:
        return None
    if parsed.hostname is None:
        return None
    scheme = parsed.scheme.lower()
    if port is None:
        port = {"http": 80, "https": 443}.get(scheme)
    identity: tuple[object, ...] = (
        scheme,
        parsed.username,
        parsed.password,
        parsed.hostname.lower(),
        port,
    )
    if include_resource:
        identity += (
            parsed.path.rstrip("/"),
            parsed.query,
            parsed.fragment,
        )
    elif parsed.path.rstrip("/") or parsed.query or parsed.fragment:
        identity += (
            parsed.path.rstrip("/"),
            parsed.query,
            parsed.fragment,
        )
    return identity
