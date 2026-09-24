"""Backup archive transport: filesystem end to end, other providers against fakes.

No real network and no real mounts: FTP, SFTP, SMB, and S3 use in-memory fakes
of the client libraries, and NFS stubs mount/umount/ismount while writing to a
real temporary directory that stands in for the mounted export.
"""

from __future__ import annotations

import ftplib
import hashlib
import io
import os
import shutil
import stat
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import paramiko

from history_service.backup_archive import transport
from history_service.backup_archive.settings import (
    ArchiveConfigError,
    ArchiveTargetSettings,
    read_secret_file,
)
from history_service.backup_archive.transport import (
    ArchiveTarget,
    ArchiveVerificationError,
    DependencyMissingError,
    NfsUnmountError,
    RemoteObject,
    StoredObject,
    open_target,
    transport_encrypted,
    validate_object_name,
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _TempCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def secret(self, name: str, value: str, mode: int = 0o600) -> str:
        path = self.tmp / name
        path.write_text(value + "\n", encoding="utf-8")
        os.chmod(path, mode)
        return str(path)

    def source(self, data: bytes, name: str = "backup.tar.zst") -> Path:
        path = self.tmp / "src" / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(data)
        return path


# --------------------------------------------------------------------------
# Names, settings, secrets
# --------------------------------------------------------------------------


class NameValidationTests(unittest.TestCase):
    def test_accepts_nested_relative_names(self) -> None:
        self.assertEqual(validate_object_name("config/2026/backup-1.tar.zst"), ("config", "2026", "backup-1.tar.zst"))

    def test_rejects_unsafe_names(self) -> None:
        for name in [
            "",
            "/etc/passwd",
            "../escape",
            "a/../b",
            "a/./b",
            "a//b",
            "a\\b",
            "nul\x00byte",
            ".hidden",
            "space name",
            "x" * 2000,
            "file.partial",
            "/".join(["d"] * 20),
        ]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_object_name(name)


class SettingsTests(_TempCase):
    def test_inline_credentials_are_rejected(self) -> None:
        for key in ["password", "secret_access_key", "access_key_id"]:
            with self.subTest(key=key), self.assertRaisesRegex(ArchiveConfigError, "_file"):
                ArchiveTargetSettings.from_mapping(
                    {"target_id": "t1", "provider": "ftp", "root": "backups", "host": "ftp.example.test", key: "x"}
                )

    def test_unknown_setting_is_rejected(self) -> None:
        with self.assertRaisesRegex(ArchiveConfigError, "Unknown"):
            ArchiveTargetSettings.from_mapping({"target_id": "t1", "provider": "filesystem", "root": "/b", "bogus": 1})

    def test_root_must_be_app_owned_directory(self) -> None:
        for root in ["", "/", "../x", "a/../b", "a\\b"]:
            with self.subTest(root=root), self.assertRaises(ArchiveConfigError):
                ArchiveTargetSettings(target_id="t1", provider="filesystem", root=root)

    def test_provider_specific_requirements(self) -> None:
        with self.assertRaisesRegex(ArchiveConfigError, "known_hosts"):
            ArchiveTargetSettings(
                target_id="t", provider="sftp", root="b", host="h.example.test", username="u", password_file="/p"
            )
        with self.assertRaisesRegex(ArchiveConfigError, "export path"):
            ArchiveTargetSettings(target_id="t", provider="nfs", root="b", host="h.example.test", export_path="rel")
        with self.assertRaisesRegex(ArchiveConfigError, "mount options"):
            ArchiveTargetSettings(
                target_id="t",
                provider="nfs",
                root="b",
                host="h.example.test",
                export_path="/export",
                mount_options="vers=4; rm -rf /",
            )
        with self.assertRaisesRegex(ArchiveConfigError, "S3"):
            ArchiveTargetSettings(target_id="t", provider="s3", root="b", bucket="bucket-one")
        with self.assertRaisesRegex(ArchiveConfigError, "Unsupported"):
            ArchiveTargetSettings(target_id="t", provider="scp", root="b", host="h.example.test")

    def test_repr_omits_secret_paths(self) -> None:
        settings = ArchiveTargetSettings(
            target_id="t", provider="ftp", root="b", host="ftp.example.test", password_file="/run/secrets/ftp-pass"
        )
        self.assertNotIn("ftp-pass", repr(settings))

    def test_read_secret_file_requires_private_regular_file(self) -> None:
        good = self.secret("good", "s3cret")
        self.assertEqual(read_secret_file(good, "password"), "s3cret")
        loose = self.secret("loose", "s3cret", mode=0o644)
        with self.assertRaisesRegex(ArchiveConfigError, "private regular file"):
            read_secret_file(loose, "password")
        link = self.tmp / "link"
        link.symlink_to(good)
        with self.assertRaisesRegex(ArchiveConfigError, "private regular file"):
            read_secret_file(str(link), "password")
        with self.assertRaisesRegex(ArchiveConfigError, "private regular file"):
            read_secret_file(str(self.tmp), "password")
        empty = self.secret("empty", "")
        with self.assertRaisesRegex(ArchiveConfigError, "empty"):
            read_secret_file(empty, "password")

    def test_secret_value_never_in_error_message(self) -> None:
        loose = self.secret("loose", "TOPSECRETVALUE", mode=0o640)
        with self.assertRaises(ArchiveConfigError) as caught:
            read_secret_file(loose, "password")
        self.assertNotIn("TOPSECRETVALUE", str(caught.exception))

    def test_transport_encrypted_labels(self) -> None:
        def make(**kw):
            return ArchiveTargetSettings(target_id="t", root="b", **kw)

        self.assertFalse(transport_encrypted(make(provider="ftp", host="h.example.test")))
        self.assertTrue(transport_encrypted(make(provider="ftp", host="h.example.test", use_tls=True)))
        self.assertTrue(
            transport_encrypted(
                make(provider="sftp", host="h.example.test", username="u", password_file="/p", known_hosts_path="/k")
            )
        )
        self.assertFalse(transport_encrypted(make(provider="smb", host="h.example.test", share="s")))
        self.assertTrue(transport_encrypted(make(provider="smb", host="h.example.test", share="s", smb_encrypt=True)))
        self.assertFalse(transport_encrypted(make(provider="nfs", host="h.example.test", export_path="/e")))
        self.assertTrue(
            transport_encrypted(
                make(provider="nfs", host="h.example.test", export_path="/e", mount_options="vers=4.2,sec=krb5p")
            )
        )
        s3 = {"bucket": "bucket-one", "access_key_id_file": "/a", "secret_access_key_file": "/b"}
        self.assertTrue(transport_encrypted(make(provider="s3", **s3)))
        self.assertFalse(transport_encrypted(make(provider="s3", endpoint_url="http://minio.example.test", **s3)))


# --------------------------------------------------------------------------
# Filesystem: real temp directories end to end
# --------------------------------------------------------------------------


class FilesystemTargetTests(_TempCase):
    def settings(self) -> ArchiveTargetSettings:
        return ArchiveTargetSettings(target_id="local", provider="filesystem", root=str(self.tmp / "archive"))

    def test_put_list_delete_round_trip(self) -> None:
        data = os.urandom(3 * transport.CHUNK_SIZE + 17)
        src = self.source(data)
        with open_target(self.settings()) as target:
            self.assertIsInstance(target, ArchiveTarget)
            self.assertTrue(target.transport_encrypted)
            stored = target.put(src, "full/backup-1.tar.zst")
            self.assertEqual(stored, StoredObject("full/backup-1.tar.zst", len(data), _sha(data), True))
            self.assertEqual((self.tmp / "archive/full/backup-1.tar.zst").read_bytes(), data)
            self.assertEqual(list((self.tmp / "archive/full").iterdir()), [self.tmp / "archive/full/backup-1.tar.zst"])
            target.put(src, "config/c-1.tar")
            listed = target.list()
            self.assertEqual([item.name for item in listed], ["config/c-1.tar", "full/backup-1.tar.zst"])
            self.assertTrue(all(isinstance(item, RemoteObject) and item.modified.tzinfo for item in listed))
            self.assertEqual([item.name for item in target.list("full/")], ["full/backup-1.tar.zst"])
            target.delete("full/backup-1.tar.zst")
            target.delete("full/backup-1.tar.zst")  # idempotent
            self.assertEqual([item.name for item in target.list()], ["config/c-1.tar"])

    def test_put_replaces_existing_atomically(self) -> None:
        with open_target(self.settings()) as target:
            target.put(self.source(b"old"), "a.bin")
            target.put(self.source(b"new-content"), "a.bin")
        self.assertEqual((self.tmp / "archive/a.bin").read_bytes(), b"new-content")

    def test_failed_verification_leaves_no_partial_and_keeps_existing(self) -> None:
        with open_target(self.settings()) as target:
            target.put(self.source(b"known good"), "a.bin")
            with mock.patch.object(transport, "_hash_stream", return_value=(1, "0" * 64)):
                with self.assertRaises(ArchiveVerificationError):
                    target.put(self.source(b"replacement"), "a.bin")
        self.assertEqual((self.tmp / "archive/a.bin").read_bytes(), b"known good")
        self.assertEqual(sorted(p.name for p in (self.tmp / "archive").iterdir()), ["a.bin"])

    def test_confinement_rejects_symlinked_parent_and_ignores_foreign_entries(self) -> None:
        outside = self.tmp / "outside"
        outside.mkdir()
        root = self.tmp / "archive"
        root.mkdir()
        (root / "escape").symlink_to(outside)
        (root / "stale.bin.partial").write_bytes(b"x")
        (root / "linked-file").symlink_to(self.source(b"s"))
        with open_target(self.settings()) as target:
            with self.assertRaisesRegex(Exception, "plain directory"):
                target.put(self.source(b"x"), "escape/file.bin")
            with self.assertRaises(ValueError):
                target.put(self.source(b"x"), "../outside/file.bin")
            with self.assertRaises(ValueError):
                target.delete("../outside")
            self.assertEqual(target.list(), [])
        self.assertEqual(list(outside.iterdir()), [])

    def test_delete_refuses_directory(self) -> None:
        with open_target(self.settings()) as target:
            target.put(self.source(b"x"), "d/file.bin")
            with self.assertRaisesRegex(Exception, "directory"):
                target.delete("d")

    def test_source_must_be_regular_file(self) -> None:
        link = self.tmp / "link"
        link.symlink_to(self.source(b"x"))
        with open_target(self.settings()) as target:
            with self.assertRaisesRegex(Exception, "regular file"):
                target.put(link, "a.bin")
            with self.assertRaisesRegex(Exception, "regular file"):
                target.put(self.tmp, "a.bin")

    def test_probe_writes_verifies_and_cleans_up(self) -> None:
        with open_target(self.settings()) as target:
            result = target.test()
        self.assertTrue(result["ok"], result)
        self.assertIn("readback matched", result["detail"])
        self.assertIsInstance(result["duration_ms"], int)
        self.assertEqual(list((self.tmp / "archive").iterdir()), [])

    def test_probe_reports_failure_without_raising(self) -> None:
        blocker = self.tmp / "file"
        blocker.write_bytes(b"x")
        settings = ArchiveTargetSettings(target_id="local", provider="filesystem", root=str(blocker / "sub"))
        with open_target(settings) as target:
            result = target.test()
        self.assertFalse(result["ok"])
        self.assertTrue(result["detail"])


# --------------------------------------------------------------------------
# FTP (fake ftplib client; pyftpdlib is not a dependency)
# --------------------------------------------------------------------------


class FakeFTP:
    instances: list[FakeFTP] = []

    def __init__(self, *args, **kwargs) -> None:
        self.files: dict[str, bytes] = {}
        self.dirs: set[str] = {"/"}
        self.cwd_path = "/"
        self.log: list[str] = []
        self.corrupt_size = False
        self.deny_mkd: set[str] = set()
        FakeFTP.instances.append(self)

    def connect(self, host, port, timeout=None):
        self.log.append(f"connect {host}:{port}")

    def login(self, user, password):
        self.log.append(f"login {user}")
        self.password = password

    def prot_p(self):
        self.log.append("prot_p")

    def quit(self):
        self.log.append("quit")

    def close(self):
        self.log.append("close")

    def pwd(self):
        return self.cwd_path

    def cwd(self, path):
        if path not in self.dirs:
            raise ftplib.error_perm("550 no such directory")
        self.cwd_path = path

    def mkd(self, path):
        if path in self.dirs or path in self.files:
            raise ftplib.error_perm("550 exists")
        if path in self.deny_mkd:
            raise ftplib.error_perm("550 permission denied")
        self.dirs.add(path)
        return path

    def storbinary(self, cmd, fp, blocksize=8192):
        verb, path = cmd.split(" ", 1)
        assert verb == "STOR"
        chunks = []
        while True:
            chunk = fp.read(blocksize)
            if not chunk:
                break
            self.log.append(f"chunk {len(chunk)}")
            chunks.append(chunk)
        self.files[path] = b"".join(chunks)

    def voidcmd(self, cmd):
        return "200"

    def size(self, path):
        if path not in self.files:
            raise ftplib.error_perm("550 not found")
        return len(self.files[path]) + (1 if self.corrupt_size else 0)

    def rename(self, src, dst):
        self.log.append(f"rename {src} -> {dst}")
        self.files[dst] = self.files.pop(src)

    def delete(self, path):
        if path not in self.files:
            raise ftplib.error_perm("550 not found")
        del self.files[path]

    def mlsd(self, path, facts=()):
        if path not in self.dirs:
            raise ftplib.error_perm("550 not found")
        prefix = path.rstrip("/") + "/"
        seen = set()
        for directory in sorted(self.dirs):
            if directory.startswith(prefix) and "/" not in directory[len(prefix) :]:
                seen.add(directory)
                yield directory[len(prefix) :], {"type": "dir"}
        for name, data in sorted(self.files.items()):
            if name.startswith(prefix) and "/" not in name[len(prefix) :]:
                yield name[len(prefix) :], {"type": "file", "size": str(len(data)), "modify": "20260924101500"}


class FakeFTPTLS(FakeFTP):
    pass


class FtpTargetTests(_TempCase):
    def setUp(self) -> None:
        super().setUp()
        FakeFTP.instances = []
        patcher_ftp = mock.patch.object(transport.ftplib, "FTP", FakeFTP)
        patcher_tls = mock.patch.object(transport.ftplib, "FTP_TLS", FakeFTPTLS)
        patcher_ftp.start()
        patcher_tls.start()
        self.addCleanup(patcher_ftp.stop)
        self.addCleanup(patcher_tls.stop)

    def settings(self, **kw) -> ArchiveTargetSettings:
        return ArchiveTargetSettings(
            target_id="ftp1",
            provider="ftp",
            root="/pub/jbod-ui",
            host="ftp.example.test",
            username="backup",
            password_file=self.secret("ftp-pass", "pw"),
            **kw,
        )

    def test_plain_ftp_put_uses_partial_then_rename_and_streams(self) -> None:
        data = os.urandom(2 * transport.CHUNK_SIZE + 5)
        with open_target(self.settings()) as target:
            self.assertFalse(target.transport_encrypted)
            stored = target.put(self.source(data), "full/b1.tar.zst")
            listed = target.list()
            target.delete("full/b1.tar.zst")
            target.delete("full/b1.tar.zst")
        ftp = FakeFTP.instances[0]
        self.assertEqual(stored, StoredObject("full/b1.tar.zst", len(data), _sha(data), True))
        self.assertIn("rename /pub/jbod-ui/full/b1.tar.zst.partial -> /pub/jbod-ui/full/b1.tar.zst", ftp.log)
        self.assertGreaterEqual(sum(1 for line in ftp.log if line.startswith("chunk")), 3)
        self.assertEqual(
            listed,
            [RemoteObject("full/b1.tar.zst", len(data), datetime(2026, 9, 24, 10, 15, tzinfo=timezone.utc))],
        )
        self.assertNotIn("prot_p", ftp.log)
        self.assertEqual(ftp.log[-1], "quit")
        self.assertEqual(ftp.files, {})

    def test_ftps_calls_prot_p_and_is_encrypted(self) -> None:
        with open_target(self.settings(use_tls=True)) as target:
            self.assertTrue(target.transport_encrypted)
        self.assertIsInstance(FakeFTP.instances[0], FakeFTPTLS)
        self.assertIn("prot_p", FakeFTP.instances[0].log)

    def test_size_mismatch_removes_partial_and_raises(self) -> None:
        with open_target(self.settings()) as target:
            FakeFTP.instances[0].corrupt_size = True
            with self.assertRaises(ArchiveVerificationError):
                target.put(self.source(b"data"), "a.bin")
        self.assertEqual(FakeFTP.instances[0].files, {})

    def test_interrupted_transfer_removes_partial(self) -> None:
        with open_target(self.settings()) as target:
            ftp = FakeFTP.instances[0]

            def broken_stor(cmd, fp, blocksize=8192):
                ftp.files[cmd.split(" ", 1)[1]] = fp.read(3)
                raise ftplib.error_temp("426 connection closed; transfer aborted")

            with mock.patch.object(ftp, "storbinary", side_effect=broken_stor):
                with self.assertRaises(ftplib.error_temp):
                    target.put(self.source(b"payload"), "a.bin")
        self.assertEqual(FakeFTP.instances[0].files, {})

    def test_mkdir_permission_error_is_not_swallowed(self) -> None:
        with open_target(self.settings()) as target:
            FakeFTP.instances[0].deny_mkd.add("/pub/jbod-ui/locked")
            with self.assertRaisesRegex(Exception, "could not create directory"):
                target.put(self.source(b"x"), "locked/a.bin")

    def test_probe(self) -> None:
        with open_target(self.settings()) as target:
            result = target.test()
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["transport_encrypted"])
        self.assertEqual(FakeFTP.instances[0].files, {})


