"""One browser write-origin decision for the main, admin and history services.

Invariant: a state-changing browser request is accepted on the strength of its
own Host header only after that header names exactly one valid host that passes
the shared known-host check, and every Origin and Referer the request carries
agrees with the accepted origin. A configured public origin stays authoritative.
The main, admin and history wrappers reach the same decision for the same
request; only their response text and their separate authentication differ.
"""

from __future__ import annotations

import base64
import unittest
from dataclasses import dataclass, field
from types import SimpleNamespace

from fastapi import HTTPException
from pydantic import SecretStr
from starlette.requests import Request

from admin_service.config import AdminSettings
from admin_service.route_support import _request_origin_allowed, cross_origin_rejection_detail
from app.read_ui_auth_config import ReadUiAuthSettings
from app.route_support import require_read_ui_mutation_authorization
from history_service.config import HistorySettings
from history_service.refresh_auth import authorize_refresh_request

ALLOW = "allow"
UNLISTED = "unlisted_host"
CROSS = "cross_origin"

PUBLIC_ORIGIN = "https://ui.example.invalid"
LISTED = "listed.example.invalid"
USERNAME = "operator"
PASSWORD = "synthetic-passphrase"
TOKEN = "synthetic-token"


@dataclass(frozen=True)
class Row:
    label: str
    expected: str
    host: tuple[str, ...] = ("192.0.2.10:8080",)
    origin: tuple[str, ...] = ()
    referer: tuple[str, ...] = ()
    scheme: str = "http"
    extra: tuple[tuple[bytes, bytes], ...] = field(default=())

    @property
    def carries_origin(self) -> bool:
        return bool(self.origin or self.referer)


def build_request(row: Row, *, app: object | None = None, authorization: str | None = None) -> Request:
    headers: list[tuple[bytes, bytes]] = [(b"content-type", b"application/json")]
    headers.extend((b"host", value.encode("latin-1")) for value in row.host)
    headers.extend((b"origin", value.encode("latin-1")) for value in row.origin)
    headers.extend((b"referer", value.encode("latin-1")) for value in row.referer)
    headers.extend(row.extra)
    if authorization is not None:
        headers.append((b"authorization", authorization.encode("ascii")))
    scope: dict[str, object] = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": row.scheme,
        "path": "/api/synthetic",
        "raw_path": b"/api/synthetic",
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("192.0.2.99", 12345),
        "server": ("192.0.2.1", 8000),
    }
    if app is not None:
        scope["app"] = app
    return Request(scope)


def basic_header() -> str:
    return "Basic " + base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode("ascii")


# Rows evaluated with a configured public origin. The request's own Host plays
# no part, so an unlisted or malformed Host behind a proxy is irrelevant.
CONFIGURED_ROWS = (
    Row("configured: no origin headers", ALLOW),
    Row("configured: no origin headers, unlisted proxy host", ALLOW, host=("backend.example.invalid:8000",)),
    Row("configured: no origin headers, duplicate host", ALLOW, host=("192.0.2.10", "192.0.2.11")),
    Row("configured: matching origin", ALLOW, origin=(PUBLIC_ORIGIN,)),
    Row("configured: matching origin with explicit default port", ALLOW, origin=("https://ui.example.invalid:443",)),
    Row("configured: matching origin in another case", ALLOW, origin=("HTTPS://UI.Example.Invalid",)),
    Row("configured: matching referer", ALLOW, referer=(PUBLIC_ORIGIN + "/page?x=1",)),
    Row("configured: matching origin and referer", ALLOW, origin=(PUBLIC_ORIGIN,), referer=(PUBLIC_ORIGIN + "/",)),
    Row("configured: two matching origins", ALLOW, origin=(PUBLIC_ORIGIN, PUBLIC_ORIGIN)),
    Row(
        "configured: proxy host with matching origin",
        ALLOW,
        host=("backend.example.invalid:8000",),
        origin=(PUBLIC_ORIGIN,),
    ),
    Row(
        "configured: proxy host with origin equal to that host",
        CROSS,
        host=("backend.example.invalid:8000",),
        origin=("http://backend.example.invalid:8000",),
    ),
    Row("configured: other origin", CROSS, origin=("https://other.example.invalid",)),
    Row("configured: other port", CROSS, origin=("https://ui.example.invalid:8443",)),
    Row("configured: other scheme", CROSS, origin=("http://ui.example.invalid",)),
    Row("configured: opaque origin", CROSS, origin=("null",)),
    Row("configured: mismatching referer", CROSS, referer=("https://other.example.invalid/page",)),
    Row(
        "configured: matching origin with mismatching referer",
        CROSS,
        origin=(PUBLIC_ORIGIN,),
        referer=("https://other.example.invalid/page",),
    ),
    Row("configured: one of two origins differs", CROSS, origin=(PUBLIC_ORIGIN, "https://other.example.invalid")),
    Row(
        "configured: origin equal to an IP host is not the configured origin",
        CROSS,
        origin=("http://192.0.2.10:8080",),
    ),
)

