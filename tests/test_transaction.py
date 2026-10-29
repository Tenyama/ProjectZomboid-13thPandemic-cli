"""Transactional apply/restore tests using synthetic directories only."""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pz13thlib import manifest, payload, transaction
from pz13thlib.errors import ConflictError, LockError, UnsafePathError
from tests.support import make_install


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fixture = make_install(self.root)
        self.inst = self.fixture["inst"]
        self.inv = payload.inventory(self.inst.payload)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_dry_run_creates_nothing(self) -> None:
        result = transaction.apply(self.inst, self.inv, dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertFalse(self.fixture["state"].exists())
        self.assertFalse((self.inst.runtime / "zombie").exists())
        self.assertFalse((self.inst.runtime / "pzopt").exists())

    def test_apply_and_restore(self) -> None:
        config_before = (self.inst.runtime / "ProjectZomboid64.json").read_text(encoding="utf-8")
        result = transaction.apply(self.inst, self.inv)
        self.assertTrue(result["ok"])
        self.assertTrue((self.fixture["state"] / "manifest.json").is_file())
        self.assertTrue((self.inst.runtime / "zombie" / "Foo.class").is_file())
        self.assertEqual(
            (self.inst.runtime / "media" / "videos" / "title_screen_background.bik").read_bytes(),
            b"payload-title",
        )
        # Linux config is never touched.
        self.assertEqual(
            (self.inst.runtime / "ProjectZomboid64.json").read_text(encoding="utf-8"),
            config_before,
        )
        state_id = result["state_id"]
        backup = self.fixture["state"] / "backups" / state_id / "media" / "videos" / "title_screen_background.bik"
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.read_bytes(), b"stock-title")

        restored = transaction.restore(self.inst)
        self.assertTrue(restored["ok"])
        self.assertFalse((self.inst.runtime / "zombie" / "Foo.class").exists())
        self.assertEqual(
            (self.inst.runtime / "media" / "videos" / "title_screen_background.bik").read_bytes(),
            b"stock-title",
        )
        self.assertFalse((self.fixture["state"] / "manifest.json").exists())
        self.assertFalse((self.fixture["state"] / "manifest.owner").exists())

    def test_apply_is_idempotent(self) -> None:
        transaction.apply(self.inst, self.inv)
        second = transaction.apply(self.inst, self.inv)
        self.assertTrue(second.get("already_applied"))

    def test_restore_refuses_modified_target(self) -> None:
        transaction.apply(self.inst, self.inv)
        target = self.inst.runtime / "zombie" / "Foo.class"
        target.write_bytes(b"user-changed")
        with self.assertRaises(ConflictError):
            transaction.restore(self.inst)
        self.assertEqual(target.read_bytes(), b"user-changed")
        self.assertTrue((self.fixture["state"] / "manifest.json").exists())

    def test_interrupted_apply_rolls_back(self) -> None:
        real_copy = transaction._copy_file
        calls = {"count": 0}

        def flaky(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 2:
                raise RuntimeError("simulated interruption")
            return real_copy(*args, **kwargs)

        with mock.patch.object(transaction, "_copy_file", side_effect=flaky):
            with self.assertRaises(RuntimeError):
                transaction.apply(self.inst, self.inv)

        self.assertFalse((self.fixture["state"] / "manifest.pending.json").exists())
        self.assertFalse((self.fixture["state"] / "manifest.json").exists())
        self.assertFalse((self.inst.runtime / "media" / "lua" / "client" / "pzopt" / "a.lua").exists())
        self.assertEqual(
            (self.inst.runtime / "media" / "videos" / "title_screen_background.bik").read_bytes(),
            b"stock-title",
        )

    def test_lock_refuses_concurrent_operation(self) -> None:
        with mock.patch("pz13thlib.transaction.fcntl.flock", side_effect=BlockingIOError):
            with self.assertRaises(LockError):
                with transaction.FileLock(self.fixture["state"]):
                    pass

    def test_unsafe_payload_source_refuses_apply(self) -> None:
        link = self.inst.payload / "media" / "linked.bik"
        os.symlink(self.inst.runtime / "media" / "videos" / "title_screen_background.bik", link)
        inv = payload.inventory(self.inst.payload)
        with self.assertRaises(UnsafePathError):
            transaction.apply(self.inst, inv, dry_run=True)

    def test_manifest_owner_is_verified(self) -> None:
        result = transaction.apply(self.inst, self.inv)
        current = manifest.load_manifest(self.fixture["state"])
        self.assertIsNotNone(current)
        owner = manifest.verify_owner(self.fixture["state"], current)
        self.assertEqual(owner["state_id"], result["state_id"])
        self.assertEqual(owner["jar_sha256"], self.fixture["jar_sha"])


if __name__ == "__main__":
    unittest.main()
