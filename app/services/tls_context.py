from __future__ import annotations

import http.client
import io
import ssl
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from app.config import TrueNASConfig

_DEFAULT_PORTS = {"http": 80, "https": 443}
CROSS_ORIGIN_REDIRECT_REASON = "redirect to a different origin was refused"


def _url_origin(url: str) -> tuple[str, str, int | None] | None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    if not scheme or not hostname:
        return None
    return scheme, hostname, port if port is not None else _DEFAULT_PORTS.get(scheme)


def redirect_keeps_origin(source_url: str, target_url: str) -> bool:
    """Whether a redirect keeps the request's host and does not weaken its transport.

    The scheme, host and port must match. The one exception is an HTTP to
    HTTPS upgrade on the same host and the default HTTPS port.
    """
    source = _url_origin(source_url)
    target = _url_origin(target_url)
    if source is None or target is None or source[1] != target[1]:
        return False
    if source == target:
        return True
    return source[0] == "http" and target[0] == "https" and target[2] == _DEFAULT_PORTS["https"]


class SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only when it keeps the origin and transport (#737).

    The stock handler copies request headers, including `Authorization`, to
    whatever `Location` names, so an endpoint could forward configured
    credentials to another host or downgrade them from HTTPS to HTTP.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        if not redirect_keeps_origin(req.full_url, newurl):
            if fp is not None:
                fp.close()
            raise urllib.error.HTTPError(
                req.full_url,
                code,
                CROSS_ORIGIN_REDIRECT_REASON,
                headers,
                io.BytesIO(b""),
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


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


class TlsTrustConfigurationError(RuntimeError):
    """The configured CA bundle cannot be used, so nothing can be verified.

    #537: this used to surface as a bare `OSError` (ENOENT for a missing
    bundle), which callers that also handle storage failures reported as an
    unusable application data directory, sending the operator to the wrong
    path. It is deliberately not an `OSError`.
    """

    def __init__(self, bundle_path: str, cause: BaseException) -> None:
        self.bundle_path = bundle_path
        super().__init__(
            f"The configured TLS CA bundle {bundle_path} could not be loaded: {cause}"
        )


def certificate_verification_failure_reason(exc: BaseException) -> str | None:
    """Return the verifier's reason when `exc` was caused by a failed certificate check.

    #954: the inventory reported an expired or mismatched certificate as an
    unreachable API. The SSL error can sit directly on the exception, in its
    `__cause__`/`__context__` chain, or in a `URLError.reason`.
    """
    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, ssl.SSLCertVerificationError):
            reason = " ".join(str(getattr(current, "verify_message", None) or "").split())
            return reason[:200] or "the certificate could not be verified"
        for linked in (getattr(current, "reason", None), current.__cause__, current.__context__):
            if isinstance(linked, BaseException):
                pending.append(linked)
    return None


def build_tls_client_context(config: TrueNASConfig) -> ssl.SSLContext | None:
    if not host_uses_tls(config.host):
        return None

    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    if config.verify_ssl:
        if config.tls_ca_bundle_path:
            try:
                context.load_verify_locations(cafile=config.tls_ca_bundle_path)
            except OSError as exc:
                # Includes ssl.SSLError for a malformed bundle. Verification
                # was asked for, so this fails rather than falling back.
                raise TlsTrustConfigurationError(config.tls_ca_bundle_path, exc) from exc
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
        https_handler: urllib.request.HTTPSHandler = urllib.request.HTTPSHandler(context=context)
    else:
        https_handler = _HostnameOverrideHTTPSHandler(
            context=context,
            check_hostname=context.check_hostname,
            server_hostname=normalized_server_hostname,
        )

    # Credential-carrying requests follow same-origin redirects only (#737).
    opener = urllib.request.build_opener(https_handler, SameOriginRedirectHandler())
    return opener.open(request, timeout=timeout)
