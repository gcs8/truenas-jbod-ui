from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


DEFAULT_MAX_BYTES = 8 * 1024 * 1024


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read back a published public demo and require exact artifact bytes.")
    parser.add_argument("--url", required=True)
    parser.add_argument("--artifact", type=Path, default=Path("public-demo/index.html"))
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--max-attempts", type=int, default=6)
    parser.add_argument("--timeout-seconds", type=float, default=15.0)
    parser.add_argument("--retry-delay-seconds", type=float, default=5.0)
    return parser.parse_args()


def read_bounded(url: str, *, max_bytes: int, timeout_seconds: float) -> bytes:
    request = Request(
        url,
        headers={
            "Accept": "text/html",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "User-Agent": "truenas-jbod-ui-pages-readback/1",
        },
    )
    with urlopen(request, timeout=timeout_seconds) as response:
        if response.status != 200:
            raise ValueError(f"published URL returned HTTP {response.status}")
        content_type = response.headers.get_content_type()
        if content_type != "text/html":
            raise ValueError(f"published URL returned {content_type}, expected text/html")
        declared_length = response.headers.get("Content-Length")
        if declared_length is not None and int(declared_length) > max_bytes:
            raise ValueError("published response exceeds byte limit")
        payload = response.read(max_bytes + 1)
        if len(payload) > max_bytes:
            raise ValueError("published response exceeds byte limit")
        return payload


def validate_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme == "https":
        return
    if parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}:
        return
    raise ValueError("published URL must use HTTPS outside loopback tests")


def main() -> int:
    args = parse_args()
    try:
        validate_url(args.url)
        if args.max_bytes < 1:
            raise ValueError("max bytes must be positive")
        if args.max_attempts < 1:
            raise ValueError("max attempts must be at least one")
        artifact = args.artifact.read_bytes()
        if len(artifact) > args.max_bytes:
            raise ValueError("artifact exceeds byte limit")
        if not artifact.startswith(b"<!-- public-demo-source-parity "):
            raise ValueError("artifact source parity manifest is missing")
        source_revision_match = re.search(rb'"source_revision":"([0-9a-f]{40})"', artifact[:16384])
        if source_revision_match is None:
            raise ValueError("artifact source revision is missing")

        published: bytes | None = None
        last_error: Exception | None = None
        for attempt in range(args.max_attempts):
            try:
                published = read_bounded(
                    args.url,
                    max_bytes=args.max_bytes,
                    timeout_seconds=args.timeout_seconds,
                )
                break
            except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
                last_error = exc
                if attempt + 1 < args.max_attempts:
                    time.sleep(args.retry_delay_seconds)
        if published is None:
            raise ValueError(f"published readback failed after {args.max_attempts} attempt(s): {type(last_error).__name__}")
        if published != artifact:
            raise ValueError("published bytes do not match the checked artifact")
        text = published.decode("utf-8")
        for marker in ("Demo data", "Source revision", "Build ID"):
            if marker not in text:
                raise ValueError(f"published demo is missing marker: {marker}")
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        print(f"Public demo deployment check failed: {exc}", file=sys.stderr)
        return 1

    print(
        "Public demo deployment: PASS "
        f"(exact byte match, sha256={hashlib.sha256(artifact).hexdigest()}, "
        f"Source revision={source_revision_match.group(1).decode('ascii')})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