# Rows evaluated with no public origin, the Host fallback on and no extra
# allowed names: only IP literals and localhost pass the known-host check.
FALLBACK_ROWS = (
    Row("fallback: IPv4 host, no origin headers", ALLOW),
    Row("fallback: IPv4 host, matching origin", ALLOW, origin=("http://192.0.2.10:8080",)),
    Row("fallback: IPv4 host, matching referer", ALLOW, referer=("http://192.0.2.10:8080/history?x=1",)),
    Row(
        "fallback: IPv4 host, matching origin and referer",
        ALLOW,
        origin=("http://192.0.2.10:8080",),
        referer=("http://192.0.2.10:8080/",),
    ),
    Row(
        "fallback: IPv4 host, two matching origins",
        ALLOW,
        origin=("http://192.0.2.10:8080", "http://192.0.2.10:8080"),
    ),
    Row("fallback: IPv4 host on default port, origin names the port", ALLOW, host=("192.0.2.10",), origin=("http://192.0.2.10:80",)),
    Row(
        "fallback: IPv4 host on https default port",
        ALLOW,
        host=("192.0.2.10",),
        origin=("https://192.0.2.10:443",),
        scheme="https",
    ),
    Row("fallback: localhost host, matching origin", ALLOW, host=("localhost:8080",), origin=("http://localhost:8080",)),
    Row("fallback: localhost in another case", ALLOW, host=("LocalHost:8080",), origin=("http://localhost:8080",)),
    Row(
        "fallback: IPv6 host, matching origin",
        ALLOW,
        host=("[2001:db8::1]:8080",),
        origin=("http://[2001:db8::1]:8080",),
    ),
    Row("fallback: IPv6 loopback, default port", ALLOW, host=("[::1]",), origin=("http://[::1]",)),
    Row("fallback: IPv4 host, other origin", CROSS, origin=("http://192.0.2.11:8080",)),
    Row("fallback: IPv4 host, other port", CROSS, origin=("http://192.0.2.10:8081",)),
    Row("fallback: IPv4 host, other scheme", CROSS, origin=("https://192.0.2.10:8080",)),
    Row("fallback: IPv4 host, opaque origin", CROSS, origin=("null",)),
    Row("fallback: IPv4 host, mismatching referer", CROSS, referer=("http://other.example.invalid/page",)),
    Row(
        "fallback: IPv4 host, matching origin with mismatching referer",
        CROSS,
        origin=("http://192.0.2.10:8080",),
        referer=("http://other.example.invalid/page",),
    ),
    Row(
        "fallback: IPv4 host, one of two origins differs",
        CROSS,
        origin=("http://192.0.2.10:8080", "http://other.example.invalid"),
    ),
    Row("fallback: unlisted name, no origin headers", UNLISTED, host=("unlisted.example.invalid:8080",)),
    Row(
        "fallback: unlisted name, origin agrees with host",
        UNLISTED,
        host=("unlisted.example.invalid:8080",),
        origin=("http://unlisted.example.invalid:8080",),
    ),
    Row(
        "fallback: unlisted name, referer agrees with host",
        UNLISTED,
        host=("unlisted.example.invalid:8080",),
        referer=("http://unlisted.example.invalid:8080/page",),
    ),
    Row(
        "fallback: unlisted name, other origin",
        UNLISTED,
        host=("unlisted.example.invalid:8080",),
        origin=("http://192.0.2.10:8080",),
    ),
    Row(
        "fallback: name listed elsewhere is not listed here",
        UNLISTED,
        host=(LISTED + ":8080",),
        origin=(f"http://{LISTED}:8080",),
    ),
    Row("fallback: missing host, no origin headers", UNLISTED, host=()),
    Row("fallback: missing host, origin", UNLISTED, host=(), origin=("http://192.0.2.1:8000",)),
    Row(
        "fallback: duplicate host, origin agrees with the first",
        UNLISTED,
        host=("192.0.2.10:8080", "192.0.2.10:8080"),
        origin=("http://192.0.2.10:8080",),
    ),
    Row("fallback: duplicate host, no origin headers", UNLISTED, host=("192.0.2.10:8080", "192.0.2.11:8080")),
    Row("fallback: empty host, origin", UNLISTED, host=("",), origin=("http://192.0.2.10:8080",)),
    Row("fallback: host with space, origin", UNLISTED, host=("bad host.invalid",), origin=("http://bad host.invalid",)),
    Row("fallback: host with port out of range", UNLISTED, host=("192.0.2.10:99999",), origin=("http://192.0.2.10",)),
    Row(
        "fallback: host with userinfo",
        UNLISTED,
        host=("user@192.0.2.10:8080",),
        origin=("http://192.0.2.10:8080",),
    ),
    Row(
        "fallback: unbracketed IPv6 host",
        UNLISTED,
        host=("2001:db8::1",),
        origin=("http://[2001:db8::1]",),
    ),
    Row(
        "fallback: host with path",
        UNLISTED,
        host=("192.0.2.10:8080/x",),
        origin=("http://192.0.2.10:8080",),
    ),
)

