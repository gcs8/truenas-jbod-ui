from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]


class _Handler(BaseHTTPRequestHandler):
    payload = b""

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] != "/truenas-jbod-ui/":
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        del format, args
        return


class PublicDemoDeploymentCheckTests(unittest.TestCase):
    def run_checker(self, url: str, artifact: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "scripts/check_public_demo_deployment.py",
                "--url",
                url,
                "--artifact",
                str(artifact),
                "--max-attempts",
                "1",
                "--timeout-seconds",
                "3",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_exact_published_bytes_pass_at_pages_subpath(self) -> None:
        payload = (ROOT / "public-demo/index.html").read_bytes()
        _Handler.payload = payload
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = self.run_checker(
                f"http://127.0.0.1:{server.server_port}/truenas-jbod-ui/",
                ROOT / "public-demo/index.html",
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("exact byte match", result.stdout)
        self.assertIn("Source revision", result.stdout)

    def test_changed_published_bytes_fail_closed(self) -> None:
        payload = (ROOT / "public-demo/index.html").read_bytes()
        _Handler.payload = payload + b"\n"
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = self.run_checker(
                f"http://127.0.0.1:{server.server_port}/truenas-jbod-ui/",
                ROOT / "public-demo/index.html",
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("published bytes do not match", result.stderr)

    def test_non_html_or_oversized_artifact_is_rejected_before_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            artifact = Path(temp_dir) / "index.html"
            artifact.write_bytes(b"x" * 1025)
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/check_public_demo_deployment.py",
                    "--url",
                    "http://127.0.0.1:1/truenas-jbod-ui/",
                    "--artifact",
                    str(artifact),
                    "--max-bytes",
                    "1024",
                    "--max-attempts",
                    "1",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("artifact exceeds byte limit", result.stderr)


if __name__ == "__main__":
    unittest.main()
