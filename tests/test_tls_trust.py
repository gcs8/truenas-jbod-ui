from __future__ import annotations

import ssl
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request
from unittest.mock import MagicMock, patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from admin_service.services.tls_trust import TLSTrustStoreService
from app.config import TrueNASConfig
from app.services.tls_context import build_tls_client_context, resolve_tls_server_name, urlopen_with_tls_config


def build_self_signed_pem(common_name: str) -> str:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(common_name)]),
            critical=False,
        )
        .sign(private_key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")


class TLSTrustStoreServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp())
        self.config_path = self.temp_dir / "config.yaml"
        self.config_path.write_text("systems: []\n", encoding="utf-8")
        self.service = TLSTrustStoreService(str(self.config_path))

    def test_import_pem_bundle_writes_bundle_under_tls_directory(self) -> None:
        pem_text = build_self_signed_pem("archive-core.local")

        result = self.service.import_pem_bundle(
            pem_text,
            bundle_name="Archive CORE",
            system_id="archive-core",
            host="https://archive-core.local",
        )

        bundle_path = Path(result["bundle_path"])
        self.assertTrue(bundle_path.exists())
        self.assertEqual(bundle_path.parent, self.temp_dir / "tls")
        self.assertEqual(bundle_path.name, "archive-core.pem")
        self.assertEqual(result["certificate_count"], 1)
        self.assertIn("BEGIN CERTIFICATE", bundle_path.read_text(encoding="utf-8"))

    @patch("admin_service.services.tls_trust.socket.create_connection")
    @patch.object(ssl.SSLContext, "wrap_socket")
    def test_inspect_remote_certificate_returns_presented_fingerprints(
        self,
        wrap_socket: MagicMock,
        create_connection: MagicMock,
    ) -> None:
        pem_text = build_self_signed_pem("archive-core.local")
        certificate = x509.load_pem_x509_certificate(pem_text.encode("utf-8"))
        der_bytes = certificate.public_bytes(serialization.Encoding.DER)

        raw_socket = MagicMock()
        raw_socket.__enter__.return_value = raw_socket
        raw_socket.__exit__.return_value = False
        create_connection.return_value = raw_socket

        tls_socket = MagicMock()
        tls_socket.__enter__.return_value = tls_socket
        tls_socket.__exit__.return_value = False
        tls_socket.get_unverified_chain.return_value = [der_bytes]
        wrap_socket.return_value = tls_socket

        result = self.service.inspect_remote_certificate("https://archive-core.local", timeout_seconds=5)

        self.assertEqual(result["server_hostname"], "archive-core.local")
        self.assertEqual(result["port"], 443)
        self.assertEqual(result["certificate_count"], 1)
        self.assertIn("archive-core.local", result["leaf"]["subject"])
        self.assertTrue(result["leaf"]["sha256_fingerprint"])
        self.assertEqual(result["leaf"]["san_dns"], ["archive-core.local"])

    @patch("admin_service.services.tls_trust.socket.create_connection")
    @patch("admin_service.services.tls_trust.build_tls_client_context")
    def test_validate_bundle_for_host_reports_success(
        self,
        build_tls_client_context_mock: MagicMock,
        create_connection: MagicMock,
    ) -> None:
        bundle_path = self.temp_dir / "tls" / "archive-core.pem"
        bundle_path.parent.mkdir(parents=True, exist_ok=True)
        bundle_path.write_text(build_self_signed_pem("archive-core.local"), encoding="utf-8")

        raw_socket = MagicMock()
        raw_socket.__enter__.return_value = raw_socket
        raw_socket.__exit__.return_value = False
        create_connection.return_value = raw_socket

        tls_socket = MagicMock()
        tls_socket.__enter__.return_value = tls_socket
        tls_socket.__exit__.return_value = False

        context = MagicMock()
        context.wrap_socket.return_value = tls_socket
        build_tls_client_context_mock.return_value = context

        result = self.service.validate_bundle_for_host(
            "https://archive-core.local",
            str(bundle_path),
            timeout_seconds=5,
        )

        self.assertTrue(result["validated"])
        self.assertEqual(result["bundle_path"], str(bundle_path))
        self.assertEqual(result["host"], "https://archive-core.local")
        config = build_tls_client_context_mock.call_args.args[0]
        self.assertEqual(config.host, "https://archive-core.local")
        self.assertEqual(config.tls_ca_bundle_path, str(bundle_path))
        context.wrap_socket.assert_called_once()

    @patch("admin_service.services.tls_trust.socket.create_connection")
    @patch("admin_service.services.tls_trust.build_tls_client_context")
    def test_validate_bundle_for_host_uses_tls_server_name_override(
        self,
        build_tls_client_context_mock: MagicMock,
        create_connection: MagicMock,
    ) -> None:
        bundle_path = self.temp_dir / "tls" / "archive-core.pem"
        bundle_path.parent.mkdir(parents=True, exist_ok=True)
        bundle_path.write_text(build_self_signed_pem("archive-core.local"), encoding="utf-8")

        raw_socket = MagicMock()
        raw_socket.__enter__.return_value = raw_socket
        raw_socket.__exit__.return_value = False
        create_connection.return_value = raw_socket

        tls_socket = MagicMock()
        tls_socket.__enter__.return_value = tls_socket
        tls_socket.__exit__.return_value = False

        context = MagicMock()
        context.wrap_socket.return_value = tls_socket
        build_tls_client_context_mock.return_value = context

        result = self.service.validate_bundle_for_host(
            "https://10.13.37.10",
            str(bundle_path),
            timeout_seconds=5,
            tls_server_name="TrueNAS.gcs8.io",
        )

        self.assertTrue(result["validated"])
        self.assertEqual(result["connect_host"], "10.13.37.10")
        self.assertEqual(result["server_hostname"], "TrueNAS.gcs8.io")
        config = build_tls_client_context_mock.call_args.args[0]
        self.assertEqual(config.host, "https://10.13.37.10")
        self.assertEqual(config.tls_server_name, "TrueNAS.gcs8.io")
        context.wrap_socket.assert_called_once_with(raw_socket, server_hostname="TrueNAS.gcs8.io")

    @patch("admin_service.services.tls_trust.socket.create_connection")
    @patch("admin_service.services.tls_trust.build_tls_client_context")
    def test_validate_bundle_for_host_reports_verification_failure(
        self,
        build_tls_client_context_mock: MagicMock,
        create_connection: MagicMock,
    ) -> None:
        bundle_path = self.temp_dir / "tls" / "archive-core.pem"
        bundle_path.parent.mkdir(parents=True, exist_ok=True)
        bundle_path.write_text(build_self_signed_pem("archive-core.local"), encoding="utf-8")

        raw_socket = MagicMock()
        raw_socket.__enter__.return_value = raw_socket
        raw_socket.__exit__.return_value = False
        create_connection.return_value = raw_socket

        context = MagicMock()
        context.wrap_socket.side_effect = ssl.SSLCertVerificationError("verify failed")
        build_tls_client_context_mock.return_value = context

        result = self.service.validate_bundle_for_host(
            "https://archive-core.local",
            str(bundle_path),
            timeout_seconds=5,
        )

        self.assertFalse(result["validated"])
        self.assertIn("TLS verification failed", result["detail"])

    @patch.object(TLSTrustStoreService, "validate_bundle_for_host")
    @patch.object(TLSTrustStoreService, "import_pem_bundle")
    @patch.object(TLSTrustStoreService, "inspect_remote_certificate")
    def test_trust_remote_certificate_returns_validation_result(
        self,
        inspect_remote_certificate: MagicMock,
        import_pem_bundle: MagicMock,
        validate_bundle_for_host: MagicMock,
    ) -> None:
        inspect_remote_certificate.return_value = {
            "host": "https://archive-core.local",
            "pem_chain": build_self_signed_pem("archive-core.local"),
            "leaf": {"subject": "CN=archive-core.local"},
        }
        import_pem_bundle.return_value = {
            "bundle_name": "archive-core",
            "bundle_path": "/tmp/archive-core.pem",
            "certificate_count": 1,
            "subjects": ["CN=archive-core.local"],
        }
        validate_bundle_for_host.return_value = {
            "validated": True,
            "host": "https://archive-core.local",
            "bundle_path": "/tmp/archive-core.pem",
            "detail": "Validated.",
        }

        result = self.service.trust_remote_certificate("https://archive-core.local", timeout_seconds=7)

        self.assertTrue(result["validation"]["validated"])
        validate_bundle_for_host.assert_called_once_with(
            "https://archive-core.local",
            "/tmp/archive-core.pem",
            timeout_seconds=7,
            tls_server_name=None,
        )