# Rows evaluated with no public origin, the Host fallback on and one extra
# allowed name. History has no list of extra names, so these rows apply to
# main and admin only.
LISTED_ROWS = (
    Row("listed: listed name, matching origin", ALLOW, host=(LISTED + ":8080",), origin=(f"http://{LISTED}:8080",)),
    Row("listed: listed name, no origin headers", ALLOW, host=(LISTED,)),
    Row(
        "listed: listed name in another case",
        ALLOW,
        host=("Listed.Example.Invalid:8080",),
        origin=(f"http://{LISTED}:8080",),
    ),
    Row("listed: listed name, other origin", CROSS, host=(LISTED + ":8080",), origin=("http://192.0.2.10:8080",)),
    Row(
        "listed: listed name, mismatching referer",
        CROSS,
        host=(LISTED + ":8080",),
        origin=(f"http://{LISTED}:8080",),
        referer=("http://other.example.invalid/",),
    ),
    Row(
        "listed: unlisted name",
        UNLISTED,
        host=("unlisted.example.invalid:8080",),
        origin=("http://unlisted.example.invalid:8080",),
    ),
    Row("listed: subdomain of a listed name", UNLISTED, host=("sub." + LISTED,), origin=("http://sub." + LISTED,)),
)

# Rows evaluated with no public origin and the Host fallback off (main basic
# mode): no origin can be trusted, so only a request without one is accepted.
NO_FALLBACK_ROWS = (
    Row("no fallback: no origin headers", ALLOW),
    Row("no fallback: no origin headers, unlisted host", ALLOW, host=("unlisted.example.invalid",)),
    Row("no fallback: origin agrees with an IP host", CROSS, origin=("http://192.0.2.10:8080",)),
    Row("no fallback: referer agrees with an IP host", CROSS, referer=("http://192.0.2.10:8080/",)),
    Row(
        "no fallback: origin agrees with an unlisted host",
        CROSS,
        host=("unlisted.example.invalid",),
        origin=("http://unlisted.example.invalid",),
    ),
)


