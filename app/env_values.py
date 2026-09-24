"""Small helpers shared by the settings loaders for reading ``.env`` values."""

from __future__ import annotations

import os
import types
from typing import Any, Literal, Union, get_args, get_origin


def env_is_set(env_name: str) -> bool:
    """True when the variable exists and is not blank. ``KEY=`` counts as unset."""
    value = os.getenv(env_name)
    return value is not None and value.strip() != ""


def annotation_is_text(annotation: Any) -> bool:
    """True when a field accepts plain text, so its ``.env`` value must stay a string."""
    if annotation is str:
        return True
    origin = get_origin(annotation)
    if origin is Literal:
        return all(isinstance(choice, str) for choice in get_args(annotation))
    if origin is Union or origin is types.UnionType:
        return any(
            annotation_is_text(member) for member in get_args(annotation) if member is not type(None)
        )
    return False
