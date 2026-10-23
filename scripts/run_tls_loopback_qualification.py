"""Qualify frozen-source TLS behavior using only fresh loopback endpoints."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import socket
import ssl
import stat
import tempfile
import threading


def _handle_tls_connection(connection, context, *, allow_invalid_leaf, handshakes, failures):
    started = completed = False
    try:
        with connection:
            connection.settimeout(2)
            started = True
            with context.wrap_socket(connection, server_side=True):
                completed = True
    except ssl.SSLError as error:
        expected_alerts = {
            'SSLV3_ALERT_CERTIFICATE_EXPIRED', 'SSLV3_ALERT_BAD_CERTIFICATE',
            'SSLV3_ALERT_CERTIFICATE_UNKNOWN',
        }
        if not (started and not completed and allow_invalid_leaf
                and getattr(error, 'reason', None) in expected_alerts):
            failures.append(type(error).__name__)
    except Exception as error:
        failures.append(type(error).__name__)
    else:
        handshakes.append(True)


@contextmanager
def tls_endpoint(certificate: Path, key: Path, *, allow_invalid_leaf: bool = False):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.num_tickets = 0
    context.load_cert_chain(certificate, key)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(('127.0.0.1', 0))
    listener.listen(4)
    listener.settimeout(0.2)
    port = listener.getsockname()[1]
    stop = threading.Event()
    failures: list[str] = []
    handshakes: list[bool] = []

    def serve():
        while not stop.is_set():
            try:
                connection, address = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if not stop.is_set():
                    failures.append('listener_error')
                break
            if address[0] != '127.0.0.1':
                connection.close()
                failures.append('non_loopback_peer')
                continue
            _handle_tls_connection(
                connection, context, allow_invalid_leaf=allow_invalid_leaf,
                handshakes=handshakes, failures=failures,
            )

    thread = threading.Thread(target=serve, name='synthetic-loopback-tls')
    thread.start()
    try:
        yield f'https://127.0.0.1:{port}', handshakes
    finally:
        stop.set()
        thread.join(timeout=4)
        listener.close()
        thread.join(timeout=1)
        if thread.is_alive() or failures:
            raise RuntimeError('Loopback TLS endpoint did not cleanly stop')
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            if probe.connect_ex(('127.0.0.1', port)) == 0:
                raise RuntimeError('Loopback TLS port remains open')


def make_certificate(directory: Path, window: str) -> tuple[Path, Path]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    now = datetime.now(timezone.utc)
    windows = {
        'expired': (now - timedelta(days=40), now - timedelta(days=5)),
        'future': (now + timedelta(days=5), now + timedelta(days=40)),
        'valid': (now - timedelta(days=1), now + timedelta(days=5)),
    }
    start, end = windows[window]
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
    certificate = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(private_key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(start).not_valid_after(end)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost')]), critical=False)
        .sign(private_key, hashes.SHA256())
    )
    cert = directory / 'synthetic.pem'
    key = directory / 'synthetic-key.pem'
    cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key.write_bytes(private_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    cert.chmod(0o600)
    key.chmod(0o600)
    return cert, key


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scratch', type=Path, required=True)
    parser.add_argument('--source-sha', required=True)
    parser.add_argument('--tls-source-sha256', required=True)
    parser.add_argument('--ack', required=True)
    args = parser.parse_args()
    if args.ack != 'I_APPROVE_SYNTHETIC_TLS_LOOPBACK':
        raise ValueError('Explicit synthetic-loopback acknowledgement required')
    if not re.fullmatch(r'[a-f0-9]{40}', args.source_sha):
        raise ValueError('Exact source SHA required')
    if not re.fullmatch(r'[a-f0-9]{64}', args.tls_source_sha256):
        raise ValueError('Exact TLS module SHA-256 required')
    metadata = args.scratch.stat(follow_symlinks=False)
    if (not stat.S_ISDIR(metadata.st_mode) or args.scratch.is_symlink()
            or metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077):
        raise ValueError('Scratch must be a private, task-owned real directory')
    if os.geteuid() == 0:
        raise ValueError('This qualification must run as the image nonroot user')
    service_module = importlib.import_module('admin_service.services.tls_trust')
    module_path = service_module.__file__
    if module_path is None:
        raise ValueError('Runtime TLS source path is unavailable')
    actual_hash = hashlib.sha256(Path(module_path).read_bytes()).hexdigest()
    if actual_hash != args.tls_source_sha256:
        raise ValueError('Runtime TLS source differs from admitted frozen source')
    results = []
    with tempfile.TemporaryDirectory(prefix='tls-qualification-', dir=args.scratch) as temporary:
        root = Path(temporary)
        for window in ('expired', 'future', 'valid'):
            case = root / window
            case.mkdir(mode=0o700)
            config = case / 'config.yaml'
            config.write_text('systems: []\n', encoding='utf-8')
            cert, key = make_certificate(case, window)
            service = service_module.TLSTrustStoreService(str(config))
            with tls_endpoint(cert, key, allow_invalid_leaf=window != 'valid') as (url, handshakes):
                inspection = service.inspect_remote_certificate(
                    url, timeout_seconds=3, tls_server_name='localhost',
                )
                leaf = inspection['leaf']
                if leaf['expired'] is not (window == 'expired'):
                    raise RuntimeError('Wrong actual expired-leaf classification')
                if leaf['not_yet_valid'] is not (window == 'future'):
                    raise RuntimeError('Wrong actual future-leaf classification')
                if window != 'valid':
                    try:
                        service.trust_remote_certificate(
                            url, timeout_seconds=3, bundle_name='synthetic',
                            tls_server_name='localhost',
                        )
                    except ValueError as error:
                        wording = 'expired on' if window == 'expired' else 'not valid until'
                        if wording not in str(error):
                            raise RuntimeError('Invalid leaf refused for the wrong reason') from error
                    else:
                        raise RuntimeError('Invalid leaf unexpectedly pinned')
                    if service.trust_dir.exists():
                        raise RuntimeError('Invalid leaf wrote trust storage')
                    validation = service.validate_bundle_for_host(
                        url, str(cert), timeout_seconds=3, tls_server_name='localhost',
                    )
                    reason = 'expired' if window == 'expired' else 'not yet valid'
                    if (validation['validated'] is not False
                            or reason not in str(validation.get('detail', '')).lower()):
                        raise RuntimeError('Invalid-leaf TLS refusal was not its validity failure')
                else:
                    trusted = service.trust_remote_certificate(
                        url, timeout_seconds=3, bundle_name='synthetic',
                        tls_server_name='localhost',
                    )
                    if trusted['validation']['validated'] is not True:
                        raise RuntimeError('Valid positive-control TLS verification failed')
                    bundle = Path(trusted['bundle_path'])
                    if bundle.parent != case / 'tls' or not bundle.is_file():
                        raise RuntimeError('Valid bundle escaped its private test directory')
                    # Reinstantiate the real service to prove persisted bundle reuse.
                    reloaded = service_module.TLSTrustStoreService(str(config))
                    if reloaded.validate_bundle_for_host(
                        url, str(bundle), timeout_seconds=3, tls_server_name='localhost',
                    )['validated'] is not True:
                        raise RuntimeError('Saved valid bundle failed after reload')
                if len(handshakes) < 2:
                    raise RuntimeError('Expected real loopback TLS handshakes did not occur')
            results.append({'window': window, 'result': 'pass', 'endpoint_stopped': True})
        if root.is_symlink() or root.parent != args.scratch.resolve():
            raise RuntimeError('Synthetic scratch ownership changed')
    if any(args.scratch.iterdir()):
        raise RuntimeError('Synthetic TLS fixture cleanup incomplete')
    print(json.dumps({
        'qualification': 'tls-loopback', 'result': 'pass', 'source': args.source_sha,
        'tls_source_sha256': actual_hash, 'uid': os.geteuid(), 'cases': results,
        'socket_mocking': False, 'live_core': False, 'accounts_or_grants_changed': False,
        'fixture_cleanup': 'pass', 'release_gate_cleared': False,
    }, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
