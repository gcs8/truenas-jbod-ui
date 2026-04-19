from __future__ import annotations

import http.client
import ssl
import urllib.request
from urllib.parse import urlsplit

from app.config import TrueNASConfig


def normalize_tls_target(host: str) -> str:
    if "://" not in host:
        return f"https://{host}"
    return host


def host_uses_tls(host: str) -> bool:
    if "://" not in host:
        return True
    return urlsplit(host).scheme.lower() != "http"


def resolve_tls_server_name(config: TrueNASConfig) -> str | None:
    if not host_uses_tls(config.host):
        return None

    override = str(config.tls_server_name or "").strip()
    if override:
        return override

    parsed = urlsplit(normalize_tls_target(config.host))
    return parsed.hostname


def build_tls_client_context(config: TrueNASConfig) -> ssl.SSLContext | None:
    if not host_uses_tls(config.host):
        return None

    context = ssl.create_default_context()
    if config.verify_ssl:
        if config.tls_ca_bundle_path:
            context.load_verify_locations(cafile=config.tls_ca_bundle_path)
            if hasattr(ssl, "VERIFY_X509_PARTIAL_CHAIN"):
                context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
        return context

    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


class _HostnameOverrideHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, *args, server_hostname: str | None = None, **kwargs) -> None:
        super().__init__(host, *args, **kwargs)
        self._server_hostname_override = server_hostname

    def connect(self) -> None:
        http.client.HTTPConnection.connect(self)

        if self._tunnel_host:
            server_hostname = self._tunnel_host
        else:
            server_hostname = self._server_hostname_override or self.host

        self.sock = self._context.wrap_socket(self.sock, server_hostname=server_hostname)


class _HostnameOverrideHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(
        self,
        *,
        context: ssl.SSLContext | None = None,
        check_hostname: bool | None = None,
        server_hostname: str | None = None,
    ) -> None:
        super().__init__(context=context, check_hostname=check_hostname)
        self._server_hostname = server_hostname

    def https_open(self, req):  # type: ignore[override]
        return self.do_open(
            lambda host, **kwargs: _HostnameOverrideHTTPSConnection(
                host,
                server_hostname=self._server_hostname,
                **kwargs,
            ),
            req,
        )


def urlopen_with_tls_config(
    request: urllib.request.Request,
    *,
    timeout: float,
    context: ssl.SSLContext | None,
    server_hostname: str | None = None,
):
    request_host = urlsplit(request.full_url).hostname
    normalized_server_hostname = str(server_hostname or "").strip()

    if (
        context is None
        or not normalized_server_hostname
        or not context.check_hostname
        or (
            request_host is not None
            and normalized_server_hostname.lower() == request_host.lower()
        )
    ):
        return urllib.request.urlopen(request, timeout=timeout, context=context)

    opener = urllib.request.build_opener(
        _HostnameOverrideHTTPSHandler(
            context=context,
            check_hostname=context.check_hostname,
            server_hostname=normalized_server_hostname,
        )
    )
    return opener.open(request, timeout=timeout)
