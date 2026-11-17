"""Portable 7z archive operations that `SystemBackupService` runs.

`SystemBackupService` keeps a method under each of these names, with the same
kind and signature, that passes its own module in as `backup_module` (and
itself as `self` or `cls` where the method had a receiver). Every
system-backup name these helpers use, including the limits, the 7z binary
and timeouts, the passphrase messages, the deadline policy, the metrics hook
and the logger, is looked up on `backup_module` at call time, and every
sibling method is called through `self` or `cls`, so a replacement made on the
system-backup module, class or instance still reaches this code. This module
does not import the system-backup module at run time.
"""

from __future__ import annotations

import os
import select
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from history_service.system_backup import ArchivePackaging, BundleMember, ExtractedMember


def _build_7z_archive_to_path(
    backup_module: ModuleType,
    self,
    bundle_members: list[BundleMember],
    manifest_bytes: bytes,
    archive_path: Path,
    *,
    passphrase: str | None = None,
) -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        staging_dir = Path(temp_dir) / "bundle"
        staging_dir.mkdir(parents=True, exist_ok=True)
        (staging_dir / "manifest.json").write_bytes(manifest_bytes)
        top_level_entries = {"manifest.json"}
        for member in bundle_members:
            if not member.present:
                continue
            target_path = staging_dir / member.archive_path
            target_path.parent.mkdir(parents=True, exist_ok=True)
            if member.file_path is not None:
                shutil.copyfile(member.file_path, target_path)
            else:
                target_path.write_bytes(member.content or b"")
            top_level_entries.add(member.archive_path.split("/", 1)[0])
        command = [
            "a",
            "-t7z",
            "-y",
            "-bd",
            "-mx=5",
            "-m0=lzma2",
            "-mmt=1",
            str(archive_path),
            *sorted(top_level_entries),
        ]
        if passphrase is not None:
            command.insert(4, "-mhe=on")
            command.insert(4, "-p")
        input_bytes = sum(
            path.stat().st_size for path in staging_dir.rglob("*") if path.is_file()
        )
        timeout_seconds = backup_module.seven_zip_create_timeout_seconds(input_bytes)
        backup_module.observe_backup_7z_deadline(
            operation="create",
            input_bytes=input_bytes,
            deadline_seconds=timeout_seconds,
        )
        backup_module.logger.info(
            "Portable 7z create deadline: input_bytes=%d deadline_seconds=%d "
            "floor_bytes_per_second=%d ceiling_seconds=%d",
            input_bytes,
            timeout_seconds,
            backup_module.SEVEN_ZIP_CREATE_MIN_BYTES_PER_SECOND,
            backup_module.SEVEN_ZIP_MAX_TIMEOUT_SECONDS,
        )
        result = self._run_7z_command(
            command,
            cwd=staging_dir,
            passphrase=passphrase,
            timeout_seconds=timeout_seconds,
        )
        self._raise_for_7z_failure(
            result,
            "Portable 7z backup export failed.",
            passphrase=passphrase,
        )


