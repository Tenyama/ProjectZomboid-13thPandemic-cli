"""Path validation, hashing, and process checks.

All mutating operations must resolve their targets through these helpers.
They deliberately reject symlinks in every component below the trusted root.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path, PurePosixPath
from typing import Iterable

from . import platforms
from .errors import UnsafePathError, ValidationError


def expand(value: str | Path) -> Path:
    return Path(value).expanduser()


def resolve_dir(value: str | Path, label: str) -> Path:
    path = expand(value)
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValidationError(f"{label} does not exist: {path}", details={"path": str(path), "error": str(exc)})
    if not resolved.is_dir():
        raise ValidationError(f"{label} is not a directory: {resolved}", details={"path": str(resolved)})
    return resolved


def resolve_file(value: str | Path, label: str) -> Path:
    path = expand(value)
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValidationError(f"{label} does not exist: {path}", details={"path": str(path), "error": str(exc)})
    if not resolved.is_file():
        raise ValidationError(f"{label} is not a regular file: {resolved}", details={"path": str(resolved)})
    return resolved


def realpath(value: str | Path) -> Path:
    return Path(os.path.realpath(os.fspath(expand(value))))


def is_within(path: Path, root: Path) -> bool:
    try:
        Path(os.path.realpath(os.fspath(path))).relative_to(Path(os.path.realpath(os.fspath(root))))
    except ValueError:
        return False
    return True


def require_within(path: Path, root: Path, label: str = "path") -> None:
    if not is_within(path, root):
        raise UnsafePathError(
            f"{label} escapes the allowed root: {path}",
            details={"path": str(path), "root": str(root)},
        )


def validate_relative(rel: str) -> tuple[str, ...]:
    if not isinstance(rel, str) or not rel:
        raise UnsafePathError("empty relative path")
    if "\x00" in rel:
        raise UnsafePathError("NUL byte in relative path", details={"path": rel})
    if "\\" in rel:
        raise UnsafePathError("backslash is not allowed in payload paths", details={"path": rel})
    pure = PurePosixPath(rel)
    if pure.is_absolute():
        raise UnsafePathError("absolute payload path is not allowed", details={"path": rel})
    parts = pure.parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise UnsafePathError("payload path is not normalized", details={"path": rel})
    return parts


def relative_to_posix(path: Path, root: Path) -> str:
    try:
        rel = path.relative_to(root)
    except ValueError as exc:
        raise UnsafePathError(
            f"path is outside the expected root: {path}",
            details={"path": str(path), "root": str(root)},
        ) from exc
    return rel.as_posix()


def lstat_kind(path: Path) -> str:
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return "missing"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "file"
    return "other"


def safe_target(root: Path, rel: str) -> Path:
    """Return the target path, rejecting symlinks and non-regular leaves.

    Missing leaf and missing intermediate directories are allowed; they are
    created later by ensure_parent_chain().  Any existing component below the
    root must be a real directory (or, for the leaf, a regular file).
    """

    parts = validate_relative(rel)
    current = root
    for index, part in enumerate(parts):
        candidate = current / part
        try:
            st = os.lstat(candidate)
        except FileNotFoundError:
            current = candidate
            continue
        if stat.S_ISLNK(st.st_mode):
            raise UnsafePathError(
                "refusing to follow a symlink in the target path",
                details={"path": str(candidate), "relative_path": rel},
            )
        if index < len(parts) - 1:
            if not stat.S_ISDIR(st.st_mode):
                raise UnsafePathError(
                    "a parent component is not a directory",
                    details={"path": str(candidate), "relative_path": rel},
                )
        elif not stat.S_ISREG(st.st_mode):
            raise UnsafePathError(
                "target is not a regular file",
                details={"path": str(candidate), "relative_path": rel},
            )
        current = candidate
    require_within(current, root, "target")
    return current


def ensure_parent_chain(root: Path, parent: Path) -> list[Path]:
    """Create missing parent directories below root, refusing symlinks.

    Returns the directories that were created, in creation order.
    """

    parent = Path(parent)
    require_within(parent, root, "parent directory")
    try:
        rel = parent.relative_to(root)
    except ValueError as exc:
        raise UnsafePathError("parent directory escapes the runtime root", details={"path": str(parent)}) from exc

    current = root
    created: list[Path] = []
    for part in rel.parts:
        candidate = current / part
        try:
            st = os.lstat(candidate)
        except FileNotFoundError:
            try:
                os.mkdir(candidate, 0o755)
            except FileExistsError:
                pass
            st = os.lstat(candidate)
            created.append(candidate)
        if stat.S_ISLNK(st.st_mode):
            raise UnsafePathError(
                "refusing to create or use a symlinked directory",
                details={"path": str(candidate)},
            )
        if not stat.S_ISDIR(st.st_mode):
            raise UnsafePathError(
                "parent component is not a directory",
                details={"path": str(candidate)},
            )
        current = candidate
    return created


def remove_empty_dirs(root: Path, directories: Iterable[Path | str]) -> list[Path]:
    """Remove only empty directories below root.  Never removes root itself."""

    root_real = realpath(root)
    removed: list[Path] = []
    paths = [Path(item) for item in directories]
    paths.sort(key=lambda p: len(p.parts), reverse=True)
    for path in paths:
        candidate = Path(os.path.abspath(path))
        if not is_within(candidate, root_real):
            continue
        if realpath(candidate) == root_real:
            continue
        try:
            os.rmdir(candidate)
        except FileNotFoundError:
            continue
        except OSError:
            # Not empty or otherwise not removable; leave it alone.
            continue
        removed.append(candidate)
    return removed


def sha256_file(path: str | Path, *, nofollow: bool = True) -> str:
    flags = os.O_RDONLY
    if nofollow and hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(os.fspath(path), flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValidationError(f"not a regular file: {path}", details={"path": str(path)})
        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(fd)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def game_running(runtime: Path) -> bool:
    """Return True if a Project Zomboid process is running from runtime."""

    target = os.path.realpath(os.fspath(runtime))
    for pid, command in platforms.processes():
        if "ProjectZomboid64" not in command and "zombie.gameStates.MainScreenState" not in command and "projectzomboid.jar" not in command:
            continue
        cwd = platforms.process_cwd(pid)
        if cwd is not None and os.path.realpath(cwd) == target:
            return True
    return False
