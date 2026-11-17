"""Streaming JSON validation for file-backed backup members.

The reader walks one JSON member in bounded chunks, checks object keys for
duplicates in a temporary on-disk table, and hands each mapping entry to its
model within per-entry limits. ``history_service.system_backup`` re-exports
every name defined here.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

JSON_STREAM_CHUNK_CHARS = 64 * 1024
MAX_JSON_KEY_CHARS = 64 * 1024
JSON_DUPLICATE_CACHE_KIB = 512
# Retained-size limits for ONE mapping entry of a file-backed JSON member. The
# member-size limit does not bound what one entry builds in memory, so the
# fields a model reads are counted while they are built: every value is one
# node, and key, string and number text counts toward the character limit.
# Fields the model ignores are checked and skipped without being built.
MAX_JSON_ENTRY_NODES = 65_536
MAX_JSON_ENTRY_CHARS = 1024 * 1024
JSON_ENTRY_LIMIT_MESSAGE = "JSON mapping entry exceeds its validation limit."


class _DiskBackedDuplicateKeyTracker:
    """Track object-scoped JSON keys without retaining payload keys in Python."""

    __slots__ = ("connection", "cursor", "next_scope", "root")

    def __init__(self) -> None:
        self.root: Path | None = None
        self.connection: sqlite3.Connection | None = None
        self.cursor: sqlite3.Cursor | None = None
        self.next_scope = 0

    def __enter__(self) -> "_DiskBackedDuplicateKeyTracker":
        try:
            self.root = Path(tempfile.mkdtemp(prefix="truenas-jbod-ui-json-keys-"))
            self.root.chmod(0o700)
            database_path = self.root / "keys.sqlite3"
            descriptor = os.open(
                database_path,
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)
            self.connection = sqlite3.connect(database_path)
            self.connection.execute("PRAGMA journal_mode = OFF")
            self.connection.execute("PRAGMA synchronous = OFF")
            self.connection.execute("PRAGMA temp_store = FILE")
            self.connection.execute(f"PRAGMA cache_size = -{JSON_DUPLICATE_CACHE_KIB}")
            self.connection.execute("PRAGMA mmap_size = 0")
            self.connection.execute("PRAGMA locking_mode = EXCLUSIVE")
            self.connection.execute(
                "CREATE TABLE keys (scope INTEGER NOT NULL, value TEXT NOT NULL, "
                "PRIMARY KEY (scope, value)) WITHOUT ROWID"
            )
            self.cursor = self.connection.cursor()
            return self
        except BaseException as initialization_error:
            if isinstance(initialization_error, Exception):
                primary_error: BaseException = ValueError(
                    "Temporary files could not be created; check free space in the temp folder."
                )
            else:
                primary_error = initialization_error
            try:
                self.close()
            except RuntimeError as cleanup_error:
                primary_error.add_note(str(cleanup_error))
            if primary_error is initialization_error:
                raise
            raise primary_error from None

    def new_scope(self) -> int:
        scope = self.next_scope
        self.next_scope += 1
        return scope

    def add(self, scope: int, key: str) -> None:
        if self.cursor is None:
            raise ValueError("Temporary files could not be created; check free space in the temp folder.")
        try:
            self.cursor.execute(
                "INSERT INTO keys (scope, value) VALUES (?, ?)",
                (scope, key),
            )
        except sqlite3.IntegrityError:
            raise ValueError("JSON payload contains a duplicate object key.") from None
        except sqlite3.Error:
            raise ValueError("JSON duplicate-key validation failed.") from None

    def close(self) -> None:
        cleanup_failed = False
        if self.cursor is not None:
            try:
                self.cursor.close()
            except sqlite3.Error:
                cleanup_failed = True
            self.cursor = None
        if self.connection is not None:
            try:
                self.connection.close()
            except sqlite3.Error:
                cleanup_failed = True
            self.connection = None
        root = self.root
        self.root = None
        self.next_scope = 0
        if root is not None:
            try:
                shutil.rmtree(root)
            except OSError:
                cleanup_failed = True
        if cleanup_failed:
            raise RuntimeError("JSON duplicate-key tracker cleanup failed.")

    def __exit__(
        self,
        exc_type: Any,
        exc: BaseException | None,
        traceback: Any,
    ) -> Literal[False]:
        try:
            self.close()
        except RuntimeError as cleanup_error:
            if exc is None:
                raise
            exc.add_note(str(cleanup_error))
        return False


class _StreamingJSONReader:
    """Small bounded-buffer JSON reader used for file-backed mapping members."""

    def __init__(self, source: Any) -> None:
        self.source = source
        self.buffer = ""
        self.position = 0
        self.eof = False
        self.duplicate_keys: _DiskBackedDuplicateKeyTracker | None = None
        # Per-entry budget, active only while one mapping entry is being built.
        self.entry_nodes_left: int | None = None
        self.entry_chars_left = 0

    def _charge_entry(self, chars: int) -> None:
        if self.entry_nodes_left is None:
            return
        self.entry_nodes_left -= 1
        self.entry_chars_left -= chars
        if self.entry_nodes_left < 0 or self.entry_chars_left < 0:
            raise ValueError(JSON_ENTRY_LIMIT_MESSAGE)

    def _charge_entry_key(self, key: str) -> None:
        if self.entry_nodes_left is None:
            return
        self.entry_chars_left -= len(key)
        if self.entry_chars_left < 0:
            raise ValueError(JSON_ENTRY_LIMIT_MESSAGE)

    def _new_object_scope(self) -> int:
        if self.duplicate_keys is None:
            raise ValueError("Temporary files could not be created; check free space in the temp folder.")
        return self.duplicate_keys.new_scope()

    def _record_object_key(self, scope: int, key: str) -> None:
        if self.duplicate_keys is None:
            raise ValueError("Temporary files could not be created; check free space in the temp folder.")
        self.duplicate_keys.add(scope, key)

    def _fill(self) -> bool:
        if self.position < len(self.buffer):
            return True
        if self.eof:
            return False
        self.buffer = self.source.read(JSON_STREAM_CHUNK_CHARS)
        self.position = 0
        if not self.buffer:
            self.eof = True
            return False
        return True

    def peek(self) -> str:
        return self.buffer[self.position] if self._fill() else ""

    def take(self) -> str:
        character = self.peek()
        if character:
            self.position += 1
        return character

    def whitespace(self) -> None:
        while self.peek() in {" ", "\t", "\r", "\n"}:
            self.position += 1

    def expect(self, expected: str) -> None:
        self.whitespace()
        if self.take() != expected:
            raise ValueError("JSON payload is malformed.")

    def string(self, *, materialize: bool = True) -> str | None:
        self.whitespace()
        if self.take() != '"':
            raise ValueError("JSON payload is malformed.")
        encoded = ['"'] if materialize else None
        encoded_chars = 1
        while True:
            character = self.take()
            if not character or ord(character) < 0x20:
                raise ValueError("JSON payload is malformed.")
            if encoded is not None:
                encoded.append(character)
                encoded_chars += 1
                if encoded_chars > MAX_JSON_KEY_CHARS:
                    raise ValueError("JSON scalar exceeds its validation limit.")
            if character == '"':
                break
            if character != "\\":
                continue
            escaped = self.take()
            if escaped not in {'"', "\\", "/", "b", "f", "n", "r", "t", "u"}:
                raise ValueError("JSON payload is malformed.")
            if encoded is not None:
                encoded.append(escaped)
                encoded_chars += 1
            if escaped == "u":
                digits = "".join(self.take() for _ in range(4))
                if len(digits) != 4 or any(char not in "0123456789abcdefABCDEF" for char in digits):
                    raise ValueError("JSON payload is malformed.")
                if encoded is not None:
                    encoded.append(digits)
                    encoded_chars += 4
        if encoded is None:
            return None
        return json.loads("".join(encoded))

    def value(self, *, materialize: bool, depth: int = 0) -> Any:
        if depth > 64:
            raise ValueError("JSON payload nesting exceeds its validation limit.")
        self.whitespace()
        token = self.peek()
        if token == '"':
            text = self.string(materialize=materialize)
            if text is not None:
                self._charge_entry(len(text))
            return text
        if token == "{":
            self.take()
            result: dict[str, Any] | None = {} if materialize else None
            if materialize:
                self._charge_entry(0)
            scope = self._new_object_scope()
            self.whitespace()
            if self.peek() == "}":
                self.take()
                return result
            while True:
                key = self.string()
                assert isinstance(key, str)
                self._record_object_key(scope, key)
                if result is not None:
                    self._charge_entry_key(key)
                self.expect(":")
                item = self.value(materialize=materialize, depth=depth + 1)
                if result is not None:
                    result[key] = item
                self.whitespace()
                separator = self.take()
                if separator == "}":
                    return result
                if separator != ",":
                    raise ValueError("JSON payload is malformed.")
        if token == "[":
            self.take()
            result_list: list[Any] | None = [] if materialize else None
            if materialize:
                self._charge_entry(0)
            self.whitespace()
            if self.peek() == "]":
                self.take()
                return result_list
            while True:
                item = self.value(materialize=materialize, depth=depth + 1)
                if result_list is not None:
                    result_list.append(item)
                self.whitespace()
                separator = self.take()
                if separator == "]":
                    return result_list
                if separator != ",":
                    raise ValueError("JSON payload is malformed.")
        scalar = ""
        while self.peek() and self.peek() not in {" ", "\t", "\r", "\n", ",", "]", "}"}:
            scalar += self.take()
            if len(scalar) > 1024:
                raise ValueError("JSON scalar exceeds its validation limit.")
        if not scalar:
            raise ValueError("JSON payload is malformed.")
        decoded = json.loads(scalar)
        if not materialize:
            return None
        self._charge_entry(len(scalar))
        return decoded

    def validate_mapping_entries(self, target_key: str, model: Any) -> int:
        result = 0
        try:
            with self._duplicate_key_tracker() as duplicate_keys:
                self.duplicate_keys = duplicate_keys
                result = self._validate_mapping_entries(target_key, model)
        finally:
            self.duplicate_keys = None
            self.buffer = ""
            self.position = 0
            self.eof = True
        return result

    def _validate_mapping_entries(self, target_key: str, model: Any) -> int:
        self.expect("{")
        root_scope = self._new_object_scope()
        count = 0
        self.whitespace()
        if self.peek() == "}":
            self.take()
        else:
            while True:
                key = self.string()
                assert isinstance(key, str)
                self._record_object_key(root_scope, key)
                self.expect(":")
                if key == target_key:
                    count = self._validate_entry_object(model)
                else:
                    self.value(materialize=False, depth=1)
                self.whitespace()
                separator = self.take()
                if separator == "}":
                    break
                if separator != ",":
                    raise ValueError("JSON payload is malformed.")
        self.whitespace()
        if self.peek():
            raise ValueError("JSON payload has trailing content.")
        return count

    def _validate_entry_object(self, model: Any) -> int:
        self.expect("{")
        scope = self._new_object_scope()
        count = 0
        field_names = self._entry_field_names(model)
        self.whitespace()
        if self.peek() == "}":
            self.take()
            return 0
        while True:
            key = self.string()
            assert isinstance(key, str)
            self._record_object_key(scope, key)
            self.expect(":")
            self.entry_nodes_left = MAX_JSON_ENTRY_NODES
            self.entry_chars_left = MAX_JSON_ENTRY_CHARS
            try:
                entry = self._entry_value(field_names)
            finally:
                self.entry_nodes_left = None
            model.model_validate(entry)
            count += 1
            self.whitespace()
            separator = self.take()
            if separator == "}":
                return count
            if separator != ",":
                raise ValueError("JSON payload is malformed.")

    def _entry_value(self, field_names: frozenset[str] | None) -> Any:
        """Build one mapping entry, keeping only the fields the model reads.

        Ignored fields are still parsed and their object keys still go through
        the duplicate-key tracker, but nothing of them is retained.
        """
        self.whitespace()
        if field_names is None or self.peek() != "{":
            return self.value(materialize=True, depth=2)
        self.take()
        self._charge_entry(0)
        result: dict[str, Any] = {}
        scope = self._new_object_scope()
        self.whitespace()
        if self.peek() == "}":
            self.take()
            return result
        while True:
            key = self.string()
            assert isinstance(key, str)
            self._record_object_key(scope, key)
            self.expect(":")
            if key in field_names:
                self._charge_entry_key(key)
                result[key] = self.value(materialize=True, depth=3)
            else:
                self.value(materialize=False, depth=3)
            self.whitespace()
            separator = self.take()
            if separator == "}":
                return result
            if separator != ",":
                raise ValueError("JSON payload is malformed.")

    def _duplicate_key_tracker(self) -> _DiskBackedDuplicateKeyTracker:
        return _DiskBackedDuplicateKeyTracker()

    def _entry_field_names(self, model: Any) -> frozenset[str] | None:
        return _model_entry_field_names(model)


def _model_entry_field_names(model: Any) -> frozenset[str] | None:
    """Return the entry keys a model reads, or None when every key matters.

    None (build the whole entry, within the per-entry limits) is returned when
    the model is not a plain pydantic model that ignores unknown fields, or
    when its field names cannot be listed exactly, for example alias choices,
    alias paths or an alias generator.
    """
    fields = getattr(model, "model_fields", None)
    config = getattr(model, "model_config", None)
    if not isinstance(fields, Mapping) or not isinstance(config, Mapping):
        return None
    if config.get("extra") not in (None, "ignore") or config.get("alias_generator"):
        return None
    names: set[str] = set()
    for name, info in fields.items():
        names.add(name)
        for alias in (getattr(info, "alias", None), getattr(info, "validation_alias", None)):
            if alias is None:
                continue
            if not isinstance(alias, str):
                return None
            names.add(alias)
    return frozenset(names)
