from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen

import yaml


ROOT = Path(__file__).resolve().parents[1]
STANDARD_LINK_PATTERN = re.compile(r"(?P<image>!)?\[[^\]]*\]\((?P<target>[^)]+)\)")
WIKI_LINK_PATTERN = re.compile(r"\[\[(?:[^\]|]+\|)?(?P<target>[^\]]+)\]\]")
FENCE_PATTERN = re.compile(r"^```(?P<language>[A-Za-z0-9_-]*)\s*\n(?P<body>.*?)^```\s*$", re.MULTILINE | re.DOTALL)
HEADING_PATTERN = re.compile(r"^#{1,6}\s+(?P<title>.+?)\s*#*\s*$", re.MULTILINE)
ENV_ASSIGNMENT_PATTERN = re.compile(r"^(?:export\s+|\$env:)?(?P<key>[A-Z][A-Z0-9_]+)\s*=", re.MULTILINE)
SCRIPT_COMMAND_PATTERN = re.compile(
    r"(?:python(?:3)?|\.\\\.venv\\Scripts\\python\.exe)\s+(?P<path>(?:scripts|tests)/[^\s'\";]+\.py)"
)
COPY_SOURCE_PATTERN = re.compile(r"(?:^|\n)\s*cp\s+(?P<path>[^\s'\"]+)", re.MULTILINE)
COMPOSE_FILE_PATTERN = re.compile(r"docker\s+compose\s+-f\s+(?P<path>[^\s'\"]+)")
DOCUMENT_PATHS = (Path("README.md"), *tuple(sorted(Path("wiki").glob("*.md"))))
PROCEDURAL_ENV_KEYS = {
    "PATH",
    "PYTHONPATH",
    "PYTHONHASHSEED",
    "SOURCE_COMMIT",
    "WIKI_COMMIT",
    "SCREENSHOT_SOURCE_REVISION",
    "SCREENSHOT_TAG",
    "PUBLIC_DEMO_SOURCE_REVISION",
    "PUBLIC_DEMO_ARTIFACT",
    "PUBLIC_DEMO_URL",
    "SLOT_FOCUS_ARTIFACT",
    "PLAYWRIGHT_BROWSER_CHANNEL",
    "TMPDIR",
    "USER",
}
RUNTIME_CREATED_PATHS = {".env", "compose.yaml"}
UNSLOP_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("em dash", re.compile("—")),
    ("curly quotation mark", re.compile("[“”]")),
    ("chatbot phrase", re.compile(r"(?i)\b(?:I hope this helps|let me know|of course|certainly)\b")),
    (
        "AI-style puffery",
        re.compile(r"(?i)\b(?:pivotal|testament|evolving landscape|groundbreaking|showcase|tapestry|delve)\b"),
    ),
    ("decorative emoji", re.compile("[✅🚀💡🎉]")),
)


@dataclass(frozen=True)
class CheckReport:
    documents: int
    local_links: int
    wiki_links: int
    external_links: int
    images: int
    yaml_examples: int
    command_paths: int
    configuration_keys: int
    prose_patterns: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check the current README and Wiki navigation, examples, and screenshot inventory."
    )
    parser.add_argument("--root", type=Path, default=ROOT, help="Repository root to inspect.")
    parser.add_argument(
        "--check-external",
        action="store_true",
        help="Fetch every unique external Markdown link with bounded retries.",
    )
    parser.add_argument("--external-timeout-seconds", type=float, default=10.0)
    parser.add_argument("--external-max-attempts", type=int, default=2)
    return parser.parse_args()


def markdown_documents(root: Path) -> tuple[Path, ...]:
    return (root / "README.md", *tuple(sorted((root / "wiki").glob("*.md"))))


def strip_link_title(target: str) -> str:
    target = target.strip()
    if target.startswith("<") and target.endswith(">"):
        return target[1:-1]
    match = re.match(r"^(\S+)(?:\s+[\"'].*[\"'])?$", target)
    return match.group(1) if match else target


def heading_anchors(text: str) -> set[str]:
    anchors: set[str] = set()
    counts: dict[str, int] = {}
    for match in HEADING_PATTERN.finditer(text):
        title = re.sub(r"<[^>]+>", "", match.group("title")).strip().lower()
        slug = re.sub(r"[^\w\- ]", "", title, flags=re.UNICODE)
        slug = re.sub(r"[\s-]+", "-", slug).strip("-")
        occurrence = counts.get(slug, 0)
        counts[slug] = occurrence + 1
        anchors.add(slug if occurrence == 0 else f"{slug}-{occurrence}")
    return anchors


