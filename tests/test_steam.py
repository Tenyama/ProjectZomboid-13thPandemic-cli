"""Steam VDF and launch-option parsing tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pz13thlib import steam
from tests.support import make_install, write


class SteamTests(unittest.TestCase):
    def test_parse_vdf_nested(self) -> None:
        parsed = steam.parse_vdf(
            '"root"\n{\n  "apps"\n  {\n    "108600"\n    {\n      "LaunchOptions" "-javaagent:ZombieBuddy.jar --"\n    }\n  }\n}\n'
        )
        self.assertEqual(
            parsed["root"]["apps"]["108600"]["LaunchOptions"],
            "-javaagent:ZombieBuddy.jar --",
        )

    def test_find_launch_options(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = make_install(Path(tmp))
            config = fixture["library"] / "userdata" / "101" / "config" / "localconfig.vdf"
            write(
                config,
                '"UserLocalConfigStore"\n{\n  "Software"\n  {\n    "Valve"\n    {\n      "Steam"\n      {\n'
                '        "apps"\n        {\n          "108600"\n          {\n'
                '            "LaunchOptions" "-javaagent:ZombieBuddy.jar --"\n'
                "          }\n        }\n      }\n    }\n  }\n}\n",
            )
            result = steam.find_launch_options([fixture["library"]], "108600")
            self.assertEqual(len(result["entries"]), 1)
            self.assertEqual(result["entries"][0]["userdata"], "101")
            self.assertEqual(result["entries"][0]["options"], "-javaagent:ZombieBuddy.jar --")

    def test_library_discovery_uses_libraryfolders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fixture = make_install(Path(tmp))
            libraries = steam.discover_libraries(explicit=fixture["library"])
            self.assertEqual(libraries, [fixture["library"].resolve()])


if __name__ == "__main__":
    unittest.main()
