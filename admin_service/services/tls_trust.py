from __future__ import annotations

import hashlib
import re
import socket
import ssl
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization

from app.config import TrueNASConfig
from app.services.tls_context import build_tls_client_context, resolve_tls_server_name


PEM_CERT_PATTERN = re.compile(
    r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
    re.DOTALL,
)


class TLSTrustStoreService:
    def __init__(self, config_path: str) -> None:
        self.config_path = Path(config_path)
        self.trust_dir = self.config_path.parent / "tls"

    def inspect_remote_certificate(
        self,
        host: str,
        timeout_seconds: int = 10,
        *,
        tls_server_name: str | None = None,
    ) -> dict[str, Any]:
        normalized_url, connect_host, port = self._normalize_target(host)
        negotiated_server_name = str(tls_server_name or "").strip() or connect_host
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

        with socket.create_connection((connect_host, port), timeout=timeout_seconds) as raw_socket:
            with context.wrap_socket(raw_socket, server_hostname=negotiated_server_name) as tls_socket:
                chain_getter = getattr(tls_socket, "get_unverified_chain", None)
                chain_der = list(chain_getter() or []) if callable(chain_getter) else []
                if not chain_der:
                    leaf_der = tls_socket.getpeercert(binary_form=True)
                    if leaf_der:
                        chain_der = [leaf_der]

        if not chain_der:
            raise ValueError("The remote host did not present a TLS certificate chain.")

        certificates = [x509.load_der_x509_certificate(der_bytes) for der_bytes in chain_der]
        pem_chain = "".join(ssl.DER_cert_to_PEM_cert(der_bytes) for der_bytes in chain_der)
        return {
            "host": normalized_url,
            "connect_host": connect_host,
            "server_hostname": negotiated_server_name,
            "port": port,
            "certificate_count": len(certificates),
            "leaf": self._serialize_certificate(certificates[0]),
            "chain": [self._serialize_certificate(cert) for cert in certificates],
            "pem_chain": pem_chain,
            "suggested_bundle_name": self._suggest_bundle_name(host=host),
        }

    def import_pem_bundle(
        self,
        pem_text: str,
        *,
        bundle_name: str | None = None,
        system_id: str | None = None,
        host: str | None = None,
    ) -> dict[str, Any]:
        certificates = self._parse_pem_certificates(pem_text)
        normalized_name = self._suggest_bundle_name(
            bundle_name=bundle_name,
            system_id=system_id,
            host=host,
        )
        bundle_path = self.trust_dir / f"{normalized_name}.pem"
        bundle_payload = "".join(
            cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
            for cert in certificates
        )
        self._write_text_atomic(bundle_path, bundle_payload)
        return {
            "bundle_name": normalized_name,
            "bundle_path": str(bundle_path),
            "certificate_count": len(certificates),
            "subjects": [cert.subject.rfc4514_string() for cert in certificates],
        }

    def trust_remote_certificate(
        self,
        host: str,
        *,
        timeout_seconds: int = 10,
        bundle_name: str | None = None,
        system_id: str | None = None,
        tls_server_name: str | None = None,
    ) -> dict[str, Any]:
        inspection = self.inspect_remote_certificate(
            host,
            timeout_seconds=timeout_seconds,
            tls_server_name=tls_server_name,
        )
        imported = self.import_pem_bundle(
            inspection["pem_chain"],
            bundle_name=bundle_name,
            system_id=system_id,
            host=host,
        )
        validation = self.validate_bundle_for_host(
            host,
            imported["bundle_path"],
            timeout_seconds=timeout_seconds,
            tls_server_name=tls_server_name,
        )
        return {
            **imported,
            "inspection": inspection,
            "validation": validation,
        }

    def validate_bundle_for_host(
        self,
        host: str,
        bundle_path: str,
        *,
        timeout_seconds: int = 10,
        tls_server_name: str | None = None,
    ) -> dict[str, Any]:
        normalized_url, connect_host, port = self._normalize_target(host)
        bundle_file = Path(str(bundle_path or "").strip())
        if not bundle_file.exists():
            raise ValueError("The saved TLS bundle could not be found for validation.")

        config = TrueNASConfig(
            host=normalized_url,
            verify_ssl=True,
            tls_ca_bundle_path=str(bundle_file),
            tls_server_name=tls_server_name,
        )
        context = build_tls_client_context(
            config
        )
        if context is None:
            raise ValueError("TLS validation only supports HTTPS targets.")
        server_hostname = resolve_tls_server_name(config) or connect_host

        try:
            with socket.create_connection((connect_host, port), timeout=timeout_seconds) as raw_socket:
                with context.wrap_socket(raw_socket, server_hostname=server_hostname):
                    pass
        except ssl.SSLError as exc:
            mismatch_context = (
                f" while connecting to {connect_host} and verifying the certificate as {server_hostname}"
                if server_hostname != connect_host
                else ""
            )
            return {
                "validated": False,
                "host": normalized_url,
                "connect_host": connect_host,
                "server_hostname": server_hostname,
                "port": port,
                "bundle_path": str(bundle_file),
                "detail": f"TLS verification failed with the saved bundle{mismatch_context}: {exc}",
            }
        except OSError as exc:
            return {
                "validated": False,
                "host": normalized_url,
                "connect_host": connect_host,
                "server_hostname": server_hostname,
                "port": port,
                "bundle_path": str(bundle_file),
                "detail": f"Unable to reach the host for TLS validation: {exc}",
            }

        return {
            "validated": True,
            "host": normalized_url,
            "connect_host": connect_host,
            "server_hostname": server_hostname,
            "port": port,
            "bundle_path": str(bundle_file),
            "detail": (
                f"Verified TLS certificate checks for {normalized_url} while verifying the certificate as {server_hostname} using {bundle_file}."
                if server_hostname != connect_host
                else f"Verified TLS certificate checks for {normalized_url} using {bundle_file}."
            ),
        }

    @staticmethod
    def _normalize_target(host: str) -> tuple[str, str, int]:
        raw_value = str(host or "").strip()
        if not raw_value:
            raise ValueError("A TLS host is required.")

        if "://" not in raw_value:
            raw_value = f"https://{raw_value}"
        parsed = urlsplit(raw_value)
        if parsed.scheme.lower() != "https":
            raise ValueError("TLS inspection only supports HTTPS targets.")
        if not parsed.hostname:
            raise ValueError("A valid HTTPS host is required for TLS inspection.")

        normalized_url = urlunsplit(
            ("https", parsed.netloc, parsed.path or "", parsed.query, parsed.fragment)
        )
        return normalized_url, parsed.hostname, parsed.port or 443

    @staticmethod
    def _serialize_certificate(certificate: x509.Certificate) -> dict[str, Any]:
        san_dns: list[str] = []
        san_ip: list[str] = []
        try:
            san_extension = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName)
            san_dns = list(san_extension.value.get_values_for_type(x509.DNSName))
            san_ip = [str(value) for value in san_extension.value.get_values_for_type(x509.IPAddress)]
        except x509.ExtensionNotFound:
            pass

        public_key_bytes = certificate.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return {
            "subject": certificate.subject.rfc4514_string(),
            "issuer": certificate.issuer.rfc4514_string(),
            "serial_number": format(certificate.serial_number, "X"),
            "not_valid_before": certificate.not_valid_before_utc.isoformat(),
            "not_valid_after": certificate.not_valid_after_utc.isoformat(),
            "is_ca": TLSTrustStoreService._certificate_is_ca(certificate),
            "sha256_fingerprint": TLSTrustStoreService._format_digest(certificate.fingerprint(hashes.SHA256())),
            "sha1_fingerprint": TLSTrustStoreService._format_digest(certificate.fingerprint(hashes.SHA1())),
            "spki_sha256": hashlib.sha256(public_key_bytes).hexdigest().upper(),
            "san_dns": san_dns,
            "san_ip": san_ip,
        }

    @staticmethod
    def _certificate_is_ca(certificate: x509.Certificate) -> bool:
        try:
            return bool(
                certificate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
            )
        except x509.ExtensionNotFound:
            return False

    @staticmethod
    def _format_digest(digest: bytes) -> str:
        hex_text = digest.hex().upper()
        return ":".join(hex_text[index : index + 2] for index in range(0, len(hex_text), 2))

    @staticmethod
    def _parse_pem_certificates(pem_text: str) -> list[x509.Certificate]:
        matches = PEM_CERT_PATTERN.findall(str(pem_text or ""))
        if not matches:
            raise ValueError("No PEM certificates were found in the uploaded bundle.")
        certificates: list[x509.Certificate] = []
        for pem_block in matches:
            try:
                certificates.append(x509.load_pem_x509_certificate(pem_block.encode("utf-8")))
            except ValueError as exc:
                raise ValueError("The uploaded certificate bundle was not valid PEM.") from exc
        return certificates

    @staticmethod
    def _suggest_bundle_name(
        *,
        bundle_name: str | None = None,
        system_id: str | None = None,
        host: str | None = None,
    ) -> str:
        for candidate in (bundle_name, system_id, host):
            cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(candidate or "").strip()).strip(".-_")
            if cleaned:
                return cleaned[:128].lower()
        return "trusted-remote"

    def _write_text_atomic(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(f"{path.suffix}.tmp")
        temp_path.write_text(content, encoding="utf-8", newline="\n")
        temp_path.replace(path)