# --------------------------------------------------------------------------
# SFTP (fake paramiko client)
# --------------------------------------------------------------------------


class _FakeSftpFile(io.BytesIO):
    def __init__(self, store, path, mode):
        self._store, self._path, self._mode = store, path, mode
        super().__init__(store.files[path] if "r" in mode else b"")
        self.write_calls = 0

    def set_pipelined(self, value=True):
        pass

    def prefetch(self, size=None):
        pass

    def write(self, data):
        self.write_calls += 1
        self._store.write_sizes.append(len(data))
        return super().write(data)

    def close(self):
        if "w" in self._mode and not self.closed:
            data = self.getvalue()
            if self._store.corrupt:
                data = data[:-1] + b"?"
            self._store.files[self._path] = data
        super().close()


class FakeSFTP:
    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.dirs: set[str] = {"/"}
        self.log: list[str] = []
        self.write_sizes: list[int] = []
        self.corrupt = False
        self.mkdir_error: OSError | None = None
        self.closed = False

    def stat(self, path):
        if path in self.dirs:
            return paramiko.SFTPAttributes.from_stat(os.stat_result((stat.S_IFDIR | 0o755,) + (0,) * 9))
        if path in self.files:
            return paramiko.SFTPAttributes.from_stat(os.stat_result((stat.S_IFREG | 0o644,) + (0,) * 9))
        raise FileNotFoundError(errno_enoent(), "No such file")

    def mkdir(self, path):
        if self.mkdir_error:
            raise self.mkdir_error
        self.dirs.add(path)

    def open(self, path, mode):
        return _FakeSftpFile(self, path, mode)

    def posix_rename(self, src, dst):
        self.log.append(f"posix_rename {src} -> {dst}")
        self.files[dst] = self.files.pop(src)

    def rename(self, src, dst):
        raise AssertionError("posix_rename should be used")

    def remove(self, path):
        if path not in self.files:
            raise FileNotFoundError(errno_enoent(), "No such file")
        del self.files[path]

    def listdir_attr(self, path):
        if path not in self.dirs:
            raise FileNotFoundError(errno_enoent(), "No such file")
        prefix = path.rstrip("/") + "/"
        out = []
        for directory in self.dirs:
            if directory.startswith(prefix) and "/" not in directory[len(prefix) :]:
                attr = paramiko.SFTPAttributes()
                attr.filename = directory[len(prefix) :]
                attr.st_mode = stat.S_IFDIR | 0o755
                out.append(attr)
        for name, data in self.files.items():
            if name.startswith(prefix) and "/" not in name[len(prefix) :]:
                attr = paramiko.SFTPAttributes()
                attr.filename = name[len(prefix) :]
                attr.st_mode = stat.S_IFREG | 0o644
                attr.st_size = len(data)
                attr.st_mtime = 1_790_000_000
                out.append(attr)
        return out

    def close(self):
        self.closed = True