class TLSContextTests(unittest.TestCase):
    def test_resolve_tls_server_name_prefers_override(self) -> None:
        server_name = resolve_tls_server_name(
            TrueNASConfig(
                host="https://10.13.37.10",
                tls_server_name="TrueNAS.gcs8.io",
            )
        )

        self.assertEqual(server_name, "TrueNAS.gcs8.io")

    @patch("app.services.tls_context.ssl.create_default_context")
    def test_build_tls_client_context_loads_custom_bundle_when_present(
        self,
        create_default_context: MagicMock,
    ) -> None:
        context = MagicMock()
        context.verify_flags = 0
        create_default_context.return_value = context

        returned = build_tls_client_context(
            TrueNASConfig(
                host="https://archive-core.local",
                verify_ssl=True,
                tls_ca_bundle_path="/app/config/tls/archive-core.pem",
            )
        )

        self.assertIs(returned, context)
        context.load_verify_locations.assert_called_once_with(cafile="/app/config/tls/archive-core.pem")

    @patch("app.services.tls_context.ssl.create_default_context")
    def test_build_tls_client_context_disables_verification_when_requested(
        self,
        create_default_context: MagicMock,
    ) -> None:
        context = MagicMock()
        create_default_context.return_value = context

        returned = build_tls_client_context(
            TrueNASConfig(
                host="https://archive-core.local",
                verify_ssl=False,
            )
        )

        self.assertIs(returned, context)
        self.assertFalse(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_NONE)

    @patch("app.services.tls_context.urllib.request.build_opener")
    @patch("app.services.tls_context.urllib.request.urlopen")
    def test_urlopen_with_tls_config_uses_plain_urlopen_when_verification_is_disabled(
        self,
        urlopen_mock: MagicMock,
        build_opener_mock: MagicMock,
    ) -> None:
        request = Request("https://10.13.37.40/qstorapi/storageSystemEnum?flags=0")
        context = build_tls_client_context(
            TrueNASConfig(
                host="https://10.13.37.40",
                verify_ssl=False,
            )
        )

        returned = urlopen_with_tls_config(
            request,
            timeout=5,
            context=context,
            server_hostname="TrueNAS.gcs8.io",
        )

        self.assertIs(returned, urlopen_mock.return_value)
        urlopen_mock.assert_called_once_with(request, timeout=5, context=context)
        build_opener_mock.assert_not_called()
