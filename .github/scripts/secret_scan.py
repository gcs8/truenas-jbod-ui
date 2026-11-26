"""Pinned, redacted full-reachable-history gitleaks scan. No raw output is published."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path


VERSION = "8.30.1"
RELEASE_URL = f"https://github.com/gitleaks/gitleaks/releases/download/v{VERSION}"
ARCHIVE_NAME = f"gitleaks_{VERSION}_linux_x64.tar.gz"
ARCHIVE_SHA256 = "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"
MANIFEST_NAME = f"gitleaks_{VERSION}_checksums.txt"
MANIFEST_SHA256 = "061476c21adaf5441516f96f185c1a4706a83cd6329b9b38762271b3d4a52fae"
BINARY_SHA256 = "88f91962aa2f93ac6ab281d553b9e125f5197bbbce38f9f2437f7299c32e5509"
# Explicit config prevents ambient GITLEAKS_CONFIG or a repository .gitleaks.toml
# from replacing the pinned release's built-in detectors. No added exclusions.
DEFAULT_CONFIG = "[extend]\nuseDefault = true\n"
FINGERPRINT_RE = re.compile(r"[0-9a-f]{40}:([A-Za-z0-9_./-]+):[a-z0-9][a-z0-9-]*:[1-9][0-9]*")


def validate_ignore(text: str) -> list[str]:
    """Require commit-specific native fingerprints and an adjacent review note."""
    fingerprints: list[str] = []
    previous = ""
    for line in text.splitlines():
        if line and not line.startswith("#"):
            match = FINGERPRINT_RE.fullmatch(line)
            if match is None:
                raise ValueError("invalid fingerprint")
            path = match[1]
            if path.startswith("/") or any(part in {"", ".", ".."} for part in path.split("/")):
                raise ValueError("invalid relative path")
            if not previous.startswith("# Reviewed ") or path not in previous or len(previous) < 35:
                raise ValueError("specific preceding review note required")
            if line in fingerprints:
                raise ValueError("duplicate fingerprint")
            fingerprints.append(line)
        previous = line
    return fingerprints


def scanner_env(temp: Path) -> dict[str, str]:
    return {
        "PATH": os.environ["PATH"],
        "HOME": str(temp),
        "TMPDIR": str(temp),
        "LC_ALL": "C",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }


def verify_binary(binary: Path) -> None:
    if binary.is_symlink() or hashlib.sha256(binary.read_bytes()).hexdigest() != BINARY_SHA256:
        raise ValueError("scanner integrity failure")
    result = subprocess.run(
        [str(binary), "version"],
        capture_output=True,
        check=True,
        timeout=10,
        env=scanner_env(binary.parent),
    )
    if result.stdout.decode().strip() != VERSION or result.stderr:
        raise ValueError("scanner version mismatch")


def extract_binary(data: bytes) -> bytes:
    if hashlib.sha256(data).hexdigest() != ARCHIVE_SHA256:
        raise ValueError("release checksum mismatch")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        members = archive.getmembers()
        if {item.name for item in members} != {"gitleaks", "LICENSE", "README.md"} or len(members) != 3:
            raise ValueError("unexpected release members")
        if any(not item.isfile() or item.size > 32 * 1024 * 1024 for item in members):
            raise ValueError("unsafe release member")
        stream = archive.extractfile("gitleaks")
        if stream is None:
            raise ValueError("missing scanner")
        with stream:
            binary = stream.read()
    if hashlib.sha256(binary).hexdigest() != BINARY_SHA256:
        raise ValueError("scanner checksum mismatch")
    return binary


def download(name: str, digest: str) -> bytes:
    with urllib.request.urlopen(f"{RELEASE_URL}/{name}", timeout=60) as response:
        data = response.read(64 * 1024 * 1024 + 1)
    if len(data) > 64 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("download integrity failure")
    return data


def install(directory: Path) -> None:
    manifest = download(MANIFEST_NAME, MANIFEST_SHA256).decode("ascii")
    if f"{ARCHIVE_SHA256}  {ARCHIVE_NAME}" not in manifest.splitlines():
        raise ValueError("upstream checksum disagreement")
    binary = extract_binary(download(ARCHIVE_NAME, ARCHIVE_SHA256))
    directory.mkdir(mode=0o700)
    target = directory / "gitleaks"
    with target.open("xb") as stream:
        stream.write(binary)
    target.chmod(0o700)
    verify_binary(target)
    print(json.dumps({"status": "installed", "version": VERSION}))


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "--no-replace-objects", "-C", str(repo), *args],
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=30,
        env=scanner_env(Path(tempfile.gettempdir())),
    ).strip()


def scan_command(binary: Path, repo: Path, ignore: Path, config: Path, report: Path) -> list[str]:
    return [
        str(binary),
        "git",
        "--log-opts=--all --full-history -m",
        "--redact=100",
        "--ignore-gitleaks-allow",
        "--exit-code=1",
        "--no-banner",
        "--no-color",
        "--log-level=error",
        "--timeout=600",
        "--config",
        str(config),
        "--gitleaks-ignore-path",
        str(ignore),
        "--report-format=json",
        "--report-path",
        str(report),
        str(repo),
    ]


def scan(binary: Path, repo: Path, ignore: Path) -> int:
    try:
        verify_binary(binary)
        reviewed = ignore.read_text(encoding="utf-8")
        validate_ignore(reviewed)
        if git(repo, "rev-parse", "--is-shallow-repository") != "false":
            raise ValueError("full history required")
        with tempfile.TemporaryDirectory(prefix="gitleaks-") as raw:
            temp = Path(raw)
            # Copy the validated policy once. Reports remain private, outside the
            # checkout, and are removed even on scanner or parsing failure.
            policy = temp / ".gitleaksignore"
            policy.write_text(reviewed, encoding="utf-8")
            config = temp / "default.toml"
            config.write_text(DEFAULT_CONFIG, encoding="utf-8")
            report = temp / "report.json"
            result = subprocess.run(
                scan_command(binary, repo, policy, config, report),
                env=scanner_env(temp),
                capture_output=True,
                check=False,
                timeout=630,
            )
            # Error-level diagnostics can signal partial scan failures. They are
            # never printed, and an error cannot become a zero-finding success.
            if result.returncode not in {0, 1} or result.stderr or result.stdout:
                raise ValueError("scanner failed")
            findings = json.loads(report.read_bytes())
            if not isinstance(findings, list) or any(
                not isinstance(row, dict) or row.get("Secret") != "REDACTED" for row in findings
            ):
                raise ValueError("invalid redacted report")
            if bool(findings) != (result.returncode == 1):
                raise ValueError("scanner result disagreement")
            count = len(findings)
    except (OSError, ValueError, subprocess.SubprocessError):
        print(json.dumps({"status": "scanner_error", "version": VERSION}))
        return 2
    print(json.dumps({"status": "findings" if count else "clean", "findings": count, "version": VERSION}))
    return 1 if count else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    installer = commands.add_parser("install")
    installer.add_argument("directory", type=Path)
    scanner = commands.add_parser("scan")
    scanner.add_argument("--binary", required=True, type=Path)
    scanner.add_argument("--repo", type=Path, default=Path.cwd())
    scanner.add_argument("--ignore", type=Path, default=Path(".gitleaksignore"))
    args = parser.parse_args()
    try:
        if args.command == "install":
            install(args.directory)
            return 0
        return scan(args.binary.resolve(), args.repo.resolve(), args.ignore)
    except (OSError, ValueError, tarfile.TarError, subprocess.SubprocessError):
        print(json.dumps({"status": "scanner_error", "version": VERSION}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
