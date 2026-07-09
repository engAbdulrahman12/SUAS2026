# SUAS 2026 — Web Mission Planner

Same mission logic as the desktop app (`flight.py`, `mission.py`, `connection.py`,
`geo.py`, `vision.py`, `config.py` are unchanged, byte-for-byte behavior),
now split into a **backend process** (owns the drone, flies the mission) and
a **browser tab** (just displays it). This is the important part:

> **If the browser tab freezes, reloads, or crashes, the mission keeps flying.**
> The backend never depends on the browser being open. Reopen
> `http://127.0.0.1:8000` and the page resyncs to whatever the mission is
> currently doing — waypoints already flying, camera feed already live,
> post-lap choice already pending, all of it.

The only thing that stops the mission is closing the **terminal window**
running `run.py`, or the process crashing outright — which is no different
from a desktop app closing, except now a stray UI redraw or a frozen dialog
can never take the mission down with it.

**Your ArduPilot failsafes (RTL on lost GCS heartbeat, RC loss, battery, etc.)
are still your real last line of defense.** No app-layer architecture
replaces those — double check they're configured before you fly.

## Setup

```bash
cd suas_web
python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # macOS/Linux

pip install -r requirements.txt
```

If you're using the RTSP + AI detection camera mode, also:
```bash
pip install ultralytics
```

## Run

```bash
python run.py
```

This starts the backend on `http://127.0.0.1:8000` and opens it in your
default browser automatically. Leave the terminal window open for the
whole flight.

To reconnect from another tab, another window, or after a crash: just open
`http://127.0.0.1:8000` again. No state is lost.

## What changed vs. the desktop app

- `gui.py` → replaced by `backend/app.py` (FastAPI + WebSocket) and
  `frontend/` (plain HTML/CSS/JS, no build step).
- `backend/controller.py` is the new home for the mission orchestration
  that used to live in `gui.py`'s `_run()` / `_abort()` / dialog methods —
  same sequence of calls into `flight.py` / `mission.py` / `connection.py`,
  just emitting events over a queue instead of calling Tkinter widgets
  directly.
- Camera feed is served as MJPEG (`GET /video_feed`) — the browser's
  `<img>` tag decodes this natively, no JS video loop needed. Click-to-fly
  and the U/D altitude nudge post to `/api/camera/click` and
  `/api/camera/alt`.
- All log lines — including raw progress prints inside `flight.py` /
  `connection.py` / `mission.py` — are mirrored to the browser via a
  stdout/stderr tee, same as the old GUI's log redirect.

## Project layout

```
suas_web/
  run.py                  ← start here
  requirements.txt
  backend/
    app.py                ← FastAPI routes + WebSocket + MJPEG stream
    controller.py          ← mission state machine (the safety-critical part)
    config.py               (unchanged from your original)
    connection.py           (unchanged)
    flight.py               (unchanged)
    geo.py                  (unchanged)
    mission.py               (unchanged)
    vision.py                (unchanged)
  frontend/
    index.html
    static/
      style.css
      app.js
```

## Notes / things to double-check on your machine

- I validated every Python file compiles (`py_compile`) and the JS file
  parses cleanly (`node --check`), but I don't have network access in this
  environment to actually install `fastapi`/`uvicorn`/`opencv-python` and
  run a live end-to-end test against a real or SITL vehicle. Please run a
  SITL pass yourself before trusting this for a real flight, the same way
  you'd want to test any GUI rewrite.
- `mission.waypoints` gets written into `backend/` (same relative-path logic
  as before) — load that file path into Mission Planner's PLAN tab to verify,
  same as your current workflow.
- The frontend now pulls `MISSION_ALT` / `HOME_LAT` / `HOME_LON` live from
  `/api/config` instead of hardcoding them, so editing `config.py` and
  restarting the backend is all that's needed to update the dashboard.
