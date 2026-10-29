"""Product-specific constants for the 13thPandemic launcher.

These are intentionally in one place so a fork can change the modpack identity
without hunting through the implementation.
"""

from __future__ import annotations

APP_ID = "108600"
WORKSHOP_ITEM_ID = "3487726294"
MOD_DIR_NAME = "13thPandemic"
GAME_JAR = "projectzomboid.jar"
GAME_CONFIG = "ProjectZomboid64.json"
DEFAULT_SERVER_HOST = "pz.13thpandemic.net"
ZOMBIE_BUDDY_JAR = "ZombieBuddy.jar"

# Known required server mods and the local load-order anchor for each.
ACTIVE_MODS = (
    ("ProjectVarietyChapterTwo", "ProjectVarietyChapterOne"),
)
