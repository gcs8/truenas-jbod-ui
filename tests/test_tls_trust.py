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


def build_self_signed_pem(
    common_name: str,
    *,
    not_valid_before: datetime | None = None,
    not_valid_after: datetime | None = None,
) -> str:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_valid_before or now - timedelta(days=1))
        .not_valid_after(not_valid_after or now + timedelta(days=30))
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
    @patch("admin_service.services.tls_trust.ssl.SSLContext")
    def test_inspect_remote_certificate_enforces_modern_tls_floor(
        self,
        ssl_context_cls: MagicMock,
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
        context = MagicMock()
        context.wrap_socket.return_value = tls_socket
        ssl_context_cls.return_value = context

        self.service.inspect_remote_certificate("https://archive-core.local", timeout_seconds=5)

        ssl_context_cls.assert_called_once_with(ssl.PROTOCOL_TLS_CLIENT)
        self.assertEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)

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


def _der_from_pem(pem_text: str) -> bytes:
    return x509.load_pem_x509_certificate(pem_text.encode("utf-8")).public_bytes(serialization.Encoding.DER)


class TLSCertificateValidityWindowTests(unittest.TestCase):
    """#954: an expired or not-yet-valid leaf is flagged, and trust refuses it."""

    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp())
        self.config_path = self.temp_dir / "config.yaml"
        self.config_path.write_text("systems: []\n", encoding="utf-8")
        self.service = TLSTrustStoreService(str(self.config_path))
        now = datetime.now(timezone.utc)
        self.expired_pem = build_self_signed_pem(
            "truenas.example.test",
            not_valid_before=now - timedelta(days=400),
            not_valid_after=now - timedelta(days=35),
        )
        self.future_pem = build_self_signed_pem(
            "truenas.example.test",
            not_valid_before=now + timedelta(days=20),
            not_valid_after=now + timedelta(days=400),
        )
        self.current_pem = build_self_signed_pem("truenas.example.test")

    def _inspect(self, pem_text: str) -> dict:
        with patch("admin_service.services.tls_trust.socket.create_connection") as create_connection, patch.object(
            ssl.SSLContext, "wrap_socket"
        ) as wrap_socket:
            raw_socket = MagicMock()
            raw_socket.__enter__.return_value = raw_socket
            raw_socket.__exit__.return_value = False
            create_connection.return_value = raw_socket
            tls_socket = MagicMock()
            tls_socket.__enter__.return_value = tls_socket
            tls_socket.__exit__.return_value = False
            tls_socket.get_unverified_chain.return_value = [_der_from_pem(pem_text)]
            wrap_socket.return_value = tls_socket
            return self.service.inspect_remote_certificate("https://truenas.example.test", timeout_seconds=5)

    def test_inspection_flags_an_expired_leaf(self) -> None:
        leaf = self._inspect(self.expired_pem)["leaf"]

        self.assertIs(leaf["expired"], True)
        self.assertIs(leaf["not_yet_valid"], False)
        self.assertTrue(leaf["not_valid_before"])
        self.assertTrue(leaf["not_valid_after"])

    def test_inspection_flags_a_not_yet_valid_leaf(self) -> None:
        leaf = self._inspect(self.future_pem)["leaf"]

        self.assertIs(leaf["expired"], False)
        self.assertIs(leaf["not_yet_valid"], True)

    def test_inspection_reports_a_current_leaf_as_valid(self) -> None:
        leaf = self._inspect(self.current_pem)["leaf"]

        self.assertIs(leaf["expired"], False)
        self.assertIs(leaf["not_yet_valid"], False)

    def test_trust_refuses_an_expired_leaf_without_saving_it(self) -> None:
        with patch.object(TLSTrustStoreService, "validate_bundle_for_host") as validate_bundle_for_host:
            with patch("admin_service.services.tls_trust.socket.create_connection") as create_connection, patch.object(
                ssl.SSLContext, "wrap_socket"
            ) as wrap_socket:
                raw_socket = MagicMock()
                raw_socket.__enter__.return_value = raw_socket
                raw_socket.__exit__.return_value = False
                create_connection.return_value = raw_socket
                tls_socket = MagicMock()
                tls_socket.__enter__.return_value = tls_socket
                tls_socket.__exit__.return_value = False
                tls_socket.get_unverified_chain.return_value = [_der_from_pem(self.expired_pem)]
                wrap_socket.return_value = tls_socket
                with self.assertRaises(ValueError) as raised:
                    self.service.trust_remote_certificate(
                        "https://truenas.example.test",
                        timeout_seconds=5,
                        system_id="synthetic-core",
                    )

        message = str(raised.exception)
        self.assertIn("expired on", message)
        self.assertIn("Renew it on the server", message)
        validate_bundle_for_host.assert_not_called()
        self.assertFalse((self.temp_dir / "tls").exists(), "an expired leaf must not be written to the trust store")

    def test_trust_refuses_a_not_yet_valid_leaf_without_saving_it(self) -> None:
        with patch.object(TLSTrustStoreService, "inspect_remote_certificate") as inspect_remote_certificate, patch.object(
            TLSTrustStoreService, "import_pem_bundle"
        ) as import_pem_bundle:
            certificate = x509.load_pem_x509_certificate(self.future_pem.encode("utf-8"))
            inspect_remote_certificate.return_value = {
                "host": "https://truenas.example.test",
                "pem_chain": self.future_pem,
                "leaf": TLSTrustStoreService._serialize_certificate(certificate),
            }
            with self.assertRaises(ValueError) as raised:
                self.service.trust_remote_certificate("https://truenas.example.test")

        self.assertIn("not valid until", str(raised.exception))
        import_pem_bundle.assert_not_called()

    @patch("admin_service.services.tls_trust.socket.create_connection")
    @patch("admin_service.services.tls_trust.build_tls_client_context")
    def test_validate_bundle_reports_an_unloadable_bundle_as_a_failed_check(
        self,
        build_tls_client_context_mock: MagicMock,
        create_connection: MagicMock,
    ) -> None:
        from app.services.tls_context import TlsTrustConfigurationError

        bundle_path = self.temp_dir / "tls" / "synthetic-core.pem"
        bundle_path.parent.mkdir(parents=True, exist_ok=True)
        bundle_path.write_text("not a certificate\n", encoding="utf-8")
        build_tls_client_context_mock.side_effect = TlsTrustConfigurationError(
            str(bundle_path), ssl.SSLError("synthetic malformed bundle")
        )

        result = self.service.validate_bundle_for_host("https://truenas.example.test", str(bundle_path))

        self.assertFalse(result["validated"])
        self.assertIn("could not be loaded", result["detail"])
        create_connection.assert_not_called()


class SavedTLSBundleStatusRouteTests(unittest.TestCase):
    """#954: admin re-checks a saved bundle on load and returns the real failure reason."""

    def setUp(self) -> None:
        import tests.admin_test_env  # noqa: F401  (must precede admin_service.main)
        from admin_service.main import app as admin_app

        self.route = next(
            route for route in admin_app.routes if getattr(route, "path", None) == "/api/admin/tls/saved-status"
        )
        self.temp_dir = Path(tempfile.mkdtemp())
        self.bundle_path = self.temp_dir / "tls" / "synthetic-core.pem"
        self.bundle_path.parent.mkdir(parents=True, exist_ok=True)
        self.bundle_path.write_text(build_self_signed_pem("truenas.example.test"), encoding="utf-8")

    def _settings(self, **truenas: object):
        from app.config import Settings, SystemConfig

        values = {
            "host": "https://truenas.example.test",
            "verify_ssl": True,
            "tls_ca_bundle_path": str(self.bundle_path),
            "tls_server_name": None,
            **truenas,
        }
        return Settings(
            config_file=str(self.temp_dir / "config.yaml"),
            systems=[SystemConfig(id="synthetic-core", truenas=TrueNASConfig(**values))],
        )

    def _call(self, settings, system_id: str = "synthetic-core") -> dict:
        import asyncio
        import json

        with patch("admin_service.routes.reload_app_settings", return_value=settings):
            response = asyncio.run(self.route.endpoint(system_id=system_id))
        return json.loads(response.body)

    def test_saved_bundle_is_revalidated_and_the_failure_reason_is_returned(self) -> None:
        failure = {
            "validated": False,
            "host": "https://truenas.example.test",
            "bundle_path": str(self.bundle_path),
            "detail": "TLS verification failed with the saved bundle: certificate has expired",
        }
        with patch.object(TLSTrustStoreService, "validate_bundle_for_host", return_value=failure) as validate:
            payload = self._call(self._settings(tls_server_name="truenas.example.test"))

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["system_id"], "synthetic-core")
        self.assertEqual(payload["validation"], failure)
        validate.assert_called_once()
        args, kwargs = validate.call_args
        self.assertEqual(args, ("https://truenas.example.test", str(self.bundle_path)))
        self.assertEqual(kwargs["tls_server_name"], "truenas.example.test")
        self.assertLessEqual(kwargs["timeout_seconds"], 10)

    def test_no_probe_when_verification_is_off_or_no_bundle_is_saved(self) -> None:
        for overrides in ({"verify_ssl": False}, {"tls_ca_bundle_path": None}, {"host": "http://truenas.example.test"}):
            with self.subTest(overrides=overrides):
                with patch.object(TLSTrustStoreService, "validate_bundle_for_host") as validate:
                    payload = self._call(self._settings(**overrides))
                self.assertTrue(payload["ok"])
                self.assertIsNone(payload["validation"])
                validate.assert_not_called()

    def test_missing_saved_bundle_is_reported_as_a_failed_check(self) -> None:
        missing = self.temp_dir / "tls" / "absent.pem"

        payload = self._call(self._settings(tls_ca_bundle_path=str(missing)))

        self.assertFalse(payload["validation"]["validated"])
        self.assertIn("could not be found", payload["validation"]["detail"])

    def test_a_validation_error_is_reported_without_echoing_exception_text(self) -> None:
        with patch.object(
            TLSTrustStoreService,
            "validate_bundle_for_host",
            side_effect=ValueError("synthetic internal detail /synthetic/private/path"),
        ):
            payload = self._call(self._settings())

        self.assertFalse(payload["validation"]["validated"])
        self.assertNotIn("synthetic internal detail", payload["validation"]["detail"])
        self.assertIn("could not be checked", payload["validation"]["detail"])

    def test_unknown_system_is_a_404(self) -> None:
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as raised:
            self._call(self._settings(), system_id="not-saved")
        self.assertEqual(raised.exception.status_code, 404)


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
        self.assertEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)

    def test_build_tls_client_context_names_an_unreadable_ca_bundle(self) -> None:
        # #537: an unreadable bundle raised a bare ENOENT OSError, which the
        # SMART batch reported as an unusable application data directory.
        from app.services.tls_context import TlsTrustConfigurationError

        with tempfile.TemporaryDirectory() as raw:
            missing = Path(raw) / "absent-ca.pem"
            with self.assertRaises(TlsTrustConfigurationError) as raised:
                build_tls_client_context(
                    TrueNASConfig(
                        host="https://tls.invalid",
                        api_key="synthetic-ephemeral-token",
                        verify_ssl=True,
                        tls_ca_bundle_path=str(missing),
                    )
                )

        self.assertIn(str(missing), str(raised.exception))
        self.assertNotIsInstance(raised.exception, OSError)

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
        self.assertEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)

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