def _read_7z_archive(
    backup_module: ModuleType,
    self,
    *,
    archive_path: Path,
    passphrase: str | None = None,
    workspace: Path | None = None,
) -> tuple[dict[str, Any], dict[str, ExtractedMember], ArchivePackaging, dict[str, Any]]:
    temp_root = (
        Path(tempfile.mkdtemp(prefix="truenas-jbod-ui-7z-import-"))
        if workspace is None
        else workspace
    )
    try:
        archive_path = Path(archive_path).absolute()
        archive_size = archive_path.stat(follow_symlinks=False).st_size
        extract_dir = temp_root / "extract"
        extract_dir.mkdir(parents=True, exist_ok=True)

        prompt_passphrase = passphrase if passphrase else None
        list_command = [
            "l",
            "-slt",
            str(archive_path),
        ]
        if prompt_passphrase is None:
            list_command.append("-p")
        list_result = self._run_7z_command(
            list_command,
            passphrase=prompt_passphrase,
        )
        self._raise_for_7z_failure(
            list_result,
            "Backup bundle 7z archive could not be listed.",
            passphrase=passphrase,
            reading_archive=True,
        )
        listed_entries = self._seven_zip_listed_entries(list_result.stdout, archive_path)
        self._validate_7z_listed_entries(
            listed_entries,
            archive_size=archive_size,
            member_limit=backup_module.MAX_FILE_BACKED_ARCHIVE_MEMBER_BYTES,
            expanded_limit=backup_module.MAX_FILE_BACKED_ARCHIVE_EXPANDED_BYTES,
        )
        listed_paths = [entry["Path"] for entry in listed_entries]
        listed_file_paths = [
            entry["Path"]
            for entry in listed_entries
            if not self._is_7z_directory_entry(entry)
        ]
        self._validate_unique_physical_archive_paths(
            listed_paths
        )
        encrypted = self._seven_zip_encryption_mode(listed_entries)

        manifest_entries = [
            entry
            for entry in listed_entries
            if not self._is_7z_directory_entry(entry)
            and self._normalize_archive_member_path(entry["Path"]) == "manifest.json"
        ]
        if len(manifest_entries) != 1:
            raise ValueError("Backup bundle is missing manifest.json.")
        manifest_entry = manifest_entries[0]
        if int(manifest_entry["Size"]) > backup_module.MAX_MANIFEST_BYTES:
            raise ValueError("Backup bundle manifest exceeds its size limit.")

        manifest_extract_command = [
            "x",
            str(archive_path),
            f"-o{extract_dir}",
            "-y",
            "-bd",
        ]
        if prompt_passphrase is None:
            manifest_extract_command.append("-p")
        manifest_extract_command.append("manifest.json")
        manifest_extract_result = self._run_7z_command(
            manifest_extract_command,
            passphrase=prompt_passphrase,
        )
        self._raise_for_7z_failure(
            manifest_extract_result,
            "Backup bundle 7z manifest could not be extracted.",
            passphrase=passphrase,
            reading_archive=True,
        )

        manifest_path = extract_dir / "manifest.json"
        if not manifest_path.exists():
            raise ValueError("Backup bundle is missing manifest.json.")
        self._validate_extracted_7z_tree(extract_dir, [manifest_entry])
        if manifest_path.stat().st_size > backup_module.MAX_MANIFEST_BYTES:
            raise ValueError("Backup bundle manifest exceeds its size limit.")
        manifest = self._load_manifest(manifest_path.read_bytes())
        self._validate_manifest_before_extraction(
            manifest,
            member_limit=backup_module.MAX_FILE_BACKED_ARCHIVE_MEMBER_BYTES,
            expanded_limit=backup_module.MAX_FILE_BACKED_ARCHIVE_EXPANDED_BYTES,
            large_member_group_keys=frozenset({backup_module.HISTORY_DB_KEY}),
        )
        self._validate_restore_schema_support(manifest)
        self._validate_supported_physical_archive_paths(listed_file_paths, manifest)
        self._validate_7z_listing_against_manifest(listed_entries, manifest)

        extract_command = [
            "x",
            str(archive_path),
            f"-o{extract_dir}",
            "-y",
            "-bd",
        ]
        if prompt_passphrase is None:
            extract_command.append("-p")
        extract_result = self._run_7z_command(
            extract_command,
            passphrase=prompt_passphrase,
        )
        self._raise_for_7z_failure(
            extract_result,
            "Backup bundle 7z archive could not be extracted.",
            passphrase=passphrase,
            reading_archive=True,
        )
        self._validate_extracted_7z_tree(extract_dir, listed_entries)
        extracted = self._extract_manifest_directory_members(extract_dir, manifest)
        return manifest, extracted, "7z", {
            "encrypted": encrypted,
            "_cleanup_root": temp_root,
        }
    except Exception:
        try:
            shutil.rmtree(temp_root)
        except Exception as cleanup_error:
            raise RuntimeError(
                "Backup bundle 7z workspace cleanup failed."
            ) from cleanup_error
        raise


def _validate_7z_listed_entries(
    backup_module: ModuleType,
    cls,
    entries: list[dict[str, str]],
    *,
    archive_size: int,
    member_limit: int,
    expanded_limit: int,
) -> None:
    declared: list[tuple[int, int]] = []
    materialized_paths: set[str] = set()
    for entry in entries:
        normalized_path = cls._normalize_archive_member_path(entry.get("Path", ""))
        path_parts = normalized_path.split("/")
        materialized_paths.add(normalized_path)
        materialized_paths.update(
            "/".join(path_parts[:index])
            for index in range(1, len(path_parts))
        )
        if len(materialized_paths) > backup_module.MAX_ARCHIVE_MEMBER_COUNT:
            raise ValueError("Backup bundle archive contains too many members.")
        is_directory = cls._is_7z_directory_entry(entry)
        attributes = entry.get("Attributes", "")
        attribute_tokens = attributes.split()
        if (
            entry.get("Symbolic Link")
            or entry.get("Hard Link")
            or any(len(token) >= 10 and token.startswith("l") for token in attribute_tokens)
        ):
            raise ValueError("Backup bundle 7z archive contains an unsupported link.")
        try:
            expanded_size = int(entry.get("Size", "0" if is_directory else ""))
        except ValueError as exc:
            raise ValueError("Backup bundle 7z member size metadata is invalid.") from exc
        raw_packed_size = entry.get("Packed Size", "").strip()
        packed_size = 0
        if raw_packed_size:
            try:
                packed_size = int(raw_packed_size)
            except ValueError as exc:
                raise ValueError("Backup bundle 7z packed-size metadata is invalid.") from exc
            if expanded_size > backup_module.MAX_ARCHIVE_COMPRESSION_RATIO * max(packed_size, 1):
                raise ValueError(
                    "Backup bundle archive member compression ratio exceeds its limit."
                )
        declared.append((packed_size, expanded_size))
    cls._validate_declared_archive_limits(
        declared,
        compressed_total=archive_size,
        member_limit=member_limit,
        expanded_limit=expanded_limit,
    )