def read_env_keys(root: Path) -> set[str]:
    keys = set(PROCEDURAL_ENV_KEYS)
    text = (root / ".env.example").read_text(encoding="utf-8")
    keys.update(match.group("key") for match in ENV_ASSIGNMENT_PATTERN.finditer(text))
    for relative_path in ("app/config.py", "history_service/config.py", "admin_service/config.py"):
        parsed = ast.parse((root / relative_path).read_text(encoding="utf-8"), filename=relative_path)
        for node in ast.walk(parsed):
            value: ast.expr | None = None
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "ENV_OVERRIDES" for target in node.targets
            ):
                value = node.value
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                if node.target.id == "ENV_OVERRIDES":
                    value = node.value
            if isinstance(value, ast.Dict):
                keys.update(
                    key.value
                    for key in value.keys
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)
                )
    return keys


def check_path_reference(root: Path, value: str) -> bool:
    normalized = value.replace("\\", "/").removeprefix("./")
    if not normalized or normalized.startswith(("/", "$", "<")) or "://" in normalized:
        return True
    if normalized in RUNTIME_CREATED_PATHS:
        return True
    return (root / normalized).is_file()


def same_repository_main_path(url: str) -> Path | None:
    parsed = urlsplit(url)
    prefix = "/gcs8/truenas-jbod-ui/blob/main/"
    if parsed.scheme != "https" or parsed.netloc.lower() != "github.com" or not parsed.path.startswith(prefix):
        return None
    relative = Path(unquote(parsed.path.removeprefix(prefix)))
    if relative.is_absolute() or ".." in relative.parts:
        return None
    return relative


def fetch_external(url: str, *, timeout_seconds: float, max_attempts: int) -> str | None:
    if max_attempts < 1:
        return "external max attempts must be at least one"
    request = Request(
        url,
        headers={
            "User-Agent": "truenas-jbod-ui-doc-check/1",
            "Range": "bytes=0-1023",
        },
    )
    last_error = "unknown error"
    for attempt in range(max_attempts):
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                if 200 <= response.status < 400:
                    response.read(1024)
                    return None
                last_error = f"HTTP {response.status}"
        except HTTPError as exc:
            if 200 <= exc.code < 400:
                return None
            last_error = f"HTTP {exc.code}"
        except (TimeoutError, URLError, OSError) as exc:
            last_error = type(exc).__name__
        if attempt + 1 < max_attempts:
            time.sleep(min(2**attempt, 2))
    return last_error


