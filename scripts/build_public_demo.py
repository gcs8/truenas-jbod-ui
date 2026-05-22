from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.public_demo_fixture import (  # noqa: E402
    PUBLIC_DEMO_GENERATED_AT,
    build_public_demo_html,
)


DEFAULT_OUTPUT = ROOT / "public-demo" / "index.html"
LOCAL_HISTORY_GUIDANCE = (
    "Public demo release generation requires local ignored history/history.db "
    "release input. Clean CI validates the checked-in public-demo/index.html "
    "artifact with scripts/check_public_demo_artifact.py instead."
)


def normalize_artifact_html(html: str) -> str:
    """Keep the generated artifact deterministic and diff-check friendly."""
    trailing_newline = "\n" if html.endswith("\n") else ""
    return "\n".join(line.rstrip() for line in html.splitlines()) + trailing_newline


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build the static public demo artifact from local ignored "
            "history/history.db release input."
        ),
        epilog=(
            "Clean checkout / CI validation should use "
            "scripts/check_public_demo_artifact.py public-demo instead of "
            "regenerating from local history."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"HTML output path. Defaults to {DEFAULT_OUTPUT}.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "Verify the output file already matches the generated artifact; "
            "requires the same local ignored history/history.db release input."
        ),
    )
    return parser.parse_args()


async def run() -> int:
    args = parse_args()
    output_path = args.output
    if not output_path.is_absolute():
        output_path = ROOT / output_path
    try:
        html = normalize_artifact_html(await build_public_demo_html())
    except RuntimeError as exc:
        print(f"Public demo generation failed: {exc}", file=sys.stderr)
        if "history/history.db" in str(exc):
            print(LOCAL_HISTORY_GUIDANCE, file=sys.stderr)
        return 1

    if args.check:
        if not output_path.exists():
            print(f"Public demo artifact is missing: {output_path}", file=sys.stderr)
            return 1
        current = output_path.read_text(encoding="utf-8")
        if current != html:
            print(f"Public demo artifact is stale: {output_path}", file=sys.stderr)
            return 1
        print(f"Public demo artifact is current: {output_path}")
        return 0

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    print(
        "Built public demo artifact "
        f"{output_path} ({len(html.encode('utf-8'))} bytes, "
        f"generated_at={PUBLIC_DEMO_GENERATED_AT.isoformat()})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
