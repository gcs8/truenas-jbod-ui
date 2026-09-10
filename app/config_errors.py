"""Plain-language startup errors for the three settings loaders.

Pydantic reports a bad value as ``auto_stop_seconds Input should be a valid
integer`` followed by a documentation link. The person reading the container
log typed ``ADMIN_AUTO_STOP_SECONDS=3600.0`` into ``.env`` and needs to hear
exactly that. This module turns each validation problem into one sentence that
names the variable (or the ``config.yaml`` key path), where it came from, and
what shape the value must have, without ever echoing the value itself.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from pydantic import ValidationError

ProblemLocation = tuple[int | str, ...]
LocationResolver = Callable[[ProblemLocation], tuple[str, str] | None]

_UNIT_HINTS: tuple[tuple[str, str], ...] = (
    ("_MS", " of milliseconds"),
    ("_SECONDS", " of seconds"),
    ("_DAYS", " of days"),
    ("_BYTES", " of bytes"),
)
_EXTRA_HINTS: dict[str, str] = {
    "ADMIN_AUTO_STOP_SECONDS": " (0 disables auto-stop)",
}


class ConfigurationError(SystemExit):
    """Stop the process with one plain sentence per configuration problem."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = [str(problem) for problem in problems]
        super().__init__("\n".join(f"Configuration error: {problem}" for problem in self.problems))


def format_location(location: Sequence[int | str]) -> str:
    """Render a pydantic error location the way the key appears in YAML."""
    rendered = ""
    for part in location:
        if isinstance(part, int):
            rendered += f"[{part}]"
        elif rendered:
            rendered += f".{part}"
        else:
            rendered = str(part)
    return rendered


def _unit_hint(subject: str) -> str:
    upper = subject.upper()
    for suffix, hint in _UNIT_HINTS:
        if upper.endswith(suffix):
            return hint + _EXTRA_HINTS.get(subject, "")
    return _EXTRA_HINTS.get(subject, "")


def _strip_prefix(message: str) -> str:
    for prefix in ("Value error, ", "Assertion failed, "):
        if message.startswith(prefix):
            return message[len(prefix):]
    return message


def _expected_choices(context: Mapping[str, Any]) -> str:
    expected = str(context.get("expected") or "")
    choices = [
        choice.strip().strip("'\"")
        for choice in expected.replace(" or ", ", ").split(",")
        if choice.strip()
    ]
    return ", ".join(choices)


def _requirement(error: Mapping[str, Any], subject: str) -> str:
    kind = str(error.get("type") or "")
    context = error.get("ctx") or {}
    unit = _unit_hint(subject)
    if kind in {"int_type", "int_parsing", "int_from_float"}:
        return f"must be a whole number{unit}"
    if kind in {"float_type", "float_parsing"}:
        return f"must be a number{unit}"
    if kind in {"bool_type", "bool_parsing"}:
        return "must be true or false"
    if kind == "literal_error":
        return f"must be one of {_expected_choices(context)}"
    if kind in {"string_type", "string_too_short", "string_too_long"}:
        return "must be text"
    if kind == "list_type":
        return 'must be a JSON list such as ["first", "second"]'
    if kind in {"dict_type", "model_type", "model_attributes_type"}:
        return "must be a mapping of keys to values"
    if kind == "greater_than_equal":
        return f"must be {context.get('ge')} or more{unit}"
    if kind == "greater_than":
        return f"must be more than {context.get('gt')}{unit}"
    if kind == "less_than_equal":
        return f"must be {context.get('le')} or less{unit}"
    if kind == "less_than":
        return f"must be less than {context.get('lt')}{unit}"
    if kind == "missing":
        return "is required"
    message = _strip_prefix(str(error.get("msg") or "is not valid"))
    return f"is not valid: {message.rstrip('.')}"


def describe_validation_error(
    error: ValidationError,
    *,
    resolve_location: LocationResolver,
    default_source: str,
) -> list[str]:
    """Return one plain sentence per problem in ``error``.

    ``resolve_location`` maps a pydantic location to ``(subject, source)``, for
    example ``("ADMIN_AUTO_STOP_SECONDS", ".env")`` or
    ``("systems[0].truenas.platform", "/app/config/config.yaml")``. Problems
    raised by whole-model validators carry an empty location and are already
    written as complete sentences, so they pass through untouched.
    """
    problems: list[str] = []
    for item in error.errors(include_url=False, include_input=False, include_context=True):
        location = tuple(item.get("loc") or ())
        kind = str(item.get("type") or "")
        if not location and kind in {"value_error", "assertion_error"}:
            problems.append(_strip_prefix(str(item.get("msg") or "")).strip())
            continue
        resolved = resolve_location(location) if location else None
        subject, source = resolved or (format_location(location) or "configuration", default_source)
        problems.append(f"{subject} in {source} {_requirement(item, subject)}.")
    return problems or ["configuration could not be validated."]
