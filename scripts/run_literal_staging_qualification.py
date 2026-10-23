#!/usr/bin/env python3
"""Disposable #736 staging only. No rotation, migration, restore, or appliance I/O.

The unprivileged coordinator admits a fresh GitHub-hosted runner and allocates
one private fixture root. Its sudo worker changes only newly created fixtures.
The documented Python heredoc executes unchanged with real os/stat/fchown.
Container probes import the accepted image's real admission helpers. Synthetic
archive-shaped bytes do NOT qualify encryption or a FULL restore round trip.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import uuid

SOURCE_SHA = "e85b8d5b56962bf6bb55d2b1fc1e425c5407dd3b"
BRANCH = "test/jbod-staging-qualification-20261023"
ACK = "I_APPROVE_DISPOSABLE_STAGING_QA"
LABEL = "org.truenas-jbod.staging-qualification"
ARCHIVE = "jbod-scheduled-backup-20260101T000000Z-00000000.tar.zst.enc"
PAYLOAD = b"synthetic archive-shaped bytes; staging does not decrypt\n"
STAGE = "history/.segment-rotation-backup"
STATUS = "backup-status/scheduled-backup.json"
FAULTS = ("fresh", "stale", "history_owner", "backup_owner", "status_owner", "archive_owner",
          "backup_mode", "status_mode", "archive_mode", "history_symlink", "backup_symlink",
          "status_symlink", "archive_symlink", "digest")
BASE_PATHS = {"history", "history/history.db", "backups", "backups/scheduled",
              "backups/scheduled/" + ARCHIVE, "backup-status", STATUS}
ALLOWED_PATHS = BASE_PATHS | {STAGE, STAGE + "/" + ARCHIVE} | {
    value + ".real" for value in ("history", "backups/scheduled", STATUS,
                                  "backups/scheduled/" + ARCHIVE)
} | {"history.real/history.db", "backups/scheduled.real/" + ARCHIVE}

CONTAINER_PROBE = r'''
import hashlib, json, os, sys
from pathlib import Path
from history_service.segment_sealer import _require_source_owner
from history_service.segment_rotation import _validate_backup_evidence
scenario, expected_uid, expected_gid, archive, digest = sys.argv[1:]
assert os.geteuid() == int(expected_uid) and os.getegid() == int(expected_gid)
assert 10001 in {os.getegid(), *os.getgroups()}
hot = Path('/app/history/history.db')
if scenario == 'wrong-owner':
    assert os.geteuid() != hot.stat().st_uid
    try:
        _require_source_owner(hot.stat())
    except ValueError as error:
        assert 'must own' in str(error)
    else:
        raise AssertionError('wrong effective UID admitted')
else:
    _require_source_owner(hot.stat())
    stage = Path('/app/history/.segment-rotation-backup')
    staged = stage / archive
    assert staged.stat().st_uid == os.geteuid()
    assert staged.stat().st_gid == os.getegid()
    assert staged.stat().st_mode & 0o7777 == 0o600
    assert stage.stat().st_mode & 0o7777 == 0o700
    assert hashlib.sha256(staged.read_bytes()).hexdigest() == digest
    if os.geteuid() == 10001:
        try:
            Path('/app/backups/scheduled', archive).read_bytes()
        except PermissionError:
            pass
        else:
            raise AssertionError('history UID read private backup directly')
    try:
        record = _validate_backup_evidence(stage, Path('/app/backup-status/scheduled-backup.json'))
    except ValueError as error:
        assert scenario == 'stale' and 'stale' in str(error)
    else:
        assert scenario == 'fresh' and record['sha256'] == digest
    assert not Path('/app/history/segments').exists()
print(json.dumps({'probe': scenario, 'uid': os.geteuid(), 'gid': os.getegid(), 'pass': True}))
'''


def run(argv: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None,
        check: bool = True, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=cwd, env=env, check=check, timeout=timeout,
                          text=True, capture_output=True, stdin=subprocess.DEVNULL)


def clean_env(scratch: Path) -> dict[str, str]:
    return {"PATH": "/usr/bin:/bin", "HOME": str(scratch), "TMPDIR": str(scratch),
            "PYTHONDONTWRITEBYTECODE": "1", "DOCKER_CONFIG": str(scratch / "docker-config")}


def docker(scratch: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(["/usr/bin/docker", "--host", "unix:///var/run/docker.sock", *args],
               env=clean_env(scratch), check=check)


def require_hosted_runner() -> None:
    expected = {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted",
                "RUNNER_OS": "Linux", "GITHUB_REPOSITORY": "gcs8/truenas-jbod-ui",
                "GITHUB_ACTOR": "gcs8", "GITHUB_TRIGGERING_ACTOR": "gcs8",
                "GITHUB_REF": "refs/heads/" + BRANCH, "GITHUB_EVENT_NAME": "push"}
    if any(os.environ.get(key) != value for key, value in expected.items()):
        raise ValueError("requires the admitted GitHub-hosted branch-only Linux runner")
    if os.geteuid() == 0:
        raise ValueError("coordinator must be the existing unprivileged runner user")


def require_source(source: Path, revision: str) -> None:
    if revision != SOURCE_SHA or source.is_symlink() or source.resolve() != source:
        raise ValueError("source must be the exact accepted checkout")
    git_env = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_CONFIG_GLOBAL": os.devnull, "GIT_OPTIONAL_LOCKS": "0"}
    if run(["git", "-c", f"safe.directory={source}", "rev-parse", "HEAD"], cwd=source, env=git_env).stdout.strip() != revision:
        raise ValueError("source revision mismatch")
    if run(["git", "-c", f"safe.directory={source}", "status", "--porcelain", "--untracked-files=all"],
           cwd=source, env=git_env).stdout.strip():
        raise ValueError("source checkout is not read-only/clean")


def staging_code(source: Path) -> str:
    guide = (source / "docs/SEGMENTED_HISTORY_V2.md").read_text(encoding="utf-8")
    blocks = [block for block in re.findall(r"```bash\n(.*?)\n```", guide, re.DOTALL)
              if "STAGE_NAME =" in block]
    if len(blocks) != 1 or blocks[0].count("<<'PY'\n") != 1:
        raise ValueError("expected exactly one literal documented staging heredoc")
    return blocks[0].split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]


def require_runtime_path(root: Path, scratch: Path, uid: int, *, existing: bool) -> None:
    if scratch.is_symlink() or scratch.resolve() != scratch:
        raise ValueError("scratch parent must not be a symlink")
    metadata = scratch.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != uid or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise ValueError("scratch parent must be private and owned by the runner UID")
    if root.parent != scratch or root.name != "runtime" or root.is_symlink():
        raise ValueError("runtime must be the exact non-symlink direct child")
    if existing:
        metadata = root.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != uid or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise ValueError("runtime root ownership or mode changed")
    elif os.path.lexists(root):
        raise ValueError("runtime root must be fresh and absent")


def snapshot(root: Path) -> dict[str, tuple]:
    """No symlink traversal; ignore directory mtime/nlink changed by staging."""
    result = {}
    def walk(parent: Path) -> None:
        for path in sorted(parent.iterdir()):
            metadata = path.lstat()
            content = path.read_bytes() if stat.S_ISREG(metadata.st_mode) else None
            target = os.readlink(path) if stat.S_ISLNK(metadata.st_mode) else None
            links = metadata.st_nlink if stat.S_ISREG(metadata.st_mode) else None
            result[path.relative_to(root).as_posix()] = (
                metadata.st_dev, metadata.st_ino, metadata.st_uid, metadata.st_gid,
                metadata.st_mode, links, hashlib.sha256(content).hexdigest() if content is not None else None,
                content, target)
            if stat.S_ISDIR(metadata.st_mode):
                walk(path)
    walk(root)
    return result


def cleanup_case(root: Path) -> None:
    entries = snapshot(root)
    if set(entries) - ALLOWED_PATHS:
        raise ValueError("unexpected fixture entries; retain all evidence")
    # Only enumerated task-owned paths; no recursive chown, glob removal, or prune.
    for name in sorted(entries, key=lambda value: (value.count("/"), value), reverse=True):
        path = root / name
        if stat.S_ISDIR(path.lstat().st_mode):
            path.rmdir()
        else:
            path.unlink()
    root.rmdir()
    if os.path.lexists(root):
        raise ValueError("fixture removal not verified")


def make_case(root: Path, history: tuple[int, int], backup: tuple[int, int], fault: str) -> None:
    root.mkdir(mode=0o700)
    for name in ("history", "backups", "backups/scheduled", "backup-status"):
        (root / name).mkdir(mode=0o700)
    hot = root / "history/history.db"
    hot.write_bytes(b"synthetic owner-admission marker, not SQLite\n")
    archive = root / "backups/scheduled" / ARCHIVE
    archive.write_bytes(PAYLOAD)
    success_at = datetime.now(timezone.utc) - timedelta(days=3 if fault == "stale" else 0)
    status_path = root / STATUS
    status_path.write_text(json.dumps({
        "schema_version": 1, "enabled": True, "success_count": 1, "failure_count": 0,
        "last_retention_removed": 0, "last_attempt_at": None, "last_failure_at": None,
        "last_success_at": success_at.isoformat(), "last_error_code": None,
        "included_groups": ["history_db"], "last_absent_groups": [],
        "last_artifact_name": ARCHIVE, "last_size_bytes": len(PAYLOAD),
        "last_sha256": "0" * 64 if fault == "digest" else hashlib.sha256(PAYLOAD).hexdigest(),
    }), encoding="utf-8")
    paths = {"history": (root / "history", history, 0o750),
             "hot": (hot, history, 0o640),
             "backup": (root / "backups/scheduled", backup, 0o700),
             "status_directory": (root / "backup-status", (backup[0], 10001), 0o750),
             "status": (status_path, (backup[0], 10001), 0o640),
             "archive": (archive, backup, 0o600)}
    for name, (path, ids, mode) in paths.items():
        os.chown(path, 99999 if fault == name + "_owner" else ids[0], ids[1])
        path.chmod(0o777 if fault == name + "_mode" else mode)
    for name in ("history", "backup", "status", "archive"):
        if fault == name + "_symlink":
            path = paths[name][0]
            moved = path.with_name(path.name + ".real")
            path.rename(moved)
            path.symlink_to(moved.name, target_is_directory=moved.is_dir())


def probe_arguments(root: Path, image: str, name: str, token: str, ids: tuple[int, int],
                    scenario: str, *, wrong_owner: bool) -> list[str]:
    argv = ["create", "--name", name, "--label", f"{LABEL}={token}", "--network", "none",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--read-only",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=16m", "--pids-limit", "64",
            "--memory", "256m", "--cpus", "1", "--user", f"{ids[0]}:{ids[1]}",
            "--group-add", "10001", "--no-healthcheck", "--entrypoint", "python"]
    if wrong_owner:
        mounts = [(root / "history/history.db", "/app/history/history.db")]
    else:
        mounts = [(root / "history", "/app/history"),
                  (root / "backup-status", "/app/backup-status"),
                  (root / "backups/scheduled", "/app/backups/scheduled")]
    for source, target in mounts:
        argv += ["--mount", f"type=bind,src={source},dst={target},readonly"]
    return argv + [image, "-c", CONTAINER_PROBE, scenario, str(ids[0]), str(ids[1]), ARCHIVE,
                   hashlib.sha256(PAYLOAD).hexdigest()]


def inspect_owned(scratch: Path, name: str, token: str, image: str) -> dict:
    # A successful list distinguishes absence from daemon/query failure.
    ids = docker(scratch, "ps", "--all", "--quiet", "--filter", "name=^/" + name + "$").stdout.split()
    if len(ids) != 1:
        raise ValueError("probe container ownership is absent or ambiguous")
    record, = json.loads(docker(scratch, "inspect", ids[0]).stdout)
    if (record["Name"] != "/" + name or record["Image"] != image
            or record["Config"]["Labels"].get(LABEL) != token):
        raise ValueError("probe container ownership mismatch; no deletion authorized")
    return record


def container_probe(scratch: Path, root: Path, image: str, token: str, ids: tuple[int, int],
                    scenario: str) -> None:
    name = "tjui-staging-" + token + "-" + uuid.uuid4().hex[:8]
    if docker(scratch, "ps", "--all", "--quiet", "--filter", "name=^/" + name + "$").stdout.strip():
        raise ValueError("probe name collision")
    try:
        created = docker(scratch, *probe_arguments(root, image, name, token, ids, scenario,
                                                  wrong_owner=scenario == "wrong-owner"))
        record = inspect_owned(scratch, name, token, image)
        if record["Id"] != created.stdout.strip():
            raise ValueError("created container ID mismatch")
        host = record["HostConfig"]
        if (not host["ReadonlyRootfs"] or host["NetworkMode"] != "none"
                or host["CapDrop"] != ["ALL"] or host["Privileged"]
                or "no-new-privileges:true" not in host["SecurityOpt"]
                or any(mount["RW"] for mount in record["Mounts"] if mount["Type"] == "bind")):
            raise ValueError("effective container restrictions mismatch")
        result = docker(scratch, "start", "--attach", record["Id"])
        after = inspect_owned(scratch, name, token, image)
        if after["State"]["Running"] or after["State"]["ExitCode"] != 0:
            raise ValueError("container probe failed: " + result.stdout + result.stderr)
        receipt = json.loads(result.stdout)
        if receipt != {"probe": scenario, "uid": ids[0], "gid": ids[1], "pass": True}:
            raise ValueError("container receipt mismatch")
        print(json.dumps(receipt), flush=True)
    finally:
        # Also resolves ambiguous create timeouts by exact name + image + token.
        listed = docker(scratch, "ps", "--all", "--quiet", "--filter", "name=^/" + name + "$").stdout.strip()
        if listed:
            owned = inspect_owned(scratch, name, token, image)
            docker(scratch, "rm", "--force", owned["Id"])
        if docker(scratch, "ps", "--all", "--quiet", "--filter", "name=^/" + name + "$").stdout.strip():
            raise ValueError("probe container cleanup unverified")


def require_image(scratch: Path, image: str) -> None:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise ValueError("exact local image ID required")
    record, = json.loads(docker(scratch, "image", "inspect", image).stdout)
    if record["Id"] != image or record["Config"]["Labels"].get("org.opencontainers.image.revision") != SOURCE_SHA:
        raise ValueError("local image source identity mismatch")


def census(scratch: Path) -> None:
    if docker(scratch, "info", "--format", "{{.Swarm.LocalNodeState}}").stdout.strip() != "inactive":
        raise ValueError("exclusive non-Swarm daemon required")
    if docker(scratch, "ps", "--all", "--quiet").stdout.strip():
        raise ValueError("fresh exclusive daemon has foreign or remaining containers")
    if docker(scratch, "volume", "ls", "--quiet").stdout.strip():
        raise ValueError("fresh exclusive daemon has volumes")
    networks = set(docker(scratch, "network", "ls", "--format", "{{.Name}}").stdout.split())
    if networks != {"bridge", "host", "none"}:
        raise ValueError("fresh exclusive daemon has non-default networks")


def worker(args: argparse.Namespace) -> None:
    uid = int(os.environ["SUDO_UID"])
    if os.geteuid() != 0 or uid == 0:
        raise ValueError("worker requires existing sudo runner rights only")
    require_source(args.source, args.source_commit)
    require_runtime_path(args.runtime_root, args.scratch_root, uid, existing=True)
    if (args.runtime_root / "owner-token").read_text() != args.token:
        raise ValueError("runtime ownership token mismatch")
    if set(path.name for path in args.runtime_root.iterdir()) != {"owner-token"}:
        raise ValueError("worker fixture root was not fresh")
    require_image(args.scratch_root, args.image)
    census(args.scratch_root)
    code = staging_code(args.source)
    count = 0
    for label, history, backup in (("root", (0, 0), (0, 0)),
                                   ("nonroot", (10001, 10001), (1000, 1000))):
        for fault in FAULTS:
            root = args.runtime_root / f"{label}-{fault}"
            make_case(root, history, backup, fault)
            originals = snapshot(root)
            completed = False
            try:
                env = clean_env(args.scratch_root) | dict(zip(
                    ("HISTORY_UID", "HISTORY_GID", "BACKUP_UID", "BACKUP_GID", "APP_GID"),
                    map(str, (*history, *backup, 10001))))
                result = run([sys.executable, "-I", "-B", "-c", code], cwd=root, env=env, check=False)
                current = snapshot(root)
                if any(current.get(name) != before for name, before in originals.items()):
                    raise ValueError("literal recipe changed original ownership/mode/inode/hash/contents")
                success = fault in {"fresh", "stale"}
                if success:
                    if result.returncode or result.stdout != "staged_backup=ok\n":
                        raise ValueError("literal staging failed: " + result.stderr)
                    if set(current) != set(originals) | {STAGE, STAGE + "/" + ARCHIVE}:
                        raise ValueError("staging created unexpected publication")
                    for name, mode in ((STAGE, 0o700), (STAGE + "/" + ARCHIVE, 0o600)):
                        metadata = (root / name).stat()
                        if (metadata.st_uid, metadata.st_gid, stat.S_IMODE(metadata.st_mode)) != (*history, mode):
                            raise ValueError("staging actual UID/GID/mode mismatch")
                    if (root / STAGE / ARCHIVE).read_bytes() != PAYLOAD:
                        raise ValueError("staging content mismatch")
                    container_probe(args.scratch_root, root, args.image, args.token, history, fault)
                    wrong = (10001, 10001) if history[0] == 0 else (0, 0)
                    container_probe(args.scratch_root, root, args.image, args.token, wrong, "wrong-owner")
                else:
                    expected = ("ownership" if fault.endswith("_owner") else "mode" if fault.endswith("_mode")
                                else "integrity verification" if fault == "digest" else "")
                    if (result.returncode == 0 or "staged_backup=ok" in result.stdout
                            or expected not in result.stderr or current != originals):
                        raise ValueError("literal refusal/publication contract failed for " + fault)
                if snapshot(root) != current:
                    raise ValueError("container probes mutated the read-only fixture")
                print(f"staging_case={label}:{fault} pass=true originals_unchanged=true", flush=True)
                count += 1
                completed = True
            finally:
                # Preserve failed/changed evidence and uncertain active bind sources.
                if completed:
                    cleanup_case(root)
    if count != 2 * len(FAULTS):
        raise ValueError("staging case count incomplete")
    census(args.scratch_root)
    print(f"staging_cases={count} container_probes=8 publication=none restore_roundtrip=not-tested", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--scratch-root", type=Path, required=True)
    parser.add_argument("--ack", choices=[ACK], required=True)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--token", default="", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        if not re.fullmatch(r"[0-9a-f]{32}", args.token):
            raise ValueError("invalid task token")
        worker(args)
        return
    require_hosted_runner()  # Refuse local execution before any subprocess or fixture write.
    require_source(args.source, args.source_commit)
    runner_temp = Path(os.environ["RUNNER_TEMP"])
    if (runner_temp.resolve() != runner_temp or args.scratch_root.parent != runner_temp
            or not args.scratch_root.name.startswith("tjui-runtime-")):
        raise ValueError("fresh private scratch must be directly below RUNNER_TEMP")
    require_runtime_path(args.runtime_root, args.scratch_root, os.getuid(), existing=False)
    require_image(args.scratch_root, args.image)
    census(args.scratch_root)
    args.runtime_root.mkdir(mode=0o700)
    token = uuid.uuid4().hex
    marker = args.runtime_root / "owner-token"
    marker.write_text(token)
    marker.chmod(0o600)
    child = ["/usr/bin/sudo", "-n", "--", "/usr/bin/python3", "-I", "-B", str(Path(__file__).resolve()),
             *sys.argv[1:], "--worker", "--token", token]
    # No new account, sudoers, group membership, grants, package install, or daemon.
    result = run(child, env=clean_env(args.scratch_root), check=False, timeout=1200)
    print(result.stdout, end="", flush=True)
    print(result.stderr, end="", file=sys.stderr, flush=True)
    census(args.scratch_root)
    require_source(args.source, args.source_commit)
    require_runtime_path(args.runtime_root, args.scratch_root, os.getuid(), existing=True)
    if set(path.name for path in args.runtime_root.iterdir()) != {"owner-token"} or marker.read_text() != token:
        raise ValueError("fixture cleanup unverified; retain runtime evidence")
    marker.unlink()
    args.runtime_root.rmdir()
    if os.path.lexists(args.runtime_root):
        raise ValueError("runtime absence unverified")
    if result.returncode:
        raise ValueError("staging worker failed; see bounded synthetic diagnostic above")
    print("staging_cleanup=pass task_containers=0 task_networks=0 task_volumes=0 runtime_root=absent")


if __name__ == "__main__":
    main()