def check_docs(
    root: Path,
    *,
    check_external: bool = False,
    external_timeout_seconds: float = 10.0,
    external_max_attempts: int = 2,
) -> tuple[CheckReport, list[str]]:
    documents = markdown_documents(root)
    errors: list[str] = []
    if len(documents) != 25:
        errors.append(f"expected 25 README/Wiki documents, found {len(documents)}")
    env_keys = read_env_keys(root)
    local_links = wiki_links = external_links = image_count = yaml_examples = command_paths = config_keys = 0
    prose_patterns = 0
    external_urls: set[str] = set()
    screenshot_refs: set[tuple[str, str]] = set()

    for document in documents:
        relative_document = document.relative_to(root).as_posix()
        text = document.read_text(encoding="utf-8")
        for label, pattern in UNSLOP_PATTERNS:
            count = len(pattern.findall(text))
            prose_patterns += count
            if count:
                errors.append(f"{relative_document}: found {count} {label} prose pattern(s)")
        anchors_by_path: dict[Path, set[str]] = {document: heading_anchors(text)}
        for match in STANDARD_LINK_PATTERN.finditer(text):
            target = strip_link_title(match.group("target"))
            parsed = urlsplit(target)
            if parsed.scheme in {"http", "https"}:
                external_links += 1
                if parsed.scheme != "https":
                    errors.append(f"{relative_document}: external Markdown link must use HTTPS: {target}")
                candidate_path = same_repository_main_path(target)
                if candidate_path is not None:
                    if not (root / candidate_path).is_file():
                        errors.append(f"{relative_document}: missing same-repository link target: {target}")
                    continue
                external_urls.add(target)
                continue
            if parsed.scheme or target.startswith(("mailto:", "data:")):
                continue
            path_text = unquote(parsed.path)
            resolved = document if not path_text else (document.parent / path_text).resolve()
            local_links += 1
            if not resolved.is_file():
                errors.append(f"{relative_document}: missing local link target: {target}")
                continue
            if parsed.fragment and resolved.suffix.lower() == ".md":
                anchors = anchors_by_path.setdefault(
                    resolved,
                    heading_anchors(resolved.read_text(encoding="utf-8")),
                )
                if unquote(parsed.fragment).lower() not in anchors:
                    errors.append(f"{relative_document}: missing Markdown anchor: {target}")
            if match.group("image"):
                image_count += 1
                screenshot_refs.add((relative_document, resolved.relative_to(root).as_posix()))

        for match in WIKI_LINK_PATTERN.finditer(text):
            wiki_links += 1
            target = match.group("target").strip()
            slug, _separator, fragment = target.partition("#")
            resolved = root / "wiki" / f"{slug}.md"
            if not resolved.is_file():
                errors.append(f"{relative_document}: missing Wiki page: {target}")
                continue
            if fragment and fragment.lower() not in heading_anchors(resolved.read_text(encoding="utf-8")):
                errors.append(f"{relative_document}: missing Wiki anchor: {target}")

        for fence in FENCE_PATTERN.finditer(text):
            language = fence.group("language").lower()
            body = fence.group("body")
            if language in {"yaml", "yml"}:
                yaml_examples += 1
                try:
                    yaml.safe_load(body)
                except yaml.YAMLError as exc:
                    errors.append(f"{relative_document}: invalid YAML example: {type(exc).__name__}")
            if language not in {"bash", "sh", "shell", "powershell", "dotenv", "text", "yaml", "yml"}:
                continue
            for pattern in (SCRIPT_COMMAND_PATTERN, COPY_SOURCE_PATTERN, COMPOSE_FILE_PATTERN):
                for command_match in pattern.finditer(body):
                    path = command_match.group("path").rstrip("\\")
                    command_paths += 1
                    if not check_path_reference(root, path):
                        errors.append(f"{relative_document}: command references missing path: {path}")
            for env_match in ENV_ASSIGNMENT_PATTERN.finditer(body):
                key = env_match.group("key")
                config_keys += 1
                if key not in env_keys:
                    errors.append(f"{relative_document}: undocumented configuration key: {key}")

    manifest_path = root / "docs/images/screenshots/manifest.json"
    expected_names: set[str] = set()
    if not manifest_path.is_file():
        errors.append("missing docs/images/screenshots/manifest.json")
    else:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            images = manifest.get("images")
            if not isinstance(images, dict):
                raise ValueError
            expected_names = set(images)
        except (json.JSONDecodeError, ValueError):
            errors.append("invalid docs/images/screenshots/manifest.json")
    docs_assets = {path.name for path in (root / "docs/images/screenshots").glob("*.png")}
    wiki_assets = {path.name for path in (root / "wiki/images").glob("*.png")}
    if docs_assets != expected_names:
        errors.append("docs screenshot set does not match the manifest")
    if wiki_assets != expected_names:
        errors.append("Wiki screenshot set does not match the manifest")
    for name in sorted(expected_names):
        docs_path = f"docs/images/screenshots/{name}"
        wiki_path = f"wiki/images/{name}"
        if not any(path == docs_path for _document, path in screenshot_refs):
            errors.append(f"README/Wiki does not reference {docs_path}")
        if not any(path == wiki_path for _document, path in screenshot_refs):
            errors.append(f"README/Wiki does not reference {wiki_path}")

    if check_external:
        for url in sorted(external_urls):
            failure = fetch_external(
                url,
                timeout_seconds=external_timeout_seconds,
                max_attempts=external_max_attempts,
            )
            if failure:
                errors.append(f"external link failed ({failure}): {url}")

    return (
        CheckReport(
            documents=len(documents),
            local_links=local_links,
            wiki_links=wiki_links,
            external_links=external_links,
            images=image_count,
            yaml_examples=yaml_examples,
            command_paths=command_paths,
            configuration_keys=config_keys,
            prose_patterns=prose_patterns,
        ),
        errors,
    )


def main() -> int:
    args = parse_args()
    report, errors = check_docs(
        args.root.resolve(),
        check_external=args.check_external,
        external_timeout_seconds=args.external_timeout_seconds,
        external_max_attempts=args.external_max_attempts,
    )
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print(
        "Public docs: PASS "
        f"({report.documents} documents, {report.local_links} local links, "
        f"{report.wiki_links} Wiki links, {report.external_links} external links, "
        f"{report.images} images, {report.yaml_examples} YAML examples, "
        f"{report.command_paths} command paths, {report.configuration_keys} configuration keys, "
        f"{report.prose_patterns} prose patterns)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