def _decision():
    # Looked up at call time so the service rows below still run when the
    # helper is absent.
    from app import http_auth

    return http_auth.browser_write_origin_decision


class BrowserWriteOriginDecisionMatrixTests(unittest.TestCase):
    def assert_rows(self, rows, *, public_origin, allowed_hosts, allow_host_fallback) -> None:
        decide = _decision()
        for row in rows:
            with self.subTest(row=row.label):
                self.assertEqual(
                    decide(
                        build_request(row),
                        public_origin,
                        allowed_hosts,
                        allow_host_fallback=allow_host_fallback,
                    ),
                    row.expected,
                )

    def test_configured_public_origin_is_authoritative_with_or_without_host_fallback(self) -> None:
        for fallback in (True, False):
            with self.subTest(allow_host_fallback=fallback):
                self.assert_rows(
                    CONFIGURED_ROWS,
                    public_origin=PUBLIC_ORIGIN,
                    allowed_hosts=frozenset({LISTED}),
                    allow_host_fallback=fallback,
                )

    def test_host_fallback_needs_one_known_host_and_agreeing_origins(self) -> None:
        self.assert_rows(FALLBACK_ROWS, public_origin=None, allowed_hosts=frozenset(), allow_host_fallback=True)

    def test_empty_public_origin_uses_the_host_fallback(self) -> None:
        self.assert_rows(FALLBACK_ROWS, public_origin="", allowed_hosts=frozenset(), allow_host_fallback=True)

    def test_host_fallback_accepts_only_listed_extra_names(self) -> None:
        self.assert_rows(
            LISTED_ROWS,
            public_origin=None,
            allowed_hosts=frozenset({LISTED}),
            allow_host_fallback=True,
        )

    def test_without_host_fallback_no_request_origin_is_trusted(self) -> None:
        self.assert_rows(
            NO_FALLBACK_ROWS,
            public_origin=None,
            allowed_hosts=frozenset({LISTED}),
            allow_host_fallback=False,
        )

    def test_matrix_has_positive_and_negative_rows_of_each_kind(self) -> None:
        for name, rows, kinds, origin_allowed in (
            ("configured", CONFIGURED_ROWS, {ALLOW, CROSS}, True),
            ("fallback", FALLBACK_ROWS, {ALLOW, CROSS, UNLISTED}, True),
            ("listed", LISTED_ROWS, {ALLOW, CROSS, UNLISTED}, True),
            ("no fallback", NO_FALLBACK_ROWS, {ALLOW, CROSS}, False),
        ):
            with self.subTest(rows=name):
                self.assertEqual({row.expected for row in rows}, kinds)
                self.assertEqual(any(row.carries_origin and row.expected == ALLOW for row in rows), origin_allowed)