def _validate_7z_listing_against_manifest(
    backup_module: ModuleType,
    cls,
    entries: list[dict[str, str]],
    manifest: dict[str, Any],
) -> None:
    manifest_entries = {
        cls._normalize_archive_member_path(str(entry["archive_path"])): entry
        for entry in manifest.get("files", [])
        if isinstance(entry, dict) and entry.get("archive_path")
    }
    listed_payload_paths: set[str] = set()
    non_history_total = 0
    for listed_entry in entries:
        if cls._is_7z_directory_entry(listed_entry):
            continue
        archive_path = cls._normalize_archive_member_path(listed_entry["Path"])
        expanded_size = int(listed_entry["Size"])
        if archive_path == "manifest.json":
            non_history_total += expanded_size
            continue

        manifest_entry = manifest_entries.get(archive_path)
        if manifest_entry is None:
            raise ValueError(
                "Backup bundle archive contains a member not declared in its manifest."
            )
        listed_payload_paths.add(archive_path)
        is_history_member = cls._is_declared_history_manifest_member(
            manifest,
            key=str(manifest_entry.get("key") or "").strip(),
            group_key=str(manifest_entry.get("group_key") or "").strip(),
            archive_path=archive_path,
        )
        if (
            manifest.get("format") == backup_module.DEBUG_BUNDLE_FORMAT
            and manifest_entry.get("group_key") == backup_module.HISTORY_DB_KEY
        ):
            is_history_member = True
        if (
            manifest.get("format") == backup_module.BUNDLE_FORMAT
            and manifest_entry.get("group_key") == backup_module.HISTORY_DB_KEY
            and not is_history_member
        ):
            raise ValueError(
                "Backup bundle history member does not match its declared backup group."
            )
        member_limit = (
            backup_module.MAX_FILE_BACKED_ARCHIVE_MEMBER_BYTES
            if is_history_member
            else backup_module.MAX_ARCHIVE_MEMBER_BYTES
        )
        if expanded_size > member_limit:
            raise ValueError(
                "Backup bundle archive member exceeds its expanded byte limit."
            )

        expected_size = manifest_entry.get("size_bytes")
        if expected_size is not None and expanded_size != expected_size:
            raise ValueError(
                "Backup bundle archive member size does not match its manifest."
            )
        if not is_history_member:
            non_history_total += expanded_size

    if listed_payload_paths != set(manifest_entries):
        raise ValueError("Backup bundle archive members do not match its manifest.")
    if non_history_total > backup_module.MAX_ARCHIVE_EXPANDED_BYTES:
        raise ValueError(
            "Backup bundle non-history members exceed the expanded byte limit."
        )


def _seven_zip_encryption_mode(backup_module: ModuleType, cls, entries: list[dict[str, str]]) -> bool:
    observed: set[bool] = set()
    for entry in entries:
        if cls._is_7z_directory_entry(entry):
            continue
        if "Encrypted" not in entry:
            raise ValueError("Backup bundle 7z member encryption metadata is missing.")
        raw_mode = entry["Encrypted"].strip()
        if raw_mode not in {"+", "-"}:
            raise ValueError("Backup bundle 7z member encryption metadata is malformed.")
        observed.add(raw_mode == "+")
    if len(observed) > 1:
        raise ValueError("Backup bundle 7z archive mixes encrypted and plaintext members.")
    if not observed:
        raise ValueError("Backup bundle 7z archive contains no regular members.")
    return observed.pop()


def _is_7z_directory_entry(backup_module: ModuleType, entry: dict[str, str]) -> bool:
    return entry.get("Folder") == "+" or entry.get("Attributes", "").startswith("D")


