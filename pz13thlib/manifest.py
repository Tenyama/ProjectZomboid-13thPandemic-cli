"""Manifest, owner sidecar, and atomic state-file helpers."""

from __future__ import annotations

import json
import os
import secrets
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import paths
from .errors import ManifestError, UnsafePathError, ValidationError

SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def new_state_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(4)}"


def fsync_dir(path: Path) -> None:
    try:
        fd = os.open(os.fspath(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=os.fspath(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False, sort_keys=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        fsync_dir(path.parent)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def load_json(path: Path) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise ManifestError(f"state file is missing: {path}", details={"path": str(path)}) from exc
    except (OSError, ValueError) as exc:
        raise ManifestError(f"cannot read state file {path}: {exc}", details={"path": str(path)}) from exc


def ensure_state_dir(state_dir: Path) -> Path:
    state_dir = Path(state_dir).expanduser()
    try:
        st = os.lstat(state_dir)
    except FileNotFoundError:
        try:
            state_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
        except FileExistsError:
            st = os.lstat(state_dir)
        else:
            os.chmod(state_dir, 0o700)
            return state_dir
    if stat.S_ISLNK(st.st_mode):
        resolved = state_dir.resolve(strict=True)
        if not resolved.is_dir():
            raise ValidationError("state directory symlink does not point to a directory", details={"path": str(state_dir)})
        return resolved
    if not stat.S_ISDIR(st.st_mode):
        raise ValidationError("state path is not a directory", details={"path": str(state_dir)})
    return state_dir


def layouts(state_dir: Path) -> dict[str, Path]:
    state_dir = Path(state_dir)
    return {
        "state_dir": state_dir,
        "lock": state_dir / "lock",
        "manifest": state_dir / "manifest.json",
        "owner": state_dir / "manifest.owner",
        "pending": state_dir / "manifest.pending.json",
        "backups": state_dir / "backups",
    }


def manifest_sha256(manifest_path: Path) -> str:
    return paths.sha256_file(manifest_path)


def write_owner(state_dir: Path, manifest_path: Path, state_id: str, runtime: Path, jar_sha256: str) -> Path:
    layout = layouts(state_dir)
    owner = {
        "schema_version": SCHEMA_VERSION,
        "state_id": state_id,
        "manifest_sha256": manifest_sha256(manifest_path),
        "runtime_realpath": str(paths.realpath(runtime)),
        "jar_sha256": jar_sha256,
        "written_at": utc_now(),
    }
    atomic_write_json(layout["owner"], owner)
    return layout["owner"]


def load_manifest(state_dir: Path) -> dict[str, Any] | None:
    layout = layouts(state_dir)
    if not layout["manifest"].is_file():
        return None
    data = load_json(layout["manifest"])
    if not isinstance(data, dict):
        raise ManifestError("manifest is not a JSON object", details={"path": str(layout["manifest"])})
    return data


def verify_owner(state_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    layout = layouts(state_dir)
    if not layout["manifest"].is_file():
        raise ManifestError("manifest.json is missing", details={"path": str(layout["manifest"])})
    if not layout["owner"].is_file():
        raise ManifestError("manifest.owner is missing", details={"path": str(layout["owner"])})
    owner = load_json(layout["owner"])
    if not isinstance(owner, dict):
        raise ManifestError("manifest.owner is not a JSON object", details={"path": str(layout["owner"])})
    actual = manifest_sha256(layout["manifest"])
    expected = owner.get("manifest_sha256")
    if actual != expected:
        raise ManifestError(
            "manifest.owner hash does not match manifest.json; refusing to continue",
            details={"expected": expected, "actual": actual},
        )
    if owner.get("state_id") != manifest.get("state_id"):
        raise ManifestError(
            "manifest.owner state id does not match manifest.json",
            details={"owner_state_id": owner.get("state_id"), "manifest_state_id": manifest.get("state_id")},
        )
    return owner


def backup_path(state_dir: Path, state_id: str, relative_path: str) -> Path:
    parts = paths.validate_relative(relative_path)
    base = Path(state_dir) / "backups" / state_id
    target = base.joinpath(*parts)
    paths.require_within(target, base, "backup target")
    return target


def write_pending(state_dir: Path, pending: dict[str, Any]) -> Path:
    layout = layouts(state_dir)
    atomic_write_json(layout["pending"], pending)
    return layout["pending"]


def load_pending(state_dir: Path) -> dict[str, Any] | None:
    layout = layouts(state_dir)
    if not layout["pending"].is_file():
        return None
    data = load_json(layout["pending"])
    if not isinstance(data, dict):
        raise ManifestError("pending manifest is not a JSON object", details={"path": str(layout["pending"])})
    return data


def remove_pending(state_dir: Path) -> None:
    layout = layouts(state_dir)
    try:
        layout["pending"].unlink()
    except FileNotFoundError:
        pass
    fsync_dir(layout["state_dir"])


def write_final_manifest(state_dir: Path, manifest: dict[str, Any]) -> Path:
    layout = layouts(state_dir)
    atomic_write_json(layout["manifest"], manifest)
    return layout["manifest"]


def validate_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ManifestError(
            "unsupported manifest schema version",
            details={"expected": SCHEMA_VERSION, "found": manifest.get("schema_version")},
        )
    if not isinstance(manifest.get("entries"), list):
        raise ManifestError("manifest entries are missing")
    if not isinstance(manifest.get("game"), dict):
        raise ManifestError("manifest game section is missing")
    if not isinstance(manifest.get("payload"), dict):
        raise ManifestError("manifest payload section is missing")


def remove_backup_set(state_dir: Path, state_id: str) -> None:
    base = Path(state_dir) / "backups" / state_id
    if not base.exists():
        return
    for dirpath, dirnames, filenames in os.walk(base, topdown=False, followlinks=False):
        for filename in filenames:
            try:
                (Path(dirpath) / filename).unlink()
            except OSError:
                pass
        for dirname in dirnames:
            try:
                (Path(dirpath) / dirname).rmdir()
            except OSError:
                pass
    try:
        base.rmdir()
    except OSError:
        pass


def relative_backup_path(state_id: str, relative_path: str) -> str:
    paths.validate_relative(relative_path)
    return f"backups/{state_id}/{relative_path}"
