"""Typed errors and process exit codes."""

from __future__ import annotations

from typing import Any


class PzError(Exception):
    """Base class for expected, user-facing failures."""

    code = "error"
    exit_code = 3

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


class UsageError(PzError):
    code = "usage"
    exit_code = 2


class DetectionError(PzError):
    code = "detection"
    exit_code = 3


class ValidationError(PzError):
    code = "validation"
    exit_code = 3


class UnsafePathError(PzError):
    code = "unsafe_path"
    exit_code = 4


class ConflictError(PzError):
    code = "conflict"
    exit_code = 4


class LockError(PzError):
    code = "lock_held"
    exit_code = 5


class LaunchError(PzError):
    code = "launch"
    exit_code = 6


class TransactionError(PzError):
    code = "transaction"
    exit_code = 7


class ManifestError(PzError):
    code = "manifest"
    exit_code = 7