def errno_enoent() -> int:
    import errno

    return errno.ENOENT


class FakeSSHClient:
    instances: list[FakeSSHClient] = []

    def __init__(self):
        self.host_keys = paramiko.HostKeys()
        self.policy = None
        self.sftp = FakeSFTP()
        self.connect_kwargs = None
        self.closed = False
        FakeSSHClient.instances.append(self)

    def load_host_keys(self, path):
        self.host_keys.load(path)

    def get_host_keys(self):
        return self.host_keys

    def save_host_keys(self, path):
        self.host_keys.save(path)

    def set_missing_host_key_policy(self, policy):
        self.policy = policy

    def connect(self, **kwargs):
        self.connect_kwargs = kwargs
        hostname = kwargs["hostname"] if kwargs.get("port", 22) == 22 else f"[{kwargs['hostname']}]:{kwargs['port']}"
        known = self.host_keys.lookup(hostname)
        if known is None or SERVER_KEY.get_name() not in known:
            self.policy.missing_host_key(self, hostname, SERVER_KEY)
        elif known[SERVER_KEY.get_name()] != SERVER_KEY:
            raise paramiko.BadHostKeyException(hostname, SERVER_KEY, known[SERVER_KEY.get_name()])

    def open_sftp(self):
        return self.sftp

    def _log(self, level, message):
        pass

    def close(self):
        self.closed = True