class ServiceWrapperParityTests(unittest.TestCase):
    """Each wrapper reaches the shared decision; only the wording differs."""

    @staticmethod
    def main_outcome(row: Row, *, auth_mode: str, public_origin: str | None, allowed_hosts: str) -> str:
        if auth_mode == "basic":
            auth = ReadUiAuthSettings(
                auth_mode="basic",
                auth_username=USERNAME,
                auth_password=SecretStr(PASSWORD),
                allowed_hosts=allowed_hosts,
            )
            authorization = basic_header()
        else:
            auth = ReadUiAuthSettings(auth_mode="network", allowed_hosts=allowed_hosts)
            authorization = None
        app = SimpleNamespace(
            state=SimpleNamespace(operator_auth_settings=auth, read_ui_public_origin=public_origin)
        )
        try:
            require_read_ui_mutation_authorization(build_request(row, app=app, authorization=authorization))
        except HTTPException as exc:
            if exc.status_code != 403:
                raise
            if exc.detail == "This request came from a different site and was blocked.":
                return CROSS
            if str(exc.detail).startswith("The main UI only accepts changes from an IP address, localhost"):
                return UNLISTED
            raise
        return ALLOW

    @staticmethod
    def admin_outcome(row: Row, *, auth_mode: str, public_origin: str | None, allowed_hosts: str) -> str:
        if auth_mode == "basic":
            settings = AdminSettings(
                auth_mode="basic",
                auth_username=USERNAME,
                auth_password=SecretStr(PASSWORD),
                public_origin=public_origin,
                allowed_hosts=allowed_hosts,
                auto_stop_seconds=0,
            )
        else:
            settings = AdminSettings(public_origin=public_origin, allowed_hosts=allowed_hosts, auto_stop_seconds=0)
        request = build_request(row)
        if _request_origin_allowed(request, settings):
            return ALLOW
        detail = cross_origin_rejection_detail(request, settings)
        if detail.startswith("The admin service only accepts changes from an IP address, localhost"):
            return UNLISTED
        if detail.startswith("This page was opened at "):
            return CROSS
        raise AssertionError(detail)

    @staticmethod
    def history_outcome(row: Row, *, auth_mode: str, public_origin: str | None) -> str:
        if auth_mode == "token":
            settings = HistorySettings(refresh_auth_mode="token", refresh_token=SecretStr(TOKEN), public_origin=public_origin)
            authorization = f"Bearer {TOKEN}"
        else:
            settings = HistorySettings(public_origin=public_origin)
            authorization = None
        try:
            authorize_refresh_request(build_request(row, authorization=authorization), settings)
        except HTTPException as exc:
            if exc.status_code == 403 and exc.detail == "Cross-origin history refresh rejected.":
                return "refused"
            raise
        return ALLOW

    @staticmethod
    def history_expected(row: Row) -> str:
        # History serves server-side refresh calls that carry no Origin or
        # Referer; those are not browser writes and its token or network check
        # decides them. Every request that carries either header gets the shared
        # decision, with any refusal reported in history's single wording.
        if not row.carries_origin:
            return ALLOW
        return ALLOW if row.expected == ALLOW else "refused"

    def test_main_network_mode_matches_the_shared_matrix(self) -> None:
        for rows, public_origin, allowed in (
            (CONFIGURED_ROWS, PUBLIC_ORIGIN, LISTED),
            (FALLBACK_ROWS, None, ""),
            (LISTED_ROWS, None, LISTED),
        ):
            for row in rows:
                with self.subTest(row=row.label):
                    self.assertEqual(
                        self.main_outcome(row, auth_mode="network", public_origin=public_origin, allowed_hosts=allowed),
                        row.expected,
                    )

    def test_main_basic_mode_never_trusts_the_request_host(self) -> None:
        for rows, public_origin in ((CONFIGURED_ROWS, PUBLIC_ORIGIN), (NO_FALLBACK_ROWS, None)):
            for row in rows:
                with self.subTest(row=row.label):
                    self.assertEqual(
                        self.main_outcome(row, auth_mode="basic", public_origin=public_origin, allowed_hosts=LISTED),
                        row.expected,
                    )

    def test_admin_matches_the_shared_matrix_in_both_auth_modes(self) -> None:
        cases = [
            ("network", CONFIGURED_ROWS, PUBLIC_ORIGIN, LISTED),
            ("basic", CONFIGURED_ROWS, PUBLIC_ORIGIN, LISTED),
            ("network", FALLBACK_ROWS, None, ""),
            ("network", LISTED_ROWS, None, LISTED),
        ]
        for auth_mode, rows, public_origin, allowed in cases:
            for row in rows:
                with self.subTest(auth_mode=auth_mode, row=row.label):
                    self.assertEqual(
                        self.admin_outcome(row, auth_mode=auth_mode, public_origin=public_origin, allowed_hosts=allowed),
                        row.expected,
                    )

    def test_admin_configured_origin_with_trailing_slash_is_still_authoritative(self) -> None:
        for row in CONFIGURED_ROWS:
            with self.subTest(row=row.label):
                self.assertEqual(
                    self.admin_outcome(row, auth_mode="network", public_origin=PUBLIC_ORIGIN + "/", allowed_hosts=""),
                    row.expected,
                )

    def test_history_with_a_configured_origin_matches_the_shared_matrix(self) -> None:
        for auth_mode in ("network", "token"):
            for row in CONFIGURED_ROWS:
                with self.subTest(auth_mode=auth_mode, row=row.label):
                    self.assertEqual(
                        self.history_outcome(row, auth_mode=auth_mode, public_origin=PUBLIC_ORIGIN),
                        self.history_expected(row),
                    )

    def test_history_without_a_public_origin_trusts_its_host_only_after_the_known_host_check(self) -> None:
        for auth_mode in ("network", "token"):
            for row in FALLBACK_ROWS:
                with self.subTest(auth_mode=auth_mode, row=row.label):
                    self.assertEqual(
                        self.history_outcome(row, auth_mode=auth_mode, public_origin=None),
                        self.history_expected(row),
                    )

    def test_history_has_no_extra_allowed_names(self) -> None:
        for row in LISTED_ROWS:
            if not row.carries_origin or row.expected != ALLOW:
                continue
            with self.subTest(row=row.label):
                self.assertEqual(self.history_outcome(row, auth_mode="network", public_origin=None), "refused")

    def test_three_services_agree_on_every_request_that_carries_an_origin(self) -> None:
        for rows, public_origin in ((CONFIGURED_ROWS, PUBLIC_ORIGIN), (FALLBACK_ROWS, None)):
            for row in rows:
                if not row.carries_origin:
                    continue
                with self.subTest(row=row.label):
                    main = self.main_outcome(row, auth_mode="network", public_origin=public_origin, allowed_hosts="")
                    admin = self.admin_outcome(row, auth_mode="network", public_origin=public_origin, allowed_hosts="")
                    history = self.history_outcome(row, auth_mode="network", public_origin=public_origin)
                    self.assertEqual(main, admin)
                    self.assertEqual(history == ALLOW, main == ALLOW)

    def test_history_leaves_requests_without_origin_or_referer_to_its_refresh_authentication(self) -> None:
        server_call = Row("server-side call", ALLOW, host=("history.example.invalid:8001",))
        self.assertEqual(self.history_outcome(server_call, auth_mode="network", public_origin=None), ALLOW)
        self.assertEqual(self.history_outcome(server_call, auth_mode="token", public_origin=None), ALLOW)
        settings = HistorySettings(refresh_auth_mode="token", refresh_token=SecretStr(TOKEN))
        for authorization in (None, "Bearer wrong"):
            with self.subTest(authorization=authorization):
                with self.assertRaises(HTTPException) as raised:
                    authorize_refresh_request(build_request(server_call, authorization=authorization), settings)
                self.assertEqual(raised.exception.status_code, 401)

    def test_history_checks_the_origin_before_the_token_and_still_requires_the_token(self) -> None:
        settings = HistorySettings(refresh_auth_mode="token", refresh_token=SecretStr(TOKEN))
        unlisted = Row(
            "unlisted",
            UNLISTED,
            host=("unlisted.example.invalid:8081",),
            origin=("http://unlisted.example.invalid:8081",),
        )
        known = Row("known", ALLOW, host=("127.0.0.1:8081",), origin=("http://127.0.0.1:8081",))
        for row, authorization, status in (
            (unlisted, None, 403),
            (unlisted, "Bearer wrong", 403),
            (unlisted, f"Bearer {TOKEN}", 403),
            (known, None, 401),
            (known, "Bearer wrong", 401),
        ):
            with self.subTest(row=row.label, authorization=authorization):
                with self.assertRaises(HTTPException) as raised:
                    authorize_refresh_request(build_request(row, authorization=authorization), settings)
                self.assertEqual(raised.exception.status_code, status)
        authorize_refresh_request(build_request(known, authorization=f"Bearer {TOKEN}"), settings)


if __name__ == "__main__":
    unittest.main()