def _validate_extracted_7z_tree(
    backup_module: ModuleType,
    cls,
    extract_dir: Path,
    listed_entries: list[dict[str, str]],
) -> None:
    expected_sizes = {
        cls._normalize_archive_member_path(entry["Path"]): int(entry["Size"])
        for entry in listed_entries
        if not cls._is_7z_directory_entry(entry)
    }
    expected_directories = {
        cls._normalize_archive_member_path(entry["Path"])
        for entry in listed_entries
        if cls._is_7z_directory_entry(entry)
    }
    actual_sizes: dict[str, int] = {}
    actual_directories: set[str] = set()
    for root, directories, filenames in os.walk(extract_dir, followlinks=False):
        root_path = Path(root)
        for directory in directories:
            directory_path = root_path / directory
            if directory_path.is_symlink():
                raise ValueError("Backup bundle 7z archive extracted an unsupported link.")
            actual_directories.add(
                cls._normalize_archive_member_path(
                    directory_path.relative_to(extract_dir).as_posix()
                )
            )
        for filename in filenames:
            file_path = root_path / filename
            if file_path.is_symlink() or not file_path.is_file():
                raise ValueError("Backup bundle 7z archive extracted an unsupported file type.")
            relative = cls._normalize_archive_member_path(
                file_path.relative_to(extract_dir).as_posix()
            )
            actual_sizes[relative] = file_path.stat().st_size
    if actual_sizes != expected_sizes:
        raise ValueError("Backup bundle 7z extracted members do not match its listing.")
    if not expected_directories.issubset(actual_directories):
        raise ValueError("Backup bundle 7z extracted directories do not match its listing.")


def _seven_zip_listed_entries(backup_module: ModuleType, output: str, archive_path: Path) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            if current:
                entries.append(current)
                current = {}
            continue
        if " = " not in line:
            continue
        key, value = line.split(" = ", 1)
        if key in current:
            raise ValueError("Backup bundle 7z listing contains duplicate metadata keys.")
        current[key] = value
    if current:
        entries.append(current)

    archive_names = {str(archive_path), archive_path.name}
    return [
        entry
        for entry in entries
        if entry.get("Path")
        and not (
            entry["Path"] in archive_names
            and entry.get("Type") == "7z"
        )
    ]