SERVER_KEY = paramiko.ECDSAKey.generate()
OTHER_KEY = paramiko.ECDSAKey.generate()


class SftpTargetTests(_TempCase):
    def setUp(self) -> None:
        super().setUp()
        FakeSSHClient.instances = []
        patcher = mock.patch.object(transport.paramiko, "SSHClient", FakeSSHClient)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.known_hosts = self.tmp / "known_hosts"

    def settings(self, **kw) -> ArchiveTargetSettings:
        values = {
            "target_id": "sftp1",
            "provider": "sftp",
            "root": "/srv/backups/jbod-ui",
            "host": "sftp.example.test",
            "username": "backup",
            "password_file": self.secret("sftp-pass", "pw"),
            "known_hosts_path": str(self.known_hosts),
        }
        values.update(kw)
        return ArchiveTargetSettings(**values)

    def pin_key(self, key=SERVER_KEY) -> None:
        keys = paramiko.HostKeys()
        keys.add("sftp.example.test", key.get_name(), key)
        keys.save(str(self.known_hosts))

    def test_unknown_host_key_is_rejected_by_default(self) -> None:
        self.known_hosts.write_text("")
        with self.assertRaises(paramiko.SSHException):
            with open_target(self.settings()):
                pass
        client = FakeSSHClient.instances[0]
        self.assertIsInstance(client.policy, paramiko.RejectPolicy)
        self.assertTrue(client.closed)
        self.assertFalse(client.connect_kwargs["look_for_keys"])
        self.assertFalse(client.connect_kwargs["allow_agent"])

    def test_missing_known_hosts_file_fails_closed(self) -> None:
        with self.assertRaisesRegex(ArchiveConfigError, "known_hosts"):
            with open_target(self.settings()):
                pass

    def test_changed_host_key_is_rejected_even_with_tofu(self) -> None:
        self.pin_key(OTHER_KEY)
        with self.assertRaises(paramiko.BadHostKeyException):
            with open_target(self.settings(trust_on_first_use=True)):
                pass

    def test_tofu_pins_the_first_key(self) -> None:
        with open_target(self.settings(trust_on_first_use=True)):
            pass
        pinned = paramiko.HostKeys(str(self.known_hosts))
        self.assertEqual(pinned.lookup("sftp.example.test")[SERVER_KEY.get_name()], SERVER_KEY)

    def test_put_verifies_by_readback_and_renames(self) -> None:
        self.pin_key()
        data = os.urandom(2 * transport.CHUNK_SIZE + 3)
        with open_target(self.settings()) as target:
            self.assertTrue(target.transport_encrypted)
            stored = target.put(self.source(data), "full/b1.tar.zst")
            listed = target.list()
            target.delete("full/b1.tar.zst")
            target.delete("full/b1.tar.zst")
        client = FakeSSHClient.instances[0]
        self.assertEqual(stored, StoredObject("full/b1.tar.zst", len(data), _sha(data), True))
        self.assertEqual(
            client.sftp.log,
            ["posix_rename /srv/backups/jbod-ui/full/b1.tar.zst.partial -> /srv/backups/jbod-ui/full/b1.tar.zst"],
        )
        self.assertLessEqual(max(client.sftp.write_sizes), transport.CHUNK_SIZE)
        self.assertEqual([item.name for item in listed], ["full/b1.tar.zst"])
        self.assertEqual(client.sftp.files, {})
        self.assertTrue(client.sftp.closed and client.closed)

    def test_corrupt_readback_removes_partial(self) -> None:
        self.pin_key()
        with open_target(self.settings()) as target:
            FakeSSHClient.instances[0].sftp.corrupt = True
            with self.assertRaises(ArchiveVerificationError):
                target.put(self.source(b"payload"), "a.bin")
        self.assertEqual(FakeSSHClient.instances[0].sftp.files, {})

    def test_mkdir_permission_error_surfaces(self) -> None:
        self.pin_key()
        with self.assertRaises(PermissionError):
            with mock.patch.object(FakeSFTP, "mkdir", side_effect=PermissionError(13, "Permission denied")):
                with open_target(self.settings()):
                    pass


