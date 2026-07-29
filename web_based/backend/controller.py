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
import checklist

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
    mission_alt: float = None   # takeoff/RTL/fallback altitude for THIS mission --
                                # None falls back to config.MISSION_ALT


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
        self.click_mode = "pin"   # "fly" or "pin" -- what a camera click does. Defaults to
                                  # pin: a mistaken click just drops a (removable) pin instead
                                  # of commanding the drone somewhere.
        self.fly_mode_unlocked = False   # separate from click_to_fly_enabled -- pin mode is
                                          # available as soon as connected, but fly mode stays
                                          # locked during an active mission until the mapping/
                                          # search pass is actually done (see _enable_click_to_fly).

        # object pins (click-to-pin) + route planning
        self.pins = []               # [{id, name, lat, lon, distance_m}, ...]
        self.mapping_exit_point = None   # (lat, lon) -- where the mapping pass ended
        self.mission_alt = config.MISSION_ALT   # overridden per-mission in start_mission()
        self.home_point = None           # (lat, lon) -- RTL destination for this mission

        # dedicated read-only connection for incoming Pi STATUSTEXT messages
        # (separate socket from self.conn — never races the mission thread's
        # recv_match() loop)
        self.status_conn = None
        self.status_listener_thread = None
        self.status_listener_running = False
        self.pi_link_uri = None   # None -> Pi commands go via self.conn (normal, one Pixhawk)
        self.skip_laps_event = threading.Event()
        self.guided_backup_event = threading.Event()

        # Pre-flight safety checklist
        self.telemetry = {}             # populated from real MAVLink messages, see _on_telemetry_message()
        self.check_overrides = {}       # {check_name: bool}
        self.engineering_mode = False   # False = Competition Mode (overrides disabled), matches spec
        self._last_checklist_push = 0.0

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
            "click_mode": "pin",
            "status_text": "Ready",
            "status_level": "info",
            "armed": False,
            "conn_active": False,
            "pi_link_uri": None,
            "engineering_mode": False,
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
    def _pi_conn(self):
        """The connection actually used to talk TO the Pi (commands, acks).

        Normally this is the same connection as the vehicle (self.conn) —
        there's only one Pixhawk, so the vehicle link IS the Pi link.

        If a dedicated Pi Link URI is configured, this is status_conn
        instead. That's for exactly one situation: testing with SITL for
        safe simulated flight WHILE a real Pixhawk sits on the bench,
        wired to the real Pi, purely to validate the actual production
        message-relay hardware chain (the part that's turned out to have
        real bugs before — routing/forwarding behavior, not website code).
        In that setup self.conn talks to SITL and has nothing to do with
        the Pi at all, so commands have to go out over the separate real
        connection instead.
        """
        if self.pi_link_uri and self.status_conn is not None:
            return self.status_conn
        return self.conn

    def set_pi_link_uri(self, uri: str):
        """Empty/None -> normal single-Pixhawk behavior (commands go out
        over the vehicle connection). Non-empty -> commands/acks go out
        over a separate connection to that URI instead — see _pi_conn()."""
        self.pi_link_uri = uri or None
        with self.lock:
            self.state["pi_link_uri"] = self.pi_link_uri
        self._push_state()
        if self.pi_link_uri:
            self._log(f"[PI-LINK] Using dedicated Pi link: {self.pi_link_uri} "
                     f"(commands go here, not the vehicle connection)", "info")
        else:
            self._log("[PI-LINK] Using the vehicle connection for Pi commands (normal mode).", "info")

        # The SENDING side (_pi_conn()) re-checks pi_link_uri fresh every
        # call, so it switches targets immediately. The RECEIVING side
        # (the status listener) is a persistent connection that only
        # opens once -- if it's already running on the OLD target, it'll
        # just sit there while commands go to the NEW target and nothing
        # answers. Restart it so both sides actually match.
        if self.status_listener_running:
            self._log("[PI-LINK] Restarting listener so receiving matches the new target...", "info")
            self.stop_status_listener()
            threading.Thread(target=self._restart_status_listener, daemon=True).start()

    def _restart_status_listener(self):
        time.sleep(0.5)   # let the old connection actually close first
        self.start_status_listener()

    def send_text_command(self, command: str, label: str):
        conn = self._pi_conn()
        if conn is None:
            self._log(f"[PI-CMD] Not connected — cannot send {label}.", "error")
            return
        from pymavlink import mavutil
        payload = command.encode("utf-8")[:50]
        try:
            conn.mav.statustext_send(mavutil.mavlink.MAV_SEVERITY_INFO, payload)
            self._log(f"[PI-CMD] {label} → sent '{command}'", "info")
        except Exception as e:
            self._log(f"[PI-CMD] {label} failed: {e}", "error")

    # ── Pi status listener (dedicated connection — read-only in normal
    # mode, but also used for writes when pi_link_uri makes it the ONLY
    # connection to the Pi, since then there's no self.conn to conflict with) ──
    _SEVERITY_TO_LEVEL = {0: "error", 1: "error", 2: "error", 3: "error",
                          4: "warn", 5: "warn", 6: "info", 7: "plain"}

    def start_status_listener(self, uri: str = None):
        if self.status_listener_running:
            return
        target_uri = uri or self.pi_link_uri or config.default_status_uri()

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

            # One-time request -- PARAM_VALUE isn't streamed automatically,
            # has to be explicitly asked for. Response arrives async and is
            # picked up in the PARAM_VALUE branch below.
            try:
                self.status_conn.mav.param_request_read_send(
                    self.status_conn.target_system, self.status_conn.target_component,
                    b"ARMING_CHECK", -1)
            except Exception:
                pass

            while self.status_listener_running:
                try:
                    msg = self.status_conn.recv_match(
                        type=["STATUSTEXT", "DATA_TRANSMISSION_HANDSHAKE", "ENCAPSULATED_DATA",
                             "HEARTBEAT", "SYS_STATUS", "GPS_RAW_INT", "BATTERY_STATUS",
                             "EKF_STATUS_REPORT", "PARAM_VALUE", "RC_CHANNELS"],
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
                        self._check_prearm_text(msg)
                        if not self.image_receiver.handle_message(msg):
                            self._handle_pi_statustext(msg)
                    elif mtype in ("DATA_TRANSMISSION_HANDSHAKE", "ENCAPSULATED_DATA"):
                        self.image_receiver.handle_message(msg)
                    elif mtype in ("HEARTBEAT", "SYS_STATUS", "GPS_RAW_INT", "BATTERY_STATUS",
                                  "EKF_STATUS_REPORT", "PARAM_VALUE", "RC_CHANNELS"):
                        self._on_telemetry_message(msg)
                except Exception as e:
                    print(f"[PI-LINK] Error handling {msg.get_type()}: {e}", file=sys.__stdout__)
                    import traceback
                    traceback.print_exc(file=sys.__stdout__)

                self._maybe_push_checklist()

            with self.lock:
                self.state["pi_link_active"] = False
            self._push_state()

        self.status_listener_thread = threading.Thread(target=_run, daemon=True)
        self.status_listener_thread.start()

    def _check_prearm_text(self, msg):
        text = (msg.text or "").rstrip("\x00")
        if "PreArm" in text or text.startswith("Arm:"):
            self.telemetry["prearm_text"] = text
            self.telemetry["prearm_ts"] = time.time()

    def _on_telemetry_message(self, msg):
        """Populates self.telemetry from real MAVLink messages, for the
        pre-flight checklist. See checklist.py for what actually reads
        these values -- this method has zero checklist logic itself, it
        just records the latest raw data."""
        now = time.time()
        mtype = msg.get_type()

        if mtype == "HEARTBEAT":
            if msg.autopilot == mavutil.mavlink.MAV_AUTOPILOT_INVALID:
                # This link carries HEARTBEATs from more than just the vehicle
                # (Mission Planner's own GCS heartbeat, MAVProxy's synthetic
                # one, etc). Those report MAV_AUTOPILOT_INVALID since they
                # aren't a flight controller and have no real flight mode --
                # processing them here is exactly what caused the mode to
                # flicker between the real value and garbage.
                return
            armed = bool(msg.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED)
            try:
                mode = mavutil.mode_string_v10(msg)
            except Exception:
                mode = str(msg.custom_mode)
            self.telemetry.update({"heartbeat_ts": now, "armed": armed, "mode": mode})

        elif mtype == "SYS_STATUS":
            present = msg.onboard_control_sensors_present
            health = msg.onboard_control_sensors_health
            ahrs_bit = getattr(mavutil.mavlink, "MAV_SYS_STATUS_AHRS", None)
            ahrs_healthy = True
            if ahrs_bit and (present & ahrs_bit):
                ahrs_healthy = bool(health & ahrs_bit)
            self.telemetry["sys_status_ts"] = now
            self.telemetry["ahrs_healthy"] = ahrs_healthy
            if msg.voltage_battery not in (0, 65535, -1):
                self.telemetry["battery_ts"] = now
                self.telemetry["battery_voltage"] = msg.voltage_battery / 1000.0
            if msg.battery_remaining >= 0:
                self.telemetry["battery_remaining"] = msg.battery_remaining

        elif mtype == "EKF_STATUS_REPORT":
            flags = msg.flags
            required = 0
            for name in ("EKF_ATTITUDE", "EKF_VELOCITY_HORIZ", "EKF_POS_HORIZ_ABS", "EKF_POS_VERT_ABS"):
                required |= getattr(mavutil.mavlink, name, 0)
            self.telemetry["sys_status_ts"] = now   # shares the EKF/AHRS freshness window
            self.telemetry["ekf_healthy"] = ((flags & required) == required) if required else True

        elif mtype == "GPS_RAW_INT":
            self.telemetry.update({
                "gps_ts": now,
                "gps_fix_type": msg.fix_type,
                "satellites_visible": msg.satellites_visible,
                "hdop": (msg.eph / 100.0) if msg.eph not in (65535, -1) else 99.0,
            })

        elif mtype == "BATTERY_STATUS":
            # Secondary source -- some vehicles report voltage here but
            # not in SYS_STATUS. Only overwrite if this looks valid.
            try:
                v = msg.voltages[0] / 1000.0
                if 0 < v < 100:
                    self.telemetry["battery_ts"] = now
                    self.telemetry["battery_voltage"] = v
            except Exception:
                pass
            if msg.battery_remaining >= 0:
                self.telemetry["battery_remaining"] = msg.battery_remaining

        elif mtype == "PARAM_VALUE":
            if msg.param_id.strip("\x00") == "ARMING_CHECK":
                self.telemetry["arming_check_param"] = msg.param_value

        elif mtype == "RC_CHANNELS":
            self.telemetry["rc_ts"] = now

    def _maybe_push_checklist(self):
        """Throttled to ~3-4x/sec -- matches the spec's "several times a
        second" without pushing a websocket message on every single
        MAVLink message (which can arrive much faster than that)."""
        now = time.time()
        if now - self._last_checklist_push < 0.25:
            return
        self._last_checklist_push = now
        self._emit({"type": "checklist_update", **self.get_checklist()})

    def get_checklist(self) -> dict:
        results = checklist.run_checks(self.telemetry, config.CHECKLIST_CONFIG, self.check_overrides)
        return {
            "results": [vars(r) for r in results],
            "ready": checklist.overall_ready(results),
            "hard_fail": checklist.has_unresolved_hard_fails(results),
            "engineering_mode": self.engineering_mode,
        }

    def set_override(self, name: str, enabled: bool):
        if not self.engineering_mode:
            self._log(f"[CHECKLIST] Override for '{name}' ignored -- "
                     f"Competition Mode has overrides disabled.", "warn")
            return
        self.check_overrides[name] = enabled
        self._log(f"[CHECKLIST] Override for '{name}' -> {'enabled' if enabled else 'disabled'}",
                 "warn" if enabled else "info")
        self._maybe_push_checklist_now()

    def set_engineering_mode(self, enabled: bool):
        self.engineering_mode = enabled
        if not enabled:
            self.check_overrides = {}   # leaving engineering mode clears all overrides
        with self.lock:
            self.state["engineering_mode"] = enabled
        self._log(f"[CHECKLIST] {'ENGINEERING MODE enabled -- overrides allowed' if enabled else 'Competition Mode restored -- overrides disabled and cleared'}",
                 "warn" if enabled else "info")
        self._push_state()
        self._maybe_push_checklist_now()

    def _maybe_push_checklist_now(self):
        self._last_checklist_push = 0.0
        self._maybe_push_checklist()

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
        conn = self._pi_conn()
        if conn is None:
            return
        prefix = IMGACK_PREFIX if ok else IMGFAIL_PREFIX
        text = f"{prefix}{image_id:08x}"
        try:
            conn.mav.statustext_send(mavutil.mavlink.MAV_SEVERITY_INFO, text.encode("utf-8")[:50])
        except Exception as e:
            self._log(f"[IMG] Failed to send {'ack' if ok else 'fail'} confirmation: {e}", "error")

    def _on_image_resend_request(self, image_id: int, missing_seqs: list):
        """Sends an IMGRESEND STATUSTEXT back to the Pi over whichever
        connection actually reaches it (_pi_conn()). A STATUSTEXT is
        capped at 50 bytes, so a long list of missing packet numbers gets
        split across multiple messages rather than silently truncated/corrupted."""
        if self._pi_conn() is None:
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
            self._pi_conn().mav.statustext_send(mavutil.mavlink.MAV_SEVERITY_INFO, text.encode("utf-8")[:50])
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
            self.fly_mode_unlocked = True   # bench testing, no mission running -- fine to allow
                                             # immediately. A real mission re-locks this at start.
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
        self.fly_mode_unlocked = True
        with self.lock:
            self.state["click_to_fly_enabled"] = True
        self._push_state()
        self._set_status("Mapping complete — click the camera feed to fly to that GPS point", "info")

    def set_click_mode(self, mode: str):
        if mode not in ("fly", "pin"):
            return
        self.click_mode = mode
        with self.lock:
            self.state["click_mode"] = mode
        self._push_state()
        self._log(f"[CLICK] Mode → {mode}", "info")

    def on_camera_click(self, px: float, py: float, w: int, h: int):
        if not self.click_to_fly_enabled or self.conn is None:
            return
        self._log(f"[CLICK] pixel=({px:.0f},{py:.0f}) of {w}x{h}  mode={self.click_mode}", "info")
        if self.click_mode == "pin":
            threading.Thread(target=self._add_pin_from_click, args=(px, py, w, h), daemon=True).start()
        else:
            if not self.fly_mode_unlocked:
                self._log("[CLICK] Click-to-fly is locked until the mapping/search line "
                         "finishes -- switch to Pin mode, or wait.", "warn")
                return
            from flight import fly_to_clicked_point
            threading.Thread(target=fly_to_clicked_point,
                             args=(self.conn, px, py, w, h), daemon=True).start()

    def _add_pin_from_click(self, px, py, w, h):
        from flight import localize_pixel_click
        result = localize_pixel_click(self.conn, px, py, w, h)
        if result is None:
            self._log("[PIN] Could not localize click — missing GPS/attitude.", "error")
            return
        lat, lon, _alt = result
        pin_id = (self.pins[-1]["id"] + 1) if self.pins else 1
        name = f"OBJ{pin_id}"
        pin = {"id": pin_id, "name": name, "lat": lat, "lon": lon, "distance_m": None}
        self.pins.append(pin)
        self._log(f"[PIN] Added {name} @ {lat:.7f}, {lon:.7f}", "ok")
        self._emit_pins()

    def clear_pins(self):
        self.pins = []
        self._emit_pins()
        self._log("[PIN] Cleared all pins.", "info")

    def _emit_pins(self):
        self._emit({"type": "pins_update", "pins": list(self.pins)})

    def _on_mapping_position(self, lat, lon):
        """Piggybacked on fly_to()'s existing position reads during the
        mapping pass — no separate reader thread, no concurrency risk."""
        self._recompute_pin_distances(lat, lon)

    def _recompute_pin_distances(self, cur_lat, cur_lon):
        if not self.pins:
            return
        from geo import distance_m
        for pin in self.pins:
            pin["distance_m"] = round(distance_m(cur_lat, cur_lon, pin["lat"], pin["lon"]), 1)
        self._emit_pins()

    def _observe_auto_laps(self, conn, auto_mode_id, after_laps_idx) -> bool:
        """Purely observational -- never touches mode, mission-current, or
        anything else. The operator is manually driving AUTO; this just
        watches MISSION_CURRENT to know when the lap sequence is done.
        Returns True if it reached the post-laps item while still in AUTO
        (laps genuinely complete), False if it should fall back to the
        GUIDED backup path (operator override, or AUTO dropped out
        unexpectedly) -- the caller handles the GUIDED fallback itself.
        """
        last_seq = None
        last_statustext = None
        while True:
            if self.skip_laps_event.is_set():
                self._log("[MAIN] Skip requested during AUTO -- taking over in GUIDED.", "warn")
                return False
            if self.guided_backup_event.is_set():
                self._log("[MAIN] Backup requested during AUTO -- taking over in GUIDED.", "warn")
                return False

            msg = conn.recv_match(type=["HEARTBEAT", "MISSION_CURRENT", "STATUSTEXT"],
                                  blocking=True, timeout=2)
            if msg is None:
                continue
            if msg.get_type() == "STATUSTEXT":
                # Capture whatever the FC broadcasts right around a mode
                # change -- failsafe messages ("EKF Failsafe", "Radio
                # Failsafe", "Battery Failsafe", etc.) show up this way.
                # Keeping the last one seen means if AUTO drops out on the
                # very next HEARTBEAT, we can report WHY instead of just
                # "left unexpectedly" with no explanation.
                text = (msg.text or "").rstrip("\x00")
                if text:
                    last_statustext = text
                continue
            if msg.get_type() == "HEARTBEAT":
                if msg.autopilot == mavutil.mavlink.MAV_AUTOPILOT_INVALID:
                    # This link carries HEARTBEATs from more than just the
                    # vehicle (Mission Planner's own GCS presence heartbeat,
                    # etc) -- those report MAV_AUTOPILOT_INVALID since they
                    # aren't a flight controller and have no real flight
                    # mode. Treating one of these as "the vehicle changed
                    # mode" is exactly what caused a false "left AUTO"
                    # detection here before.
                    continue
                if msg.custom_mode != auto_mode_id:
                    reason = f" -- last FC message: \"{last_statustext}\"" if last_statustext else \
                             " -- no STATUSTEXT seen to explain why"
                    self._log(f"[MAIN] Vehicle left AUTO unexpectedly{reason} -- "
                             f"taking over in GUIDED.", "warn")
                    return False
                continue
            # MISSION_CURRENT
            if msg.seq != last_seq:
                last_seq = msg.seq
                self._log(f"[MAIN] AUTO progress -- mission item {msg.seq}", "info")
            if msg.seq >= after_laps_idx:
                self._log("[MAIN] Reached the end of the lap sequence under AUTO.", "ok")
                return True

    def start_guided_backup(self):
        if not (self.mission_thread and self.mission_thread.is_alive()):
            self._log("[MAIN] No mission running to start.", "warn")
            return
        self.guided_backup_event.set()
        self._log("[MAIN] Guided backup requested.", "warn")

    def skip_to_search(self):
        if not (self.mission_thread and self.mission_thread.is_alive()):
            self._log("[MAIN] No mission running to skip.", "warn")
            return
        self.skip_laps_event.set()
        self._log("[MAIN] Skip requested -- ending laps early, heading to the search/mapping line.", "warn")

    def suggest_route(self):
        """Fixed start (mapping-pass exit point) and fixed end (home/RTL
        point), brute-force every ordering of the pinned points in
        between. For the handful of points this realistically has, exact
        brute force is instant — no heuristic/approximation needed."""
        from geo import distance_m
        import itertools

        if not self.pins:
            return {"order": [], "names": [], "total_distance_m": 0.0}

        start = self.mapping_exit_point
        end = self.home_point
        if start is None and self.pins:
            start = (self.pins[0]["lat"], self.pins[0]["lon"])
        if end is None and self.pins:
            end = (self.pins[-1]["lat"], self.pins[-1]["lon"])

        best_order, best_dist = None, None
        for perm in itertools.permutations(self.pins):
            total = distance_m(start[0], start[1], perm[0]["lat"], perm[0]["lon"])
            for a, b in zip(perm, perm[1:]):
                total += distance_m(a["lat"], a["lon"], b["lat"], b["lon"])
            total += distance_m(perm[-1]["lat"], perm[-1]["lon"], end[0], end[1])
            if best_dist is None or total < best_dist:
                best_dist = total
                best_order = perm

        return {
            "order": [p["id"] for p in best_order],
            "names": [p["name"] for p in best_order],
            "total_distance_m": round(best_dist, 1),
        }

    def fly_to_pin(self, pin_id: int):
        if self.conn is None:
            self._log("[PIN] Not connected.", "error")
            return
        pin = next((p for p in self.pins if p["id"] == pin_id), None)
        if pin is None:
            self._log(f"[PIN] No such pin: {pin_id}", "error")
            return
        from flight import fly_to
        self._log(f"[PIN] Flying to {pin['name']} @ {pin['lat']:.7f},{pin['lon']:.7f}", "info")
        threading.Thread(target=fly_to,
                         args=(self.conn, pin["lat"], pin["lon"], self.mission_alt),
                         daemon=True).start()

    def fly_route(self, order: list):
        if self.conn is None:
            self._log("[ROUTE] Not connected.", "error")
            return

        def _run():
            from flight import fly_to
            for pin_id in order:
                pin = next((p for p in self.pins if p["id"] == pin_id), None)
                if pin is None:
                    continue
                self._log(f"[ROUTE] → {pin['name']}", "info")
                fly_to(self.conn, pin["lat"], pin["lon"], self.mission_alt)
            self._log("[ROUTE] Route complete. Use RTL/Abort to return home.", "ok")

        threading.Thread(target=_run, daemon=True).start()

    def set_altitude(self, alt: float):
        if self.conn is None:
            self._log("[ALT] Not connected.", "error")
            return

        def _run():
            from connection import get_latest_position
            pos = get_latest_position(self.conn)
            if pos is None:
                self._log("[ALT] Could not read current position.", "error")
                return
            lat, lon = pos.lat / 1e7, pos.lon / 1e7
            self._log(f"[ALT] Changing altitude to {alt:.1f}m (position unchanged)", "info")
            from flight import fly_to
            fly_to(self.conn, lat, lon, alt)

        threading.Thread(target=_run, daemon=True).start()

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
        self.clear_pins()
        self.mapping_exit_point = None
        self.home_point = None
        self.skip_laps_event.clear()
        self.guided_backup_event.clear()
        self.fly_mode_unlocked = False
        self.click_mode = "pin"
        # This mission's altitude -- explicit override from the website if
        # given, otherwise the config fallback. Stored on self so pin-flying
        # (fly_to_pin/fly_route, called independently of _run()) uses the
        # SAME value as the rest of this mission, not a stale config constant.
        params.mission_alt = params.mission_alt if params.mission_alt is not None else config.MISSION_ALT
        self.mission_alt = params.mission_alt
        self._log(f"[MAIN] Mission altitude: {self.mission_alt:.1f} m AGL", "info")
        with self.lock:
            self.state["mission_running"] = True
            self.state["click_mode"] = "pin"
        self._push_state()
        self._set_status("Running…", "warn")
        self.mission_thread = threading.Thread(target=self._run, args=(params,), daemon=True)
        self.mission_thread.start()

    def _run(self, params: MissionParams):
        import config as cfg
        from connection import connect, wait_gps
        from flight import arm, fly_to, FlightCancelled, rtl_and_land, set_fixed_home, set_mode, set_param, set_speed, takeoff
        from mission import WP_FILE, build_items, save_waypoints_file

        armed = False
        manual_handoff = False
        try:
            # Phase 1 — save preview waypoints, wait for operator to verify
            p0 = params.waypoints[0]
            items, _, _ = build_items(p0[0], p0[1], params.waypoints, params.laps,
                                      home_lat=p0[0], home_lon=p0[1],
                                      search_corners=params.search_corners,
                                      mission_alt=params.mission_alt)
            save_waypoints_file(items)
            self._log(f"✓ {WP_FILE} saved — load in MP PLAN tab to verify.", "ok")
            self._log("After verifying waypoints in Mission Planner, click Continue →", "info")
            self.continue_ev.clear()
            with self.lock:
                self.state["awaiting_continue"] = True
            self._push_state()
            self.continue_ev.wait()

            # Phase 2 — connect. Reuse an existing connection from standalone
            # Connect if one is already active, instead of opening a SECOND
            # one to the same port -- most links (especially SITL's TCP
            # ports) only accept one client at a time, so a duplicate
            # connection attempt here just hangs waiting for a heartbeat
            # that will never arrive, since the port is already held by
            # the first connection.
            if self.conn is not None:
                self._log("[CONN] Reusing existing connection (already connected).", "info")
                conn = self.conn
            else:
                self._log(f"[CONN] Connecting → {params.uri}", "info")
                self._set_status("Connecting…", "info")
                conn = connect(uri=params.uri)
                self.conn = conn
            self.start_status_listener()
            with self.lock:
                self.state["conn_active"] = True
            self._push_state()
            take_lat, take_lon, alt_msl = wait_gps(conn, simulation=bool(cfg.TEST_FLAG))
            home_lat = cfg.HOME_LAT or take_lat
            home_lon = cfg.HOME_LON or take_lon
            home_alt = cfg.HOME_ALT_MSL or alt_msl
            self.home_point = (home_lat, home_lon)
            self._log(f"[MAIN] Takeoff: {take_lat:.8f}, {take_lon:.8f}", "info")
            self._log(f"[MAIN] HOME   : {home_lat:.8f}, {home_lon:.8f}", "info")

            items, first_lap_idx, after_laps_idx = build_items(
                take_lat, take_lon, params.waypoints, params.laps,
                home_lat=home_lat, home_lon=home_lon,
                search_corners=params.search_corners,
                mission_alt=params.mission_alt)
            save_waypoints_file(items)

            # Pre-flight safety checklist gate. Telemetry only starts
            # flowing once the status listener connects (just now), so
            # give it a moment to populate before evaluating -- this is
            # the REAL enforcement point (not just a UI nicety): a
            # non-overridable failure (e.g. ARMING_CHECK disabled on the
            # flight controller) stops here regardless of Engineering
            # Mode or any override toggle.
            self._log("[CHECKLIST] Running pre-flight checklist...", "info")
            time.sleep(2.0)
            check_data = self.get_checklist()
            failed_names = [r["name"] for r in check_data["results"] if r["status"] == "fail"]
            if check_data["hard_fail"]:
                raise RuntimeError(f"Pre-flight checklist: non-overridable failure(s), "
                                  f"cannot arm: {', '.join(failed_names)}")
            if not check_data["ready"]:
                raise RuntimeError(f"Pre-flight checklist not ready: {', '.join(failed_names)}")
            self._log("[CHECKLIST] Vehicle Ready for Flight", "ok")

            # Phase 3 — fly
            set_mode(conn, "GUIDED")
            arm(conn)
            armed = True
            with self.lock:
                self.state["armed"] = True
            self._push_state()
            self._set_status("Flying", "ok")
            takeoff(conn, params.mission_alt)
            set_fixed_home(conn, home_lat, home_lon, home_alt)

            # Mission is NOT uploaded automatically anymore, and the website
            # never switches to AUTO itself -- the operator uploads the
            # saved file via Mission Planner and switches modes manually,
            # at whatever moment they judge correct. This sidesteps the
            # exact problem repeated automatic attempts kept hitting: a
            # human watching Mission Planner's own UI sees the real
            # rejection reason instantly, instead of us guessing blind
            # through relayed log text.
            set_param(conn, "RTL_ALT", params.mission_alt * 100)
            self._log(f"[MISSION] {len(items)}-item mission file saved locally -- "
                     f"upload it via Mission Planner (remember to Set Current WP to "
                     f"your first lap waypoint) when ready.", "info")

            # Laps: never change yaw, fly at the vehicle's tuned max speed.
            # These are FC parameters, independent of who uploads the
            # mission or switches modes -- set automatically either way.
            self._log("[MAIN] Yaw behavior -> never change (laps)", "info")
            set_param(conn, "WP_YAW_BEHAVIOR", cfg.WP_YAW_BEHAVIOR_LAPS)
            set_param(conn, "WPNAV_SPEED", cfg.LAP_SPEED_MS * 100)
            set_speed(conn, cfg.LAP_SPEED_MS)

            # Wait for the operator to either (a) manually upload the
            # mission + switch to AUTO in Mission Planner, or (b) press
            # the "Start as Guided Mode" backup button on the website. No
            # timeout -- this is a deliberate human action, not something
            # to watch a clock on. Abort/RTL remains available the whole
            # time regardless (sent directly to the vehicle from its own
            # thread, independent of whatever this loop is doing).
            self._log("[MAIN] Waiting for manual AUTO switch (Mission Planner) or "
                     "the 'Start as Guided Mode' backup button...", "info")
            auto_mode_id = conn.mode_mapping().get("AUTO")
            auto_flew_laps = False
            while True:
                if self.guided_backup_event.is_set():
                    self._log("[MAIN] Backup requested -- flying laps in GUIDED.", "warn")
                    break
                msg = conn.recv_match(type="HEARTBEAT", blocking=True, timeout=2)
                if (msg is not None and msg.autopilot != mavutil.mavlink.MAV_AUTOPILOT_INVALID
                        and msg.custom_mode == auto_mode_id):
                    self._log("[MAIN] AUTO engaged manually -- monitoring progress "
                             "(not driving it).", "ok")
                    auto_flew_laps = self._observe_auto_laps(conn, auto_mode_id, after_laps_idx)
                    break

            if not auto_flew_laps:
                set_mode(conn, "GUIDED")
                try:
                    for lap in range(1, params.laps + 1):
                        self._log(f"[MAIN] — Lap {lap}/{params.laps} (GUIDED) —", "info")
                        prev = params.mission_alt
                        for i, wp in enumerate(params.waypoints, 1):
                            # Checked BETWEEN waypoints, not mid-flight --
                            # lets whichever leg is currently in progress
                            # finish reaching its target normally, and only
                            # refuses to START the next one, avoiding a
                            # sudden stop/direction change while still
                            # moving.
                            if self.skip_laps_event.is_set():
                                raise FlightCancelled("skip requested between waypoints")
                            lat, lon, alt = wp
                            if abs(alt - prev) > 0.5:
                                self._log(f"[MAIN] Alt {prev:.1f}->{alt:.1f} m", "info")
                                if not fly_to(conn, lat, lon, alt, radius=cfg.LAP_ACCEPT_RADIUS_M):
                                    raise TimeoutError(f"Alt-adjust timeout lap {lap} WP {i}")
                            if not fly_to(conn, lat, lon, alt, radius=cfg.LAP_ACCEPT_RADIUS_M):
                                raise TimeoutError(f"Timeout lap {lap} WP {i}")
                            prev = alt
                except FlightCancelled:
                    self._log("[MAIN] Skipping remaining laps as requested.", "warn")

            set_mode(conn, "GUIDED")
            self._log("[MAIN] Laps complete.", "ok")

            # Phase 4 — ask: home or search?
            choice = self._ask_post_lap_choice(search_available=bool(params.search_corners))

            if choice == "search" and params.search_corners:
                from mission import build_straight_line_path
                from geo import distance_m
                self._log("[SEARCH] Building straight-line pass along the longest side...", "info")
                corners_latlon = [(c[0], c[1]) for c in params.search_corners]
                search_alt = params.search_corners[0][2]
                path = build_straight_line_path(corners_latlon, alt=search_alt)
                self._log(f"[SEARCH] {len(path)}-point straight-line pass generated.", "info")

                # Read the ACTUAL current position -- not the last lap
                # waypoint's coordinates. Skip to Search Line (or AUTO
                # stopping somewhere mid-lap) can leave the drone somewhere
                # else entirely, so this is computed fresh at the moment
                # this choice is made, not assumed from the mission plan.
                # Specifically GLOBAL_POSITION_INT -- get_latest_position()
                # can non-deterministically return a GPS_RAW_INT instead,
                # which has no relative_alt field at all.
                pos_msg = conn.recv_match(type="GLOBAL_POSITION_INT", blocking=True, timeout=2)
                if pos_msg is not None:
                    cur_lat, cur_lon = pos_msg.lat / 1e7, pos_msg.lon / 1e7
                    cur_alt = pos_msg.relative_alt / 1000.0
                else:
                    cur_lat, cur_lon = params.waypoints[-1][0], params.waypoints[-1][1]
                    cur_alt = search_alt
                    self._log("[SEARCH] Could not read current position -- using last lap "
                             "waypoint as an estimate instead.", "warn")

                # Let whatever motion was previously in progress (a lap leg,
                # or one just cancelled by Skip) actually stop before
                # committing to a new direction. Issuing a brand-new target
                # immediately -- possibly the opposite way -- while the
                # vehicle is still actively moving toward the old one risks
                # a sudden direction change. Holding at the CURRENT position
                # and altitude first lets it decelerate and settle cleanly;
                # the altitude transition to the search pass's own altitude
                # happens naturally as part of the first real leg below.
                self._log("[SEARCH] Holding position to settle before starting the pass...", "info")
                fly_to(conn, cur_lat, cur_lon, cur_alt, timeout=15)

                # Fly to whichever end is actually closer to the drone's
                # CURRENT position (not a fixed geometric end), avoiding
                # flying past one end just to loop back to it.
                d_to_first = distance_m(cur_lat, cur_lon, path[0][0], path[0][1])
                d_to_last = distance_m(cur_lat, cur_lon, path[-1][0], path[-1][1])
                if d_to_last < d_to_first:
                    path = list(reversed(path))
                    self._log(f"[SEARCH] Reversed pass direction -- closer entry point "
                             f"({d_to_last:.0f}m vs {d_to_first:.0f}m) from current position.", "info")

                # Search/mapping pass: face the next waypoint, fly slower
                # for stable video, rather than the laps' never-change-yaw
                # + max-speed behavior.
                self._log("[SEARCH] Yaw behavior -> face next waypoint", "info")
                set_param(conn, "WP_YAW_BEHAVIOR", cfg.WP_YAW_BEHAVIOR_SEARCH)
                # DO_CHANGE_SPEED alone wasn't reliably taking effect for
                # GUIDED position-target navigation -- also set WPNAV_SPEED
                # directly (cm/s) as a second, more authoritative path.
                set_param(conn, "WPNAV_SPEED", cfg.SEARCH_SPEED_MS * 100)
                set_speed(conn, cfg.SEARCH_SPEED_MS)

                self._log("[SEARCH] Starting camera feed...", "info")
                threading.Thread(target=self.start_camera, daemon=True).start()

                self._log("[MAIN] ── Search / mapping ──", "info")
                for i, (lat, lon, alt) in enumerate(path, 1):
                    self._log(f"[SEARCH] Leg {i}/{len(path)}", "info")

                    if i == 1:
                        # Trigger on COMMENCEMENT of the entry leg, not arrival —
                        # the command's travel time then overlaps with the flight
                        # time to get there, instead of stacking as dead time
                        # after arrival.
                        self.send_text_command(config.CMD_RECORD_START,
                                              "Auto: Start Recording (mapping pass)")

                    near_cb = None
                    if i == len(path):
                        # Heads-up as we approach the exit point — a second,
                        # independent sanity check on top of having already
                        # visually verified these points in the pre-flight
                        # waypoints file.
                        near_cb = lambda d: self._log(
                            f"[SEARCH] Approaching pass exit (~{d:.0f}m) — recording will stop soon.",
                            "info")

                    if not fly_to(conn, lat, lon, alt, near_cb=near_cb,
                                  position_cb=self._on_mapping_position):
                        raise TimeoutError(f"Search leg {i} timeout")

                    if i == len(path):
                        # No further leg to hide this behind, so this one is
                        # arrival-based and may run a little past the true
                        # edge — acceptable per the "even it will be late" call.
                        self.send_text_command(config.CMD_RECORD_STOP,
                                              "Auto: Stop Recording (mapping pass)")

                self._log("[SEARCH] Mapping complete ✓", "ok")
                self.mapping_exit_point = (path[-1][0], path[-1][1])

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