class LoopbackQualificationServerTests(unittest.TestCase):
    """Control-only faults, without opening a socket or generating a certificate."""

    def _run_connection(self, *, allowed=False, handshake_error=None, close_error=None):
        from scripts.run_tls_loopback_qualification import _handle_tls_connection

        connection = MagicMock()
        context = MagicMock()
        stream = context.wrap_socket.return_value
        stream.__enter__.return_value = stream
        stream.__exit__.return_value = False
        if handshake_error is not None:
            context.wrap_socket.side_effect = handshake_error
        if close_error is not None:
            stream.__exit__.side_effect = close_error
        handshakes, failures = [], []
        _handle_tls_connection(connection, context, allow_invalid_leaf=allowed,
                               handshakes=handshakes, failures=failures)
        return handshakes, failures, stream

    @staticmethod
    def _ssl_error(reason):
        error = ssl.SSLError(1, 'synthetic server fault')
        error.reason = reason
        return error

    def test_valid_control_needs_no_application_receive(self):
        handshakes, failures, stream = self._run_connection()
        self.assertEqual(handshakes, [True])
        self.assertEqual(failures, [])
        stream.recv.assert_not_called()

    def test_expected_invalid_leaf_alert_is_tolerated_only_in_negative_cases(self):
        for reason in ('SSLV3_ALERT_CERTIFICATE_EXPIRED', 'SSLV3_ALERT_BAD_CERTIFICATE',
                       'SSLV3_ALERT_CERTIFICATE_UNKNOWN'):
            with self.subTest(reason=reason):
                handshakes, failures, _ = self._run_connection(
                    allowed=True, handshake_error=self._ssl_error(reason))
                self.assertEqual(handshakes, [])
                self.assertEqual(failures, [])
                _, failures, _ = self._run_connection(
                    allowed=False, handshake_error=self._ssl_error(reason))
                self.assertTrue(failures)

    def test_unexpected_handshake_ssl_connection_and_timeout_fail(self):
        for error in (self._ssl_error('INTERNAL_ERROR'), ConnectionResetError('synthetic'),
                      TimeoutError('synthetic')):
            with self.subTest(error=type(error).__name__):
                handshakes, failures, _ = self._run_connection(
                    allowed=True, handshake_error=error)
                self.assertEqual(handshakes, [])
                self.assertTrue(failures)

    def test_post_handshake_close_fault_never_receives_invalid_leaf_exemption(self):
        for allowed in (False, True):
            with self.subTest(allowed=allowed):
                handshakes, failures, _ = self._run_connection(
                    allowed=allowed,
                    close_error=self._ssl_error('SSLV3_ALERT_CERTIFICATE_EXPIRED'))
                self.assertEqual(handshakes, [])
                self.assertTrue(failures)