# --------------------------------------------------------------------------
# SMB (fake smbclient module)
# --------------------------------------------------------------------------


class FakeSmbClient(types.ModuleType):
    def __init__(self):
        super().__init__("smbclient")
        self.files: dict[str, bytes] = {}
        self.dirs: set[str] = set()
        self.log: list[str] = []
        self.corrupt = False

    def register_session(self, server, **kwargs):
        self.log.append(f"register {server} encrypt={kwargs.get('encrypt')}")
        self.session_kwargs = kwargs

    def delete_session(self, server, port=445):
        self.log.append(f"delete_session {server}")

    def makedirs(self, path, exist_ok=False):
        parts = path.split("\\")
        for index in range(4, len(parts) + 1):
            self.dirs.add("\\".join(parts[:index]))

    def stat(self, path):
        if path in self.dirs:
            return os.stat_result((stat.S_IFDIR | 0o755,) + (0,) * 9)
        raise FileNotFoundError(errno_enoent(), "missing")

    @contextmanager
    def open_file(self, path, mode="rb"):
        if "w" in mode:
            buffer = io.BytesIO()
            yield buffer
            data = buffer.getvalue()
            self.files[path] = data[:-1] + b"?" if self.corrupt and data else data
        else:
            yield io.BytesIO(self.files[path])

    def replace(self, src, dst):
        self.log.append(f"replace {src} -> {dst}")
        self.files[dst] = self.files.pop(src)

    def remove(self, path):
        if path not in self.files:
            raise FileNotFoundError(errno_enoent(), "missing")
        del self.files[path]

    def scandir(self, path):
        prefix = path + "\\"
        entries = []
        for directory in self.dirs:
            if directory.startswith(prefix) and "\\" not in directory[len(prefix) :]:
                entries.append(_SmbEntry(directory[len(prefix) :], True, 0))
        for name, data in self.files.items():
            if name.startswith(prefix) and "\\" not in name[len(prefix) :]:
                entries.append(_SmbEntry(name[len(prefix) :], False, len(data)))
        return iter(entries)


class _SmbEntry:
    def __init__(self, name, is_dir, size):
        self.name, self._dir, self._size = name, is_dir, size

    def is_dir(self):
        return self._dir

    def is_file(self):
        return not self._dir

    def stat(self):
        return os.stat_result((stat.S_IFREG | 0o644, 0, 0, 0, 0, 0, self._size, 0, 1_790_000_000, 0))


class SmbTargetTests(_TempCase):
    def settings(self, **kw) -> ArchiveTargetSettings:
        return ArchiveTargetSettings(
            target_id="smb1",
            provider="smb",
            root="jbod-ui/archive",
            host="nas.example.test",
            share="backups",
            username="backup",
            domain="EXAMPLE",
            password_file=self.secret("smb-pass", "pw"),
            **kw,
        )

    def test_missing_dependency_has_install_hint(self) -> None:
        with mock.patch.dict(sys.modules, {"smbclient": None}):
            with self.assertRaisesRegex(DependencyMissingError, "install smbprotocol"):
                with open_target(self.settings()):
                    pass

    def test_put_list_delete(self) -> None:
        fake = FakeSmbClient()
        data = os.urandom(transport.CHUNK_SIZE + 9)
        with mock.patch.dict(sys.modules, {"smbclient": fake}):
            with open_target(self.settings(smb_encrypt=True)) as target:
                self.assertTrue(target.transport_encrypted)
                stored = target.put(self.source(data), "config/c1.tar")
                listed = target.list()
                target.delete("config/c1.tar")
                target.delete("config/c1.tar")
        self.assertEqual(stored, StoredObject("config/c1.tar", len(data), _sha(data), True))
        self.assertEqual(fake.session_kwargs["username"], "EXAMPLE\\backup")
        self.assertIn(
            "replace \\\\nas.example.test\\backups\\jbod-ui\\archive\\config\\c1.tar.partial -> "
            "\\\\nas.example.test\\backups\\jbod-ui\\archive\\config\\c1.tar",
            fake.log,
        )
        self.assertEqual([item.name for item in listed], ["config/c1.tar"])
        self.assertEqual(fake.log[-1], "delete_session nas.example.test")
        self.assertEqual(fake.files, {})

    def test_corrupt_readback_removes_partial(self) -> None:
        fake = FakeSmbClient()
        fake.corrupt = True
        with mock.patch.dict(sys.modules, {"smbclient": fake}):
            with open_target(self.settings()) as target:
                self.assertFalse(target.transport_encrypted)
                with self.assertRaises(ArchiveVerificationError):
                    target.put(self.source(b"payload"), "a.bin")
        self.assertEqual(fake.files, {})


# --------------------------------------------------------------------------
# NFS (stubbed mount/umount; a real temp dir stands in for the export)
# --------------------------------------------------------------------------


class FakeMounts:
    """Tracks which directories are 'mounted' and fails umount on request."""

    def __init__(self, mount_parent: Path, *, umount_failures: int = 0, lazy_works: bool = True) -> None:
        self.mounted: set[str] = set()
        self.commands: list[list[str]] = []
        self.umount_failures = umount_failures
        self.lazy_works = lazy_works
        self.mount_fails = False
        self.mount_parent = mount_parent

    def run(self, command, label):
        self.commands.append(list(command))
        binary = os.path.basename(command[0])
        if binary == "mount":
            if self.mount_fails:
                raise transport.ArchiveTransportError(f"{label} failed: access denied")
            self.mounted.add(command[-1])
            return
        if "-l" in command:
            if self.lazy_works:
                self.mounted.discard(command[-1])
                return
            raise transport.ArchiveTransportError(f"{label} failed: target is busy")
        if self.umount_failures > 0:
            self.umount_failures -= 1
            raise transport.ArchiveTransportError(f"{label} failed: target is busy")
        self.mounted.discard(command[-1])

    def ismount(self, path):
        return str(path) in self.mounted


