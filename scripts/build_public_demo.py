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
from scripts.public_demo_source_parity import add_source_parity_manifest  # noqa: E402


DEFAULT_OUTPUT = ROOT / "public-demo" / "index.html"


def normalize_artifact_html(html: str) -> str:
    trailing_newline = "\n" if html.endswith("\n") else ""
    return "\n".join(line.rstrip() for line in html.splitlines()) + trailing_newline


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the static public demo from the checked-in synthetic fixture and declared source graph.",
        epilog="The normal build is deterministic and uses no config, history database, cache, logs, or live systems.",
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
        help="Verify that the output already equals a fresh deterministic build.",
    )
    return parser.parse_args()


async def run() -> int:
    args = parse_args()
    output_path = args.output if args.output.is_absolute() else ROOT / args.output
    try:
        html = normalize_artifact_html(await build_public_demo_html())
        html = add_source_parity_manifest(html, source_root=ROOT)
    except (RuntimeError, ValueError) as exc:
        print(f"Public demo generation failed: {exc}", file=sys.stderr)
        return 1

    if args.check:
        if not output_path.exists():
            print(f"Public demo artifact is missing: {output_path}", file=sys.stderr)
            return 1
        if output_path.read_text(encoding="utf-8") != html:
            print(f"Public demo artifact is stale: {output_path}", file=sys.stderr)
            return 1
        print(f"Public demo artifact is current: {output_path}")
        return 0

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8", newline="\n")
    print(
        "Built deterministic synthetic public demo artifact "
        f"{output_path} ({len(html.encode('utf-8'))} bytes, generated_at={PUBLIC_DEMO_GENERATED_AT.isoformat()})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
