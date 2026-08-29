"""Script-safe JSON serialization for bootstrap payloads embedded in inline ``<script>`` blocks.

JSON text is not automatically safe to place inside an HTML ``<script>`` element:
``</script>`` inside a JSON string terminates the element early, ``<!--`` can open an
HTML comment inside script data, and U+2028/U+2029 are line terminators in older
JavaScript engines. This module provides one tested boundary that hardens JSON text
before it crosses into HTML, and a Jinja filter so templates never need ``|safe`` for
pre-serialized bootstrap values.

Every replacement is a valid JSON string escape, so ``json.loads`` / ``JSON.parse`` of the
hardened text yields exactly the original value.
"""

from __future__ import annotations

import json
from typing import Any

from markupsafe import Markup

SCRIPT_JSON_FILTER_NAME = "script_json_text"

# Characters that must never appear literally inside a <script> JSON payload, mapped to
# equivalent JSON escape sequences. U+2028/U+2029 are spelled with chr() so the source
# file stays pure ASCII and greppable.
_SCRIPT_UNSAFE_TRANSLATION = str.maketrans(
    {
        "<": "\\u003c",
        ">": "\\u003e",
        "&": "\\u0026",
        "'": "\\u0027",
        chr(0x2028): "\\u2028",
        chr(0x2029): "\\u2029",
    }
)


def script_safe_json_text(json_text: str) -> str:
    """Harden already-serialized JSON text for embedding inside a ``<script>`` block.

    The transformation only touches characters that can legally occur inside JSON
    string literals, replacing each with its ``\\uXXXX`` escape, so the result is still
    valid JSON that decodes to the same value. It is idempotent.
    """

    return str(json_text).translate(_SCRIPT_UNSAFE_TRANSLATION)


def script_safe_json(value: Any, **dumps_kwargs: Any) -> str:
    """Serialize ``value`` with :func:`json.dumps` and harden it for ``<script>`` embedding."""

    return script_safe_json_text(json.dumps(value, **dumps_kwargs))


def _script_json_text_filter(json_text: Any) -> Markup:
    if json_text is None:
        return Markup("null")
    return Markup(script_safe_json_text(json_text))


def register_script_json_filters(env: Any) -> None:
    """Register the ``script_json_text`` filter on a Jinja environment.

    ``script_json_text`` expects pre-serialized JSON text (the ``*_json`` context values
    used by the main UI, Storage Fabric, and offline snapshot exports) and returns
    ``Markup`` so autoescaping does not double-encode it.
    """

    env.filters[SCRIPT_JSON_FILTER_NAME] = _script_json_text_filter
