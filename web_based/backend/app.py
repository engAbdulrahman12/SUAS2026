"""SUAS 2026 — Web backend.

Run this process and leave it running. It owns the MAVLink connection,
the mission state machine, MAVProxy, and the camera worker. The browser
(frontend/) is a thin client: if it hangs, reloads, or crashes, this
process keeps flying the mission untouched — reopen the page and it
resyncs from GET /api/state.

Start:  python run.py   (from the project root, see README.md)
"""
import asyncio
import json
import os
import time

import cv2
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
from controller import MissionController, MissionParams, RECEIVED_MAPS_DIR

app = FastAPI(title="SUAS 2026 Mission Backend")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(os.path.dirname(BASE_DIR), "frontend")

ctl = MissionController()

# ── WebSocket broadcast plumbing ─────────────────────────────────
_clients: set[WebSocket] = set()
_main_loop: asyncio.AbstractEventLoop | None = None


def _broadcast(event: dict):
    """Called from controller — may run on any background thread.
    Hops onto the asyncio event loop safely via run_coroutine_threadsafe.
    """
    if _main_loop is None:
        return
    payload = json.dumps(event)
    dead = []

    async def _send_all():
        for ws in list(_clients):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            _clients.discard(ws)

    try:
        asyncio.run_coroutine_threadsafe(_send_all(), _main_loop)
    except Exception:
        pass


@app.on_event("startup")
async def _startup():
    global _main_loop
    _main_loop = asyncio.get_event_loop()
    ctl.set_emit(_broadcast)
    ctl.install_stdout_redirect()


# ── Static frontend ──────────────────────────────────────────────
app.mount("/static", StaticFiles(directory=os.path.join(FRONTEND_DIR, "static")), name="static")

os.makedirs(RECEIVED_MAPS_DIR, exist_ok=True)
app.mount("/received_maps", StaticFiles(directory=RECEIVED_MAPS_DIR), name="received_maps")


@app.get("/")
async def index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


# ── WebSocket: log / status / state / camera-info push channel ──
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    _clients.add(ws)
    # Resync: send recent log history + current state snapshot immediately
    for entry in ctl.log_history[-200:]:
        await ws.send_text(json.dumps(entry))
    for entry in ctl.pi_log_history[-200:]:
        await ws.send_text(json.dumps(entry))
    await ws.send_text(json.dumps({"type": "state", "state": dict(ctl.state)}))
    try:
        while True:
            await ws.receive_text()   # client doesn't send much; just keep alive
    except WebSocketDisconnect:
        pass
    finally:
        _clients.discard(ws)


# ── REST: connection / mode ──────────────────────────────────────
class SimBody(BaseModel):
    sim: bool


@app.post("/api/mode")
async def set_mode(body: SimBody):
    ctl.set_sim(body.sim)
    return {"ok": True}


@app.get("/api/ports")
async def ports():
    return {"ports": ctl.list_ports()}


class MavProxyBody(BaseModel):
    port: str


@app.post("/api/mavproxy/start")
async def mavproxy_start(body: MavProxyBody):
    ctl.start_mavproxy(body.port)
    return {"ok": True}


@app.post("/api/mavproxy/stop")
async def mavproxy_stop():
    ctl.stop_mavproxy()
    return {"ok": True}


# ── REST: mission ────────────────────────────────────────────────
class Waypoint(BaseModel):
    lat: float
    lon: float
    alt: float


class SearchCorner(BaseModel):
    lat: float
    lon: float
    alt: float


class ConnectBody(BaseModel):
    uri: str


@app.post("/api/connect")
async def connect_standalone(body: ConnectBody):
    ctl.connect_standalone(body.uri)
    return {"ok": True}


@app.post("/api/disconnect")
async def disconnect_standalone():
    ctl.disconnect_standalone()
    return {"ok": True}


class MissionStartBody(BaseModel):
    waypoints: list[Waypoint]
    laps: int
    uri: str
    search_corners: list[SearchCorner] | None = None


@app.post("/api/mission/start")
async def mission_start(body: MissionStartBody):
    wps = [(w.lat, w.lon, w.alt) for w in body.waypoints]
    corners = ([(c.lat, c.lon, c.alt) for c in body.search_corners]
               if body.search_corners else None)
    params = MissionParams(waypoints=wps, laps=body.laps, uri=body.uri, search_corners=corners)
    ctl.start_mission(params)
    return {"ok": True}


@app.post("/api/mission/continue")
async def mission_continue():
    ctl.user_continue()
    return {"ok": True}


class PostLapBody(BaseModel):
    choice: str   # "home" | "search"


@app.post("/api/mission/post_lap_choice")
async def mission_post_lap(body: PostLapBody):
    ctl.choose_post_lap(body.choice)
    return {"ok": True}


@app.post("/api/mission/abort")
async def mission_abort():
    ctl.abort()
    return {"ok": True}


@app.get("/api/state")
async def get_state():
    return {"state": dict(ctl.state), "log": ctl.log_history[-200:], "pi_log": ctl.pi_log_history[-200:]}


@app.get("/api/config")
async def get_config():
    """Live values from config.py — the frontend uses these instead of
    hardcoding defaults that could drift out of sync with the backend."""
    return {
        "mission_alt": config.MISSION_ALT,
        "home_lat": config.HOME_LAT,
        "home_lon": config.HOME_LON,
        "default_laps": config.DEFAULT_LAPS,
        "click_alt_step_m": config.CLICK_ALT_STEP_M,
        "camera_mode": config.CAMERA_MODE,
        "webcam_index": config.WEBCAM_INDEX,
        "rtsp_url": config.RTSP_URL,
        "cmd_record_start": config.CMD_RECORD_START,
        "cmd_record_stop": config.CMD_RECORD_STOP,
        "cmd_process_start": config.CMD_PROCESS_START,
        "cmd_send_map": config.CMD_SEND_MAP,
    }


# ── REST: camera interaction ─────────────────────────────────────
class ClickBody(BaseModel):
    px: float
    py: float
    w: int
    h: int


@app.post("/api/camera/click")
async def camera_click(body: ClickBody):
    ctl.on_camera_click(body.px, body.py, body.w, body.h)
    return {"ok": True}


class AltBody(BaseModel):
    direction: str   # "u" | "d"


@app.post("/api/camera/alt")
async def camera_alt(body: AltBody):
    ctl.on_alt_key(body.direction)
    return {"ok": True}


# ── MJPEG camera stream ──────────────────────────────────────────
def _mjpeg_generator():
    """Yields multipart JPEG frames. Browser <img src="/video_feed"> decodes
    this natively — no JS-side video loop, no base64, minimal overhead."""
    boundary = b"--frame"
    placeholder_sent = False
    while True:
        frame, dets = ctl.get_camera_frame()
        if frame is None:
            if not placeholder_sent:
                placeholder_sent = True
            time.sleep(0.1)
            continue
        placeholder_sent = False
        for (x1, y1, x2, y2, label, conf) in dets:
            cv2.rectangle(frame, (int(x1), int(y1)), (int(x2), int(y2)), (0, 255, 0), 2)
            cv2.putText(frame, f"{label} {conf:.2f}", (int(x1), max(12, int(y1) - 6)),
                       cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if not ok:
            continue
        yield (boundary + b"\r\nContent-Type: image/jpeg\r\nContent-Length: " +
               str(len(buf)).encode() + b"\r\n\r\n" + buf.tobytes() + b"\r\n")
        time.sleep(0.03)   # ~30 fps cap on the stream loop


@app.get("/video_feed")
async def video_feed():
    return StreamingResponse(_mjpeg_generator(),
                             media_type="multipart/x-mixed-replace; boundary=frame")


class CameraStartBody(BaseModel):
    mode: str            # "webcam" | "rtsp"
    source: str | None = None   # webcam index (as string) or RTSP URL; blank -> config default


@app.post("/api/camera/start")
async def camera_start(body: CameraStartBody):
    source = body.source
    if body.mode == "webcam" and source not in (None, ""):
        try:
            source = int(source)
        except ValueError:
            return {"ok": False, "error": "Webcam source must be a number (device index)."}
    elif source == "":
        source = None
    ctl.start_camera(mode=body.mode, source=source)
    return {"ok": True}


@app.post("/api/camera/stop")
async def camera_stop():
    ctl.stop_camera()
    return {"ok": True}


@app.get("/api/camera/status")
async def camera_status():
    return {"active": ctl.cam_active, "click_to_fly": ctl.click_to_fly_enabled,
           "mode": config.CAMERA_MODE}


# ── REST: Pi companion-computer signalling ───────────────────────
@app.post("/api/pi/recording/start")
async def pi_recording_start():
    ctl.send_text_command(config.CMD_RECORD_START, "Start Recording")
    return {"ok": True}


@app.post("/api/pi/recording/stop")
async def pi_recording_stop():
    ctl.send_text_command(config.CMD_RECORD_STOP, "Stop Recording")
    return {"ok": True}


@app.post("/api/pi/processing/start")
async def pi_processing_start():
    ctl.send_text_command(config.CMD_PROCESS_START, "Start Processing")
    return {"ok": True}


@app.post("/api/pi/map/send")
async def pi_map_send():
    ctl.send_text_command(config.CMD_SEND_MAP, "Send Map")
    return {"ok": True}


@app.post("/api/pi_link/start")
async def pi_link_start():
    ctl.start_status_listener()
    return {"ok": True}


@app.post("/api/pi_link/stop")
async def pi_link_stop():
    ctl.stop_status_listener()
    return {"ok": True}
