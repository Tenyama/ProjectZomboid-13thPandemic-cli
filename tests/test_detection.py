"""Detection and payload classification tests."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pz13thlib import detection, payload
from pz13thlib.errors import UnsafePathError
from tests.support import make_install, write


class DetectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fixture = make_install(self.root)
        self.inst = self.fixture["inst"]
        self.roots = mock.patch("pz13thlib.steam.default_steam_roots", return_value=[])
        self.roots.start()

    def tearDown(self) -> None:
        self.roots.stop()
        self.tmp.cleanup()

    def test_inventory_and_exclusions(self) -> None:
        inv = payload.inventory(self.inst.payload)
        self.assertEqual(inv["total_count"], 11)
        self.assertEqual(inv["included_count"], 6)
        self.assertEqual(inv["excluded_count"], 5)
        reasons = {item["relative_path"]: item["exclusion_reason"] for item in inv["excluded"]}
        self.assertEqual(reasons["jre64.xml"], "windows_jre")
        self.assertEqual(reasons["ProjectZomboid64.json"], "windows_jvm_config")
        self.assertEqual(reasons["pzopt-files.txt"], "payload_metadata")
        self.assertEqual(reasons["pzopt/uninstall/install.bash"], "installer_script")
        self.assertEqual(reasons["pzopt/uninstall/install.ps1"], "installer_script")
        self.assertEqual(inv["custom_video_count"], 2)

    def test_classification_and_conflict(self) -> None:
        inv = payload.inventory(self.inst.payload)
        classified = payload.classify_targets(inv, self.inst.runtime)
        self.assertEqual(classified["counts"]["new"], 5)
        self.assertEqual(classified["counts"]["replaced"], 1)
        self.assertEqual(classified["conflicts"][0]["relative_path"], "media/videos/title_screen_background.bik")

    def test_detection_jar_and_layout(self) -> None:
        inv = payload.inventory(self.inst.payload)
        report = detection.build_detection(self.inst, self.fixture["opts"], inv)
        self.assertEqual(report["layout"], "native-subdir")
        self.assertTrue(report["jar"]["match"])
        self.assertEqual(report["targets"]["counts"]["replaced"], 1)
        self.assertEqual(report["paths"]["runtime"], str(self.inst.runtime.resolve()))

    def test_status_does_not_create_state(self) -> None:
        inv = payload.inventory(self.inst.payload)
        applied = detection.applied_status(self.inst, inv)
        self.assertEqual(applied["state"], "not_applied")
        self.assertFalse(self.fixture["state"].exists())

    def test_symlink_target_is_rejected(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        zombie = self.inst.runtime / "zombie"
        os.symlink(outside, zombie)
        inv = payload.inventory(self.inst.payload)
        classified = payload.classify_targets(inv, self.inst.runtime)
        self.assertTrue(classified["unsafe"])

    def test_unsafe_payload_source_is_reported(self) -> None:
        link = self.inst.payload / "media" / "linked.bik"
        os.symlink(self.fixture["runtime"] / "media" / "videos" / "title_screen_background.bik", link)
        inv = payload.inventory(self.inst.payload)
        self.assertTrue(any("symlink" in item["reason"] for item in inv["unsafe"]))
        self.assertFalse(any(item["relative_path"] == "media/linked.bik" for item in inv["included"]))

    def test_macos_app_runtime_layout(self) -> None:
        from pz13thlib import platforms

        wrapper = Path(self.tmp.name) / "MacSteam" / "steamapps" / "common" / "ProjectZomboid"
        mac_java = wrapper / "Project Zomboid.app" / "Contents" / "Java"
        mac_java.mkdir(parents=True, exist_ok=True)
        (mac_java / "projectzomboid.jar").write_bytes(b"jar")
        (mac_java / "ProjectZomboid64.json").write_text("{}", encoding="utf-8")
        runtime, layout = detection._runtime_for_wrapper(wrapper)
        self.assertEqual(layout, "macos-app-java")
        self.assertEqual(platforms.layout_for_runtime(wrapper, runtime), "macos-app-java")


if __name__ == "__main__":
    unittest.main()
