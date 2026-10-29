"""Output helpers for human-readable and JSON modes."""

from __future__ import annotations

import json
import sys
from typing import Any, Iterable


def dumps(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False)


def emit_json(data: Any) -> None:
    sys.stdout.write(dumps(data))
    sys.stdout.write("\n")


def emit_error(err: Any, as_json: bool) -> None:
    if as_json:
        emit_json({"ok": False, "error": err.as_dict()})
    else:
        sys.stderr.write(f"error: {err.message}\n")
        details = getattr(err, "details", None) or {}
        if details:
            for line in _detail_lines(details):
                sys.stderr.write(f"  {line}\n")


def _detail_lines(value: Any, prefix: str = "") -> Iterable[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (dict, list, tuple)):
                yield f"{prefix}{key}:"
                yield from _detail_lines(item, prefix + "  ")
            else:
                yield f"{prefix}{key}: {item}"
    elif isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, (dict, list, tuple)):
                yield from _detail_lines(item, prefix + "- ")
            else:
                yield f"{prefix}- {item}"
    else:
        yield f"{prefix}{value}"


def print_kv(lines: Iterable[tuple[str, Any]]) -> None:
    for key, value in lines:
        sys.stdout.write(f"{key:<24} {value}\n")


def print_section(title: str) -> None:
    sys.stdout.write(f"\n{title}\n")
    sys.stdout.write("-" * len(title) + "\n")
