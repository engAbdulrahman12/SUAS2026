#!/usr/bin/env python3
"""
SUAS 2026 — Web launcher.

Starts the FastAPI backend (owns the drone connection + mission logic) and
opens the dashboard in your default browser. Leave the terminal window
running for the whole flight — closing THIS window stops the backend.
Closing/reloading/crashing the BROWSER TAB does not: reopen
http://127.0.0.1:8000 and it resyncs to whatever the mission is doing.
"""
import os
import sys
import threading
import time
import webbrowser

import uvicorn

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.join(BASE_DIR, "backend")

HOST = "127.0.0.1"
PORT = 8000


def _open_browser():
    time.sleep(1.2)
    webbrowser.open(f"http://{HOST}:{PORT}")


if __name__ == "__main__":
    threading.Thread(target=_open_browser, daemon=True).start()
    print(f"SUAS 2026 backend starting → http://{HOST}:{PORT}")
    print("Keep this window open for the whole flight.")
    uvicorn.run("app:app", host=HOST, port=PORT, app_dir=BACKEND_DIR, reload=False,
               log_level="info")