class NfsTargetTests(_TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.mount_parent = self.tmp / "mnt"
        self.mount_parent.mkdir()
        mock.patch.object(transport.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}").start()
        mock.patch.object(transport.time, "sleep").start()
        self.addCleanup(mock.patch.stopall)

    def settings(self) -> ArchiveTargetSettings:
        return ArchiveTargetSettings(
            target_id="nfs1",
            provider="nfs",
            root="jbod-ui",
            host="nfs.example.test",
            export_path="/export/backups",
            mount_options="vers=4.2",
            mount_parent=str(self.mount_parent),
        )

    def use(self, mounts: FakeMounts) -> None:
        mock.patch.object(transport, "_run_mount_command", side_effect=mounts.run).start()
        mock.patch.object(transport, "_is_mounted", side_effect=mounts.ismount).start()

    def test_mounts_for_the_block_and_removes_empty_mount_point(self) -> None:
        mounts = FakeMounts(self.mount_parent)
        self.use(mounts)
        data = b"nfs payload"
        with open_target(self.settings()) as target:
            self.assertEqual(target.provider, "nfs")
            self.assertFalse(target.transport_encrypted)
            (mount_dir,) = mounts.mounted
            stored = target.put(self.source(data), "full/b1.tar")
            self.assertEqual((Path(mount_dir) / "jbod-ui/full/b1.tar").read_bytes(), data)
            self.assertEqual([item.name for item in target.list()], ["full/b1.tar"])
            # Simulate the real unmount hiding the export's files again.
            os.unlink(Path(mount_dir) / "jbod-ui/full/b1.tar")
            os.rmdir(Path(mount_dir) / "jbod-ui/full")
            os.rmdir(Path(mount_dir) / "jbod-ui")
        self.assertTrue(stored.verified)
        self.assertEqual(
            mounts.commands[0],
            ["/usr/bin/mount", "-t", "nfs", "-o", "vers=4.2", "nfs.example.test:/export/backups", mount_dir],
        )
        self.assertEqual(mounts.commands[1], ["/usr/bin/umount", mount_dir])
        self.assertFalse(os.path.exists(mount_dir))

    def test_failed_unmount_never_deletes_files_on_the_still_mounted_export(self) -> None:
        """Data-loss regression (gcs8/switch-explorer#179).

        Every umount attempt, including the lazy one, fails, so the export stays
        mounted. The export's contents must survive: no rmtree, not even an rmdir
        of the mount point, and the error must say so.
        """

        mounts = FakeMounts(self.mount_parent, umount_failures=99, lazy_works=False)
        self.use(mounts)
        rmtree_calls: list[tuple] = []
        real_rmtree = shutil.rmtree

        def recording_rmtree(*args, **kwargs):
            rmtree_calls.append(args)
            return real_rmtree(*args, **kwargs)

        precious = mount_dir = None
        with mock.patch("shutil.rmtree", side_effect=recording_rmtree):
            with self.assertRaises(NfsUnmountError) as caught:
                with open_target(self.settings()) as target:
                    (mount_dir,) = mounts.mounted
                    # Pre-existing data on the export that the app never wrote.
                    precious = Path(mount_dir) / "someone-elses-data.db"
                    precious.write_bytes(b"irreplaceable")
                    target.put(self.source(b"x"), "full/b1.tar")
        self.assertEqual(rmtree_calls, [])
        assert precious is not None and mount_dir is not None
        self.assertEqual(precious.read_bytes(), b"irreplaceable")
        self.assertTrue((Path(mount_dir) / "jbod-ui/full/b1.tar").is_file())
        self.assertIn("still mounted", str(caught.exception))
        umounts = [cmd for cmd in mounts.commands if cmd[0].endswith("umount")]
        self.assertEqual(len(umounts), transport.NFS_UMOUNT_ATTEMPTS + 1)
        self.assertEqual(umounts[-1], ["/usr/bin/umount", "-l", mount_dir])

    def test_retry_then_lazy_unmount_succeeds(self) -> None:
        mounts = FakeMounts(self.mount_parent, umount_failures=99, lazy_works=True)
        self.use(mounts)
        with open_target(self.settings()) as target:
            (mount_dir,) = mounts.mounted
            del target
        self.assertFalse(os.path.exists(mount_dir))
        self.assertEqual(mounts.commands[-1], ["/usr/bin/umount", "-l", mount_dir])

    def test_mount_point_with_leftover_content_is_left_in_place_not_recursed(self) -> None:
        # Unmount reports success, but the directory is not empty: rmdir fails
        # and the content is kept. Cleanup never recurses.
        mounts = FakeMounts(self.mount_parent)
        self.use(mounts)
        with open_target(self.settings()):
            (mount_dir,) = mounts.mounted
        leftover = Path(mount_dir)
        leftover.mkdir(exist_ok=True)
        (leftover / "keep").write_bytes(b"k")
        transport._unmount_nfs("/usr/bin/umount", mount_dir)
        self.assertEqual((leftover / "keep").read_bytes(), b"k")

    def test_mount_failure_fails_closed_without_local_fallback(self) -> None:
        mounts = FakeMounts(self.mount_parent)
        mounts.mount_fails = True
        self.use(mounts)
        with self.assertRaisesRegex(transport.ArchiveTransportError, "mount NFS archive failed"):
            with open_target(self.settings()):
                self.fail("body must not run when the mount fails")
        self.assertEqual(list(self.mount_parent.iterdir()), [])

    def test_mount_reporting_success_without_a_mount_is_refused(self) -> None:
        mounts = FakeMounts(self.mount_parent)
        self.use(mounts)
        mock.patch.object(transport, "_is_mounted", return_value=False).start()
        with self.assertRaisesRegex(transport.ArchiveTransportError, "refusing to write"):
            with open_target(self.settings()):
                self.fail("body must not run")
        self.assertEqual(list(self.mount_parent.iterdir()), [])

    def test_body_error_is_kept_and_unmount_still_runs(self) -> None:
        mounts = FakeMounts(self.mount_parent)
        self.use(mounts)
        with self.assertRaisesRegex(RuntimeError, "boom"):
            with open_target(self.settings()):
                raise RuntimeError("boom")
        self.assertEqual(mounts.mounted, set())
        self.assertEqual(list(self.mount_parent.iterdir()), [])

    def test_missing_mount_binaries(self) -> None:
        with mock.patch.object(transport.shutil, "which", return_value=None):
            with self.assertRaisesRegex(DependencyMissingError, "mount and umount"):
                with open_target(self.settings()):
                    pass


# --------------------------------------------------------------------------
# S3 (fake boto3)
# --------------------------------------------------------------------------


class FakeS3Client:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.objects: dict[str, dict] = {}
        self.read_sizes: list[int] = []
        self.etag_override: str | None = None
        self.corrupt_get = False
        self.get_calls = 0
        self.closed = False

    def upload_fileobj(self, fileobj, bucket, key, ExtraArgs=None, Config=None):
        chunks = []
        while True:
            chunk = fileobj.read(transport.CHUNK_SIZE)
            if not chunk:
                break
            self.read_sizes.append(len(chunk))
            chunks.append(chunk)
        data = b"".join(chunks)
        size = len(data)
        if size >= Config.multipart_threshold:
            part = Config.multipart_chunksize
            md5s = [hashlib.md5(data[i : i + part]).digest() for i in range(0, size, part)]
            etag = f"{hashlib.md5(b''.join(md5s)).hexdigest()}-{len(md5s)}"
        else:
            etag = hashlib.md5(data).hexdigest()
        self.objects[key] = {
            "data": data,
            "meta": dict((ExtraArgs or {}).get("Metadata", {})),
            "etag": etag,
        }

    def head_object(self, Bucket, Key):
        obj = self.objects[Key]
        return {
            "ContentLength": len(obj["data"]),
            "ETag": f'"{self.etag_override or obj["etag"]}"',
            "Metadata": obj["meta"],
        }

    def get_object(self, Bucket, Key):
        self.get_calls += 1
        data = self.objects[Key]["data"]
        return {"Body": io.BytesIO(data[:-1] + b"?" if self.corrupt_get else data)}

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)

    def get_paginator(self, name):
        client = self

        class _Paginator:
            def paginate(self, Bucket, Prefix):
                yield {
                    "Contents": [
                        {"Key": key, "Size": len(obj["data"]), "LastModified": datetime(2026, 9, 24, tzinfo=timezone.utc)}
                        for key, obj in sorted(client.objects.items())
                        if key.startswith(Prefix)
                    ]
                }

        return _Paginator()

    def close(self):
        self.closed = True


