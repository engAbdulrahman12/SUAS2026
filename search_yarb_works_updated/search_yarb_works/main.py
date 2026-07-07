#!/usr/bin/env python3
"""
SUAS 2026 — Entry point.
Just launches the GUI. The GUI owns the full mission lifecycle.
"""
import config
from gui import launch_gui

if __name__ == "__main__":
    mode = "SIMULATION" if config.TEST_FLAG else "REAL DRONE"
    print(f"SUAS 2026 | {mode} | Alt={config.MISSION_ALT} m")
    launch_gui()