"""Mission controller — owns the drone connection, mission state machine,
MAVProxy subprocess, and camera worker. Has ZERO knowledge of any GUI or
web framework: it just calls `self._emit(event_dict)` for every log line,
status change, or state update, and something else (app.py) is responsible
for pushing those events out over a WebSocket.

This is the safety-critical split: this module keeps flying the mission
even if every browser tab is closed, frozen, or crashed. The browser is a
window onto this process — never the thing keeping it alive.
"""
import os
import shutil
import site
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

# Custom dialect MUST be set before any mavlink_connection() is created —
# see image_transfer_dialect/SETUP.md for the one-time generation/install
# step this depends on (both this machine and the Pi need it).
from pymavlink import mavutil

from image_transfer import ImageReceiver, ImageTransferConfig, IMGACK_PREFIX, IMGFAIL_PREFIX

import config

RECEIVED_MAPS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "received_maps")


def _guess_level(text: str) -> str:
    """Same heuristic the old Tkinter log box used for raw print() output
    (which carries no explicit level) — keeps colour-coding consistent."""
    if any(x in text for x in ["✓", "OK", "ready", "accepted", "Armed", "complete", "onnected"]):
        return "ok"
    if any(x in text for x in ["Error", "error", "FAIL", "fail", "Timeout", "timeout"]):
        return "error"
    if any(x in text for x in ["warn", "WARN", "RTL", "Interrupt", "Abort"]):
        return "warn"
    return "plain"


class _StdoutTee:
    """Redirects stdout/stderr so every print() in flight.py / connection.py /
    mission.py / vision.py (progress updates, GPS waiting, upload progress...)
    reaches the browser log too — not just the explicit controller._log calls.
    Still writes through to the real stdout so the server console shows it.
    """
    def __init__(self, controller, orig_stream):
        self._ctl = controller
        self._orig = orig_stream

    def write(self, text):
        if text.strip():
            entry = {"type": "log", "text": text.rstrip(), "level": _guess_level(text),
                     "ts": time.time()}
            with self._ctl.lock:
                self._ctl.log_history.append(entry)
                self._ctl.log_history = self._ctl.log_history[-500:]
            self._ctl._emit(entry)
        self._orig.write(text)

    def flush(self):
        self._orig.flush()

    def isatty(self):
        return False


@dataclass
class MissionParams:
    waypoints: list
    laps: int
    uri: str
    search_corners: object   # [(lat,lon,alt) x4] or None to skip


class MissionController:
    def __init__(self):
        self._emit_cb = lambda event: None   # set by app.py
        self.lock = threading.RLock()

        # connection / process state
        self.conn = None
        self.mav_proc = None
        self.mission_thread = None

        # camera
        self.camera = None
        self.cam_active = False
        self.click_to_fly_enabled = False

        # dedicated read-only connection for incoming Pi STATUSTEXT messages
        # (separate socket from self.conn — never races the mission thread's
        # recv_match() loop)
        self.status_conn = None
        self.status_listener_thread = None
        self.status_listener_running = False

        # Image receiver (standard MAVLink messages only — STATUSTEXT +
        # DATA_TRANSMISSION_HANDSHAKE + ENCAPSULATED_DATA, no custom dialect) —
        # wired to emit the same "map_transfer" event shape the frontend
        # already understands (start/progress/done/failed), so no frontend
        # changes were needed for this swap.
        self.image_receiver = ImageReceiver(
            config=ImageTransferConfig(output_dir=RECEIVED_MAPS_DIR, timeout_s=30.0),
            on_progress=self._on_image_progress,
            on_complete=self._on_image_complete,
            on_log=lambda text: self._log(text, "info"),
            on_resend_request=self._on_image_resend_request,
            on_ack=self._on_image_ack,
        )

        # mission flow control (mirrors the old GUI's Continue/Abort buttons)
        self.continue_ev = threading.Event()
        self.post_lap_choice = None
        self.post_lap_ev = threading.Event()

        # snapshot state for clients that (re)connect mid-mission
        self.state = {
            "sim": bool(config.TEST_FLAG),
            "mav_running": False,
            "mav_port": None,
            "mission_running": False,
            "awaiting_continue": False,
            "awaiting_post_lap": False,
            "search_available": False,
            "click_to_fly_enabled": False,
            "status_text": "Ready",
            "status_level": "info",
            "armed": False,
            "conn_active": False,
            "cam_active": False,
            "pi_link_active": False,
            "pi_last_message": None,
        }
        self.log_history = []   # last N mission-log lines, for resync on reconnect
        self.pi_log_history = []   # last N Pi STATUSTEXT messages, kept separate

    # ── wiring ───────────────────────────────────────────────────
    def set_emit(self, cb):
        self._emit_cb = cb

    def install_stdout_redirect(self):
        """Call once at startup, after set_emit(). Mirrors every print()
        from anywhere in the backend into the browser log stream."""
        sys.stdout = _StdoutTee(self, sys.__stdout__)
        sys.stderr = _StdoutTee(self, sys.__stderr__)

    def _emit(self, event: dict):
        self._emit_cb(event)

    def _log(self, text: str, level: str = "plain"):
        entry = {"type": "log", "text": text, "level": level, "ts": time.time()}
        with self.lock:
            self.log_history.append(entry)
            self.log_history = self.log_history[-500:]
        print(text)
        self._emit(entry)

    def _set_status(self, text: str, level: str = "info"):
        with self.lock:
            self.state["status_text"] = text
            self.state["status_level"] = level
        self._emit({"type": "status", "text": text, "level": level})

    def _push_state(self):
        with self.lock:
            snap = dict(self.state)
        self._emit({"type": "state", "state": snap})

    # ── ports / mode ─────────────────────────────────────────────
    @staticmethod
    def list_ports():
        try:
            import serial.tools.list_ports
            p = [x.device for x in serial.tools.list_ports.comports()]
            return sorted(p) if p else []
        except ImportError:
            return []

    def set_sim(self, sim: bool):
        config.TEST_FLAG = 1 if sim else 0
        with self.lock:
            self.state["sim"] = sim
        self._log(f"[MODE] {'SITL' if sim else 'Real Drone'}", "info")
        self._push_state()

    # ── MAVProxy ─────────────────────────────────────────────────
    def _find_mav(self):
        f = shutil.which("mavproxy.py")
        if f:
            return f
        s = os.path.join(os.path.dirname(sys.executable), "Scripts", "mavproxy.py")
        if os.path.exists(s):
            return s
        try:
            for d in site.getsitepackages():
                p = os.path.join(os.path.dirname(d), "Scripts", "mavproxy.py")
                if os.path.exists(p):
                    return p
        except Exception:
            pass
        return None

    def start_mavproxy(self, port: str):
        if self.mav_proc and self.mav_proc.poll() is None:
            self._log("[MAVProxy] Already running.", "warn")
            return
        mp = self._find_mav()
        if not mp:
            self._log("[MAVProxy] Not found. Run: py -m pip install mavproxy", "error")
            return
        cmd = [sys.executable, mp, f"--master={port}",
               f"--baudrate={config.BAUD_RATE}",
               "--out=udp:127.0.0.1:14550",   # Mission Planner
               "--out=udp:127.0.0.1:14552",   # this app's control connection
               "--out=udp:127.0.0.1:14553"]   # dedicated read-only Pi status listener
        self._log(f"[MAVProxy] {' '.join(cmd)}", "info")

        # MAVProxy's interactive "MAV>" prompt (prompt_toolkit) needs a REAL
        # Windows console screen buffer. If we pipe stdout to capture it in
        # our log, Windows gives it no console at all and it crashes with
        # NoConsoleScreenBufferError. So on Windows we give it its own real
        # console window instead of piping — we lose in-app log capture for
        # MAVProxy specifically, but it's how the tool is actually designed
        # to run. Other platforms don't have this issue, so keep piping there.
        try:
            if sys.platform == "win32":
                self.mav_proc = subprocess.Popen(
                    cmd, creationflags=subprocess.CREATE_NEW_CONSOLE)
                self._log("[MAVProxy] Launched in its own console window — "
                          "check that window directly for link status.", "info")
            else:
                self.mav_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                                 stderr=subprocess.STDOUT, text=True, bufsize=1)
                threading.Thread(target=self._mav_stream, daemon=True).start()
        except Exception as e:
            self._log(f"[MAVProxy] Failed: {e}", "error")
            return
        with self.lock:
            self.state["mav_running"] = True
            self.state["mav_port"] = port
        self._push_state()
        if sys.platform == "win32":
            threading.Thread(target=self._mav_watch_exit, daemon=True).start()

    def _mav_watch_exit(self):
        """Windows path: no piped stdout to read, so just watch for the
        process exiting and update state accordingly."""
        self.mav_proc.wait()
        self._log("[MAVProxy] Console window closed.", "warn")
        with self.lock:
            self.state["mav_running"] = False
        self._push_state()

    def _mav_stream(self):
        for line in self.mav_proc.stdout:
            line = line.rstrip()
            if line:
                tag = ("error" if "ERROR" in line.upper() else
                       "warn" if "WARN" in line.upper() else "mav")
                self._log(f"[MAV] {line}", tag)
        self._log("[MAVProxy] Stopped.", "warn")
        with self.lock:
            self.state["mav_running"] = False
        self._push_state()

    def stop_mavproxy(self):
        if self.mav_proc:
            try:
                self.mav_proc.terminate()
            except Exception:
                pass
            self.mav_proc = None
        with self.lock:
            self.state["mav_running"] = False
        self._push_state()

    # ── Pi signalling (text commands over STATUSTEXT) ───────────
    def send_text_command(self, command: str, label: str):
        if self.conn is None:
            self._log(f"[PI-CMD] Not connected — cannot send {label}.", "error")
            return
        from pymavlink import mavutil
        payload = command.encode("utf-8")[:50]
        try:
            self.conn.mav.statustext_send(mavutil.mavlink.MAV_SEVERITY_INFO, payload)
            self._log(f"[PI-CMD] {label} → sent '{command}'", "info")
        except Exception as e:
            self._log(f"[PI-CMD] {label} failed: {e}", "error")

    # ── Pi status listener (dedicated read-only connection) ─────
    _SEVERITY_TO_LEVEL = {0: "error", 1: "error", 2: "error", 3: "error",
                          4: "warn", 5: "warn", 6: "info", 7: "plain"}

    def start_status_listener(self, uri: str = None):
        if self.status_listener_running:
            return
        target_uri = uri or config.default_status_uri()

        def _run():
            from connection import connect as _connect
            try:
                self.status_conn = _connect(uri=target_uri)
            except Exception as e:
                self._log(f"[PI-LINK] Status listener failed to connect ({target_uri}): {e}", "error")
                return
            self.status_listener_running = True
            with self.lock:
                self.state["pi_link_active"] = True
            self._push_state()
            self._log(f"[PI-LINK] Listening for Pi status on {target_uri}", "ok")
            while self.status_listener_running:
                try:
                    msg = self.status_conn.recv_match(
                        type=["STATUSTEXT", "DATA_TRANSMISSION_HANDSHAKE", "ENCAPSULATED_DATA"],
                        blocking=True, timeout=2)
                except Exception:
                    break

                # Let the image receiver notice a stalled transfer on its own
                # schedule (it has no other way to detect this between messages).
                try:
                    self.image_receiver.check_timeout()
                except Exception as e:
                    print(f"[PI-LINK] check_timeout error: {e}", file=sys.__stdout__)

                if msg is None:
                    continue

                # Dispatch is wrapped so ONE bad/unexpected message can never
                # silently kill this whole background thread — without this,
                # any exception here (bad field, decode error, whatever) would
                # break out of the loop permanently with no visible error.
                try:
                    mtype = msg.get_type()
                    if mtype == "STATUSTEXT":
                        # IMGMETA-prefixed lines are our own image-transfer metadata,
                        # not a genuine Pi status message -- let the receiver consume
                        # those, and only log the rest as normal Pi status text.
                        if not self.image_receiver.handle_message(msg):
                            self._handle_pi_statustext(msg)
                    elif mtype in ("DATA_TRANSMISSION_HANDSHAKE", "ENCAPSULATED_DATA"):
                        self.image_receiver.handle_message(msg)
                except Exception as e:
                    print(f"[PI-LINK] Error handling {msg.get_type()}: {e}", file=sys.__stdout__)
                    import traceback
                    traceback.print_exc(file=sys.__stdout__)

            with self.lock:
                self.state["pi_link_active"] = False
            self._push_state()

        self.status_listener_thread = threading.Thread(target=_run, daemon=True)
        self.status_listener_thread.start()

    def _handle_pi_statustext(self, msg):
        text = (msg.text or "").rstrip("\x00")
        if text.startswith("CMD:"):
            return   # this is our own outgoing command echoed back by the link, not a Pi message
        level = self._SEVERITY_TO_LEVEL.get(getattr(msg, "severity", 6), "info")
        entry = {"type": "pi_status", "text": text, "level": level, "ts": time.time()}
        with self.lock:
            self.pi_log_history.append(entry)
            self.pi_log_history = self.pi_log_history[-500:]
            self.state["pi_last_message"] = text
        self._emit(entry)
        print(f"[PI] {text}", file=sys.__stdout__)   # bypasses the tee — console only

    # ── Map image reception (standard MAVLink messages, no custom dialect) ──
    # Actual reassembly/CRC verification lives in image_transfer.ImageReceiver
    # (self.image_receiver, wired up in __init__) — these two methods just
    # adapt its callbacks to the "map_transfer" websocket event shape the
    # frontend already understands, so no frontend changes were needed.
    def _on_image_progress(self, received, total, pct):
        if received == 0:
            self._emit({"type": "map_transfer", "phase": "start",
                       "packets": total, "size": None})
        else:
            self._emit({"type": "map_transfer", "phase": "progress",
                       "received": received, "packets": total, "pct": pct})

    def _on_image_complete(self, path, ok, reason):
        if ok:
            filename = os.path.basename(path)
            size = os.path.getsize(path)
            self._log(f"[IMG] Map received and saved → {filename} ({size} bytes)", "ok")
            self._emit({"type": "map_transfer", "phase": "done", "filename": filename, "size": size})
        else:
            self._log(f"[IMG] Map transfer failed: {reason}", "error")
            self._emit({"type": "map_transfer", "phase": "failed", "reason": reason})

    def _on_image_ack(self, image_id: int, ok: bool):
        """Sends the confirmation the sender is actually waiting for
        before it considers the transfer done. Sent multiple times if the
        sender keeps nudging with IMGDONE (meaning our first ack likely
        got lost on the way) -- idempotent on the sender's side either way."""
        if self.conn is None:
            return
        prefix = IMGACK_PREFIX if ok else IMGFAIL_PREFIX
        text = f"{prefix}{image_id:08x}"
        try:
            self.conn.mav.statustext_send(mavutil.mavlink.MAV_SEVERITY_INFO, text.encode("utf-8")[:50])
        except Exception as e:
            self._log(f"[IMG] Failed to send {'ack' if ok else 'fail'} confirmation: {e}", "error")

    def _on_image_resend_request(self, image_id: int, missing_seqs: list):
        """Sends an IMGRESEND STATUSTEXT back to the Pi over the normal
        read/write connection (the status listener is read-only, so this
        can't go out over that one). A STATUSTEXT is capped at 50 bytes,
        so a long list of missing packet numbers gets split across
        multiple messages rather than silently truncated/corrupted."""
        if self.conn is None:
            self._log("[IMG] Cannot request resend — not connected.", "warn")
            return
        prefix = f"IMGRESEND:{image_id:08x}:"
        budget = 50 - len(prefix.encode("utf-8"))
        parts = [str(s) for s in missing_seqs]

        batch, batch_len = [], 0
        for part in parts:
            add_len = len(part) + (1 if batch else 0)
            if batch_len + add_len > budget and batch:
                self._send_resend_batch(prefix, batch)
                batch, batch_len = [], 0
                add_len = len(part)
            batch.append(part)
            batch_len += add_len
        if batch:
            self._send_resend_batch(prefix, batch)

    def _send_resend_batch(self, prefix: str, batch: list):
        text = prefix + ",".join(batch)
        try:
            self.conn.mav.statustext_send(mavutil.mavlink.MAV_SEVERITY_INFO, text.encode("utf-8")[:50])
        except Exception as e:
            self._log(f"[IMG] Failed to send resend request: {e}", "error")

    def stop_status_listener(self):
        self.status_listener_running = False
        if self.status_conn:
            try:
                self.status_conn.close()
            except Exception:
                pass
        self.status_conn = None

    # ── mission flow control (replaces Continue / post-lap dialog) ──
    def user_continue(self):
        with self.lock:
            self.state["awaiting_continue"] = False
        self._push_state()
        self.continue_ev.set()

    def choose_post_lap(self, choice: str):
        """choice: 'home' or 'search'"""
        self.post_lap_choice = choice
        with self.lock:
            self.state["awaiting_post_lap"] = False
        self._push_state()
        self.post_lap_ev.set()

    def _ask_post_lap_choice(self, search_available: bool) -> str:
        self.post_lap_ev.clear()
        self.post_lap_choice = None
        with self.lock:
            self.state["awaiting_post_lap"] = True
            self.state["search_available"] = search_available
        self._push_state()
        self._log("[MAIN] Laps complete — choose Return Home or Go to Search Area.", "info")
        self.post_lap_ev.wait()
        return self.post_lap_choice or "home"

    # ── abort ────────────────────────────────────────────────────
    def abort(self):
        self._log("[ABORT] RTL commanded.", "warn")
        self.click_to_fly_enabled = False
        with self.lock:
            self.state["click_to_fly_enabled"] = False
        conn = self.conn
        if conn:
            def _do_rtl():
                try:
                    from flight import rtl_and_land
                    rtl_and_land(conn)
                except Exception as e:
                    self._log(f"[ABORT] RTL error: {e}", "error")
                finally:
                    try:
                        conn.close()
                    except Exception as e:
                        self._log(f"[ABORT] Error closing connection: {e}", "warn")
                    self.conn = None
                    self.stop_status_listener()
                    with self.lock:
                        self.state["mission_running"] = False
                        self.state["conn_active"] = False
                        self.state["armed"] = False
                    self._push_state()
            threading.Thread(target=_do_rtl, daemon=True).start()
        self._set_status("Aborted — RTL", "warn")

    # ── standalone connect (independent of running a full mission) ──
    def connect_standalone(self, uri: str):
        """Connects to the vehicle without going through the waypoint/
        arm/takeoff mission flow — just enough to use click-to-fly and
        the Pi Recording/Processing/Send Map controls on the bench."""
        if self.conn is not None:
            self._log("[CONN] Already connected.", "warn")
            return

        def _run():
            from connection import connect as _connect
            self._log(f"[CONN] Connecting → {uri}", "info")
            self._set_status("Connecting…", "info")
            try:
                conn = _connect(uri=uri)
            except Exception as e:
                self._log(f"[CONN] Connect failed: {e}", "error")
                self._set_status("Connect failed", "error")
                return
            self.conn = conn
            self.start_status_listener()
            self.click_to_fly_enabled = True
            with self.lock:
                self.state["conn_active"] = True
                self.state["click_to_fly_enabled"] = True
            self._push_state()
            self._log("[CONN] Connected ✓ (standalone — no mission required)", "ok")
            self._set_status("Connected (standalone)", "ok")

        threading.Thread(target=_run, daemon=True).start()

    def disconnect_standalone(self):
        if self.conn is None:
            self._log("[CONN] Not connected.", "warn")
            return
        if self.mission_thread is not None and self.mission_thread.is_alive():
            self._log("[CONN] A mission is running — use Abort instead of Disconnect.", "warn")
            return
        try:
            self.conn.close()
        except Exception as e:
            self._log(f"[CONN] Error closing connection: {e}", "warn")
        self.conn = None
        self.click_to_fly_enabled = False
        self.stop_status_listener()
        with self.lock:
            self.state["conn_active"] = False
            self.state["click_to_fly_enabled"] = False
        self._push_state()
        self._log("[CONN] Disconnected.", "info")
        self._set_status("Ready", "info")

    # ── camera (independent of mission state — bench-testable anytime) ──
    def start_camera(self, mode: str = None, source=None):
        if self.cam_active:
            self._log("[VISION] Camera already running — stop it first to change source.", "warn")
            return
        try:
            from vision import CameraWorker
            self.camera = CameraWorker(mode=mode, source=source)
            self.camera.start()
        except Exception as e:
            self._log(f"[VISION] Camera failed to start: {e}", "error")
            self.camera = None
            return
        self.cam_active = True
        with self.lock:
            self.state["cam_active"] = True
        self._push_state()
        effective_mode = self.camera.mode
        ai_active = effective_mode == "rtsp" and self.camera._model is not None
        if effective_mode == "rtsp":
            info = (f"RTSP feed + AI detection running ({self.camera.source})." if ai_active
                    else f"RTSP feed only, no AI model loaded ({self.camera.source}).")
        else:
            info = f"Webcam feed, no AI (source={self.camera.source})."
        self._log(f"[VISION] {info}", "ok")
        self._emit({"type": "camera_info", "text": info})

    def stop_camera(self):
        self.cam_active = False
        self.click_to_fly_enabled = False
        with self.lock:
            self.state["click_to_fly_enabled"] = False
            self.state["cam_active"] = False
        self._push_state()
        if self.camera:
            self.camera.stop()
        self.camera = None
        self._log("[VISION] Camera stopped.", "info")

    def get_camera_frame(self):
        if not self.cam_active or self.camera is None:
            return None, []
        return self.camera.get_frame()

    def _enable_click_to_fly(self):
        self.click_to_fly_enabled = True
        with self.lock:
            self.state["click_to_fly_enabled"] = True
        self._push_state()
        self._set_status("Mapping complete — click the camera feed to fly to that GPS point", "info")

    def on_camera_click(self, px: float, py: float, w: int, h: int):
        if not self.click_to_fly_enabled or self.conn is None:
            return
        self._log(f"[CLICK] pixel=({px:.0f},{py:.0f}) of {w}x{h}", "info")
        from flight import fly_to_clicked_point
        threading.Thread(target=fly_to_clicked_point,
                         args=(self.conn, px, py, w, h), daemon=True).start()

    def on_alt_key(self, direction: str):
        if not self.click_to_fly_enabled or self.conn is None:
            return
        down_m = config.CLICK_ALT_STEP_M if direction == "d" else -config.CLICK_ALT_STEP_M
        self._log(f"[ALT] {'Descend' if down_m > 0 else 'Climb'} {abs(down_m):.1f} m", "info")
        from flight import nudge_body
        threading.Thread(target=nudge_body, args=(self.conn, 0.0, 0.0, down_m), daemon=True).start()

    # ── mission execution ────────────────────────────────────────
    def start_mission(self, params: MissionParams):
        if self.mission_thread and self.mission_thread.is_alive():
            self._log("[MAIN] Mission already running.", "warn")
            return
        with self.lock:
            self.state["mission_running"] = True
        self._push_state()
        self._set_status("Running…", "warn")
        self.mission_thread = threading.Thread(target=self._run, args=(params,), daemon=True)
        self.mission_thread.start()

    def _run(self, params: MissionParams):
        import config as cfg
        from connection import connect, wait_gps
        from flight import arm, fly_to, rtl_and_land, set_fixed_home, set_mode, set_param, takeoff
        from mission import WP_FILE, build_items, save_waypoints_file, upload_mission

        armed = False
        manual_handoff = False
        try:
            # Phase 1 — save preview waypoints, wait for operator to verify
            p0 = params.waypoints[0]
            items = build_items(p0[0], p0[1], params.waypoints, params.laps,
                                home_lat=p0[0], home_lon=p0[1])
            save_waypoints_file(items)
            self._log(f"✓ {WP_FILE} saved — load in MP PLAN tab to verify.", "ok")
            self._log("After verifying waypoints in Mission Planner, click Continue →", "info")
            self.continue_ev.clear()
            with self.lock:
                self.state["awaiting_continue"] = True
            self._push_state()
            self.continue_ev.wait()

            # Phase 2 — connect
            self._log(f"[CONN] Connecting → {params.uri}", "info")
            self._set_status("Connecting…", "info")
            conn = connect(uri=params.uri)
            self.conn = conn
            self.start_status_listener()
            with self.lock:
                self.state["conn_active"] = True
            self._push_state()
            take_lat, take_lon = wait_gps(conn, simulation=bool(cfg.TEST_FLAG))
            msg = conn.recv_match(type="GLOBAL_POSITION_INT", blocking=True, timeout=5)
            alt_msl = (msg.alt / 1000.0) if msg else 0.0
            home_lat = cfg.HOME_LAT or take_lat
            home_lon = cfg.HOME_LON or take_lon
            home_alt = cfg.HOME_ALT_MSL or alt_msl
            self._log(f"[MAIN] Takeoff: {take_lat:.8f}, {take_lon:.8f}", "info")
            self._log(f"[MAIN] HOME   : {home_lat:.8f}, {home_lon:.8f}", "info")

            items = build_items(take_lat, take_lon, params.waypoints, params.laps,
                                home_lat=home_lat, home_lon=home_lon)
            save_waypoints_file(items)
            set_param(conn, "RTL_ALT", cfg.MISSION_ALT * 100)
            upload_mission(conn, items)
            self._log("✓ Mission uploaded.", "ok")

            # Phase 3 — fly
            set_mode(conn, "GUIDED")
            arm(conn)
            armed = True
            with self.lock:
                self.state["armed"] = True
            self._push_state()
            self._set_status("Flying", "ok")
            takeoff(conn, cfg.MISSION_ALT)
            set_fixed_home(conn, home_lat, home_lon, home_alt)

            for lap in range(1, params.laps + 1):
                self._log(f"[MAIN] ── Lap {lap}/{params.laps} ──", "info")
                prev = cfg.MISSION_ALT
                for i, wp in enumerate(params.waypoints, 1):
                    lat, lon, alt = wp
                    if abs(alt - prev) > 0.5:
                        self._log(f"[MAIN] Alt {prev:.1f}→{alt:.1f} m", "info")
                        if not fly_to(conn, lat, lon, alt):
                            raise TimeoutError(f"Alt-adjust timeout lap {lap} WP {i}")
                    if not fly_to(conn, lat, lon, alt):
                        raise TimeoutError(f"Timeout lap {lap} WP {i}")
                    prev = alt

            # Phase 4 — ask: home or search?
            choice = self._ask_post_lap_choice(search_available=bool(params.search_corners))

            if choice == "search" and params.search_corners:
                from mission import build_straight_line_path
                self._log("[SEARCH] Building straight-line pass along the longest side...", "info")
                corners_latlon = [(c[0], c[1]) for c in params.search_corners]
                search_alt = params.search_corners[0][2]
                path = build_straight_line_path(corners_latlon, alt=search_alt)
                self._log(f"[SEARCH] {len(path)}-point straight-line pass generated.", "info")

                self._log("[SEARCH] Starting camera feed...", "info")
                threading.Thread(target=self.start_camera, daemon=True).start()

                self._log("[MAIN] ── Search / mapping ──", "info")
                for i, (lat, lon, alt) in enumerate(path, 1):
                    self._log(f"[SEARCH] Leg {i}/{len(path)}", "info")
                    if not fly_to(conn, lat, lon, alt):
                        raise TimeoutError(f"Search leg {i} timeout")
                self._log("[SEARCH] Mapping complete ✓", "ok")

                self._enable_click_to_fly()
                self._log("[MAIN] Click the Camera Feed tab to fly to a marked object's real "
                          "GPS position. Use RTL / Abort when ready to return home.", "info")
                self._set_status("Manual visual approach — click camera feed, or RTL when done", "info")
                manual_handoff = True
                return

            rtl_and_land(conn, home_lat, home_lon)
            self._log("[MAIN] Mission complete ✓", "ok")
            self._set_status("Mission complete ✓", "ok")

        except Exception as e:
            self._log(f"[MAIN] Error: {e}", "error")
            self._set_status(f"Error: {e}", "error")
            if armed and self.conn:
                self._log("[MAIN] RTL for safety.", "warn")
                try:
                    from flight import rtl_and_land
                    rtl_and_land(self.conn)
                except Exception as re:
                    self._log(f"[MAIN] RTL failed: {re}", "error")
        finally:
            with self.lock:
                self.state["awaiting_continue"] = False
            if not manual_handoff:
                if self.conn is not None:
                    try:
                        self.conn.close()
                    except Exception as e:
                        self._log(f"[MAIN] Error closing connection: {e}", "warn")
                self.conn = None
                self.stop_status_listener()
                with self.lock:
                    self.state["mission_running"] = False
                    self.state["armed"] = False
                    self.state["conn_active"] = False
            self._push_state()