class _TransferConfig:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _fake_boto_modules(clients: list[FakeS3Client]) -> dict[str, types.ModuleType]:
    boto3 = types.ModuleType("boto3")

    def client(service, **kwargs):
        assert service == "s3"
        made = FakeS3Client(**kwargs)
        clients.append(made)
        return made

    boto3.client = client
    s3 = types.ModuleType("boto3.s3")
    s3_transfer = types.ModuleType("boto3.s3.transfer")
    s3_transfer.TransferConfig = _TransferConfig
    botocore = types.ModuleType("botocore")
    botocore_config = types.ModuleType("botocore.config")
    botocore_config.Config = lambda **kwargs: ("Config", kwargs)
    return {
        "boto3": boto3,
        "boto3.s3": s3,
        "boto3.s3.transfer": s3_transfer,
        "botocore": botocore,
        "botocore.config": botocore_config,
    }


class S3TargetTests(_TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.clients: list[FakeS3Client] = []
        patcher = mock.patch.dict(sys.modules, _fake_boto_modules(self.clients))
        patcher.start()
        self.addCleanup(patcher.stop)

    def settings(self, **kw) -> ArchiveTargetSettings:
        return ArchiveTargetSettings(
            target_id="s3a",
            provider="s3",
            root="jbod-ui/archive",
            bucket="bucket-one",
            region="us-east-1",
            access_key_id_file=self.secret("s3-id", "AKIDEXAMPLE"),
            secret_access_key_file=self.secret("s3-secret", "secretexample"),
            **kw,
        )

    def test_missing_dependency_has_install_hint(self) -> None:
        with mock.patch.dict(sys.modules, {"boto3": None}):
            with self.assertRaisesRegex(DependencyMissingError, "install boto3"):
                with open_target(self.settings()):
                    pass

    def test_credentials_come_from_files(self) -> None:
        with open_target(self.settings()):
            pass
        kwargs = self.clients[0].kwargs
        self.assertEqual(kwargs["aws_access_key_id"], "AKIDEXAMPLE")
        self.assertEqual(kwargs["aws_secret_access_key"], "secretexample")
        self.assertTrue(self.clients[0].closed)

    def test_small_put_verifies_by_etag_and_lists_under_prefix(self) -> None:
        data = os.urandom(transport.CHUNK_SIZE * 2 + 1)
        with open_target(self.settings()) as target:
            self.assertTrue(target.transport_encrypted)
            stored = target.put(self.source(data), "config/c1.tar")
            client = self.clients[0]
            client.objects["jbod-ui/archive-other/x.bin"] = {"data": b"x", "meta": {}, "etag": "e"}
            client.objects["jbod-ui/archive/config/c1.tar.partial"] = {"data": b"x", "meta": {}, "etag": "e"}
            listed = target.list()
            target.delete("config/c1.tar")
        self.assertEqual(stored, StoredObject("config/c1.tar", len(data), _sha(data), True))
        self.assertEqual(client.get_calls, 0)
        self.assertLessEqual(max(client.read_sizes), transport.CHUNK_SIZE)
        self.assertEqual(listed, [RemoteObject("config/c1.tar", len(data), datetime(2026, 9, 24, tzinfo=timezone.utc))])
        self.assertNotIn("jbod-ui/archive/config/c1.tar", client.objects)

    def test_small_object_with_opaque_etag_is_re_read(self) -> None:
        with open_target(self.settings()) as target:
            self.clients[0].etag_override = "kms-opaque"
            stored = target.put(self.source(b"payload"), "a.bin")
        self.assertTrue(stored.verified)
        self.assertEqual(self.clients[0].get_calls, 1)

    def test_corrupt_re_read_deletes_object_and_raises(self) -> None:
        with open_target(self.settings()) as target:
            client = self.clients[0]
            client.etag_override = "kms-opaque"
            client.corrupt_get = True
            with self.assertRaises(ArchiveVerificationError):
                target.put(self.source(b"payload"), "a.bin")
        self.assertEqual(client.objects, {})

    def test_large_multipart_etag_verifies_without_download(self) -> None:
        size = transport.S3_MULTIPART_THRESHOLD + 3
        src = self.tmp / "big.bin"
        with open(src, "wb") as handle:
            handle.truncate(size)
        with open_target(self.settings()) as target:
            stored = target.put(src, "full/big.bin")
        self.assertTrue(stored.verified)
        self.assertEqual(self.clients[0].get_calls, 0)
        self.assertTrue(self.clients[0].objects["jbod-ui/archive/full/big.bin"]["etag"].endswith("-2"))

    def test_large_opaque_etag_is_size_checked_but_unverified(self) -> None:
        with mock.patch.object(transport, "S3_REGET_LIMIT", 4), open_target(self.settings()) as target:
            self.clients[0].etag_override = "kms-opaque"
            stored = target.put(self.source(b"payload"), "a.bin")
        self.assertFalse(stored.verified)
        self.assertEqual(self.clients[0].get_calls, 0)

    def test_plain_http_endpoint_is_labelled_unencrypted(self) -> None:
        with open_target(self.settings(endpoint_url="http://minio.example.test:9000")) as target:
            self.assertFalse(target.transport_encrypted)
        self.assertEqual(self.clients[0].kwargs["endpoint_url"], "http://minio.example.test:9000")

    def test_probe(self) -> None:
        with open_target(self.settings()) as target:
            result = target.test()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.clients[0].objects, {})


if __name__ == "__main__":
    unittest.main()