def _run_7z_command(
    backup_module: ModuleType,
    self,
    args: list[str],
    *,
    cwd: Path | None = None,
    passphrase: str | None = None,
    timeout_seconds: float | None = None,
) -> subprocess.CompletedProcess[str]:
    timeout = backup_module.SEVEN_ZIP_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    try:
        command = [backup_module.SEVEN_ZIP_BINARY, *args]
        if passphrase is not None:
            return self._run_7z_prompt_command(
                command,
                cwd=cwd,
                passphrase=passphrase,
                timeout_seconds=timeout,
            )
        process = subprocess.Popen(
            command,
            cwd=str(cwd) if cwd is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if process.stdout is None or process.stderr is None:
            process.kill()
            raise ValueError("Portable 7z backup command output could not be captured.")

        overflow = threading.Event()
        read_errors: list[Exception] = []
        stdout_content = bytearray()
        stderr_content = bytearray()

        def read_bounded(stream: Any, target: bytearray) -> None:
            try:
                while True:
                    remaining = backup_module.MAX_7Z_COMMAND_OUTPUT_BYTES - len(target)
                    chunk = stream.read(min(backup_module.ARCHIVE_READ_CHUNK_BYTES, remaining + 1))
                    if not chunk:
                        return
                    target.extend(chunk)
                    if len(target) > backup_module.MAX_7Z_COMMAND_OUTPUT_BYTES:
                        overflow.set()
                        process.kill()
                        return
            except Exception as exc:  # pragma: no cover - OS pipe failures are rare
                read_errors.append(exc)
                process.kill()

        stdout_thread = threading.Thread(
            target=read_bounded,
            args=(process.stdout, stdout_content),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=read_bounded,
            args=(process.stderr, stderr_content),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise
        finally:
            stdout_thread.join()
            stderr_thread.join()
            process.stdout.close()
            process.stderr.close()

        if overflow.is_set():
            raise ValueError("Portable 7z backup command output exceeded its byte limit.")
        if read_errors:
            raise ValueError("Portable 7z backup command output could not be read.") from read_errors[0]
        return subprocess.CompletedProcess(
            command,
            returncode,
            stdout=stdout_content.decode("utf-8", errors="replace"),
            stderr=stderr_content.decode("utf-8", errors="replace"),
        )
    except FileNotFoundError as exc:
        raise ValueError(
            "Portable 7z backup support requires the '7z' command inside the container image."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Portable 7z backup operation timed out.") from exc


def _run_7z_prompt_command(
    backup_module: ModuleType,
    command: list[str],
    *,
    cwd: Path | None,
    passphrase: str,
    timeout_seconds: float | None = None,
) -> subprocess.CompletedProcess[str]:
    timeout = backup_module.SEVEN_ZIP_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    if "\n" in passphrase or "\r" in passphrase:
        raise ValueError("Portable 7z backup passphrases cannot contain line breaks.")
    try:
        import errno
        import pty
        import termios
    except ImportError as exc:  # pragma: no cover - production images are Linux-based.
        raise ValueError("Portable encrypted 7z backup support requires a POSIX terminal.") from exc

    master_fd, slave_fd = pty.openpty()
    process: subprocess.Popen[bytes] | None = None
    output = bytearray()
    prompt_seen = False
    password_sent = False
    try:
        # A fresh, echo-enabled PTY makes the child's no-echo state a real
        # transition, not an inherited setting. Native 7-Zip prints its
        # prompt BEFORE tcsetattr(TCSAFLUSH); writing on prompt alone can
        # both echo the secret and have that flush discard it. Do not change
        # terminal flags here: only the child can establish input readiness.
        if not termios.tcgetattr(slave_fd)[3] & termios.ECHO:
            raise ValueError("Portable 7z password terminal readiness is unsupported.")
        process = subprocess.Popen(
            command,
            cwd=str(cwd) if cwd is not None else None,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
        )
        os.close(slave_fd)
        slave_fd = -1
        deadline = time.monotonic() + timeout
        poller = select.poll()
        poller.register(master_fd, select.POLLIN | select.POLLHUP | select.POLLERR)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                process.wait()
                raise subprocess.TimeoutExpired(command, timeout)
            ready = bool(poller.poll(max(1, int(min(remaining, 0.1) * 1000))))
            if ready:
                try:
                    chunk = os.read(master_fd, backup_module.ARCHIVE_READ_CHUNK_BYTES)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        break
                    raise
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > backup_module.MAX_7Z_COMMAND_OUTPUT_BYTES:
                    process.kill()
                    process.wait()
                    raise ValueError("Portable 7z backup command output exceeded its byte limit.")
                prompt_seen = prompt_seen or b"Enter password" in output
            # Terminal changes need not produce readable output. Check on
            # every bounded poll, using the existing operation deadline.
            if prompt_seen and not password_sent and process.poll() is None:
                if not termios.tcgetattr(master_fd)[3] & termios.ECHO:
                    password = passphrase.encode("utf-8") + b"\n"
                    if os.write(master_fd, password) != len(password):
                        raise ValueError("Portable 7z password delivery was incomplete.")
                    password_sent = True
            if process.poll() is not None and not ready:
                break
        returncode = process.wait(timeout=max(deadline - time.monotonic(), 0.001))
        if prompt_seen and not password_sent and returncode == 0:
            raise ValueError("Portable 7z password terminal readiness was not established.")
        return subprocess.CompletedProcess(
            command,
            returncode,
            stdout=output.decode("utf-8", errors="replace"),
            stderr="",
        )
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        if slave_fd >= 0:
            os.close(slave_fd)
        os.close(master_fd)


def _raise_for_7z_failure(
    backup_module: ModuleType,
    result: subprocess.CompletedProcess[str],
    message: str,
    *,
    passphrase: str | None,
    reading_archive: bool = False,
) -> None:
    if result.returncode == 0:
        return
    combined_output = "\n".join(
        part for part in (result.stdout.strip(), result.stderr.strip()) if part
    )
    if "Wrong password?" in combined_output or "Cannot open encrypted archive" in combined_output:
        if passphrase:
            raise ValueError(backup_module.WRONG_PASSPHRASE_MESSAGE)
        raise ValueError(backup_module.PASSPHRASE_REQUIRED_MESSAGE)
    if reading_archive and (
        "Headers Error" in combined_output or "Can't open as archive" in combined_output
    ):
        raise ValueError("This backup file is damaged (the 7-Zip archive could not be read).")
    # Raw 7-Zip output can echo archive member names; keep it in the log only.
    backup_module.logger.warning(
        "7-Zip exited with %s: %s",
        result.returncode,
        combined_output[:2000].replace(passphrase, "***") if passphrase else combined_output[:2000],
    )
    raise ValueError(f"{message} The 7-Zip step failed (exit {result.returncode}).")
