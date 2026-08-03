"""Flight commands: mode, arm, takeoff, fly_to, RTL."""
import math
import time
from pymavlink import mavutil
import config
from geo import distance_m


def set_mode(conn, mode: str, verify: bool = True, verify_timeout: float = 5.0) -> None:
    modes = conn.mode_mapping()
    if mode not in modes:
        raise RuntimeError(f"Mode '{mode}' not available on this vehicle: {list(modes)}")
    target_mode_id = modes[mode]
    conn.set_mode(target_mode_id)
    print(f"[FLIGHT] Mode → {mode}")

    if not verify:
        time.sleep(1)
        return

    # Fire-and-forget was the old behavior here -- if the FC rejects the
    # request (e.g. "Mode change to AUTO failed: init failed"), the vehicle
    # silently stays in its previous mode and nothing downstream would ever
    # know, until something else (like a mission-progress watchdog) times
    # out much later with a confusing unrelated-looking error. Checking the
    # actual HEARTBEAT surfaces a rejection immediately and clearly instead.
    #
    # Also watches STATUSTEXT specifically: ArduPilot broadcasts the real
    # reason as a STATUSTEXT ("Mode change to AUTO failed: init failed") --
    # filtering to HEARTBEAT only (the old version of this) silently
    # discards that message, leaving us with just a generic timeout and no
    # way to know why.
    deadline = time.time() + verify_timeout
    rejection_text = None
    while time.time() < deadline:
        msg = conn.recv_match(type=["HEARTBEAT", "STATUSTEXT"], blocking=True, timeout=1)
        if msg is None:
            continue
        if msg.get_type() == "HEARTBEAT" and msg.custom_mode == target_mode_id:
            return
        if msg.get_type() == "STATUSTEXT":
            text = (msg.text or "").rstrip("\x00")
            if "mode change" in text.lower() and mode.lower() in text.lower():
                rejection_text = text   # keep the most recent match, in case of retries

    if rejection_text:
        raise RuntimeError(f"Mode change to {mode} was rejected by the flight controller: "
                          f"\"{rejection_text}\"")
    raise RuntimeError(f"Mode change to {mode} did not take effect within "
                      f"{verify_timeout:.0f}s -- the flight controller likely rejected it "
                      f"(check its own STATUSTEXT / Mission Planner message for the exact reason).")


def set_param(conn, name: str, value: float) -> None:
    conn.mav.param_set_send(conn.target_system, conn.target_component,
                            name.encode("ascii"), float(value),
                            mavutil.mavlink.MAV_PARAM_TYPE_REAL32)
    print(f"[FLIGHT] Param {name} = {value}")


def set_mission_current(conn, seq: int, verify: bool = True, verify_timeout: float = 5.0) -> None:
    """Points the uploaded mission's 'current item' at a specific index,
    without changing mode -- combine with set_mode(conn, 'AUTO') to have
    the vehicle start flying from that exact item."""
    conn.mav.command_long_send(conn.target_system, conn.target_component,
                               mavutil.mavlink.MAV_CMD_DO_SET_MISSION_CURRENT,
                               0, seq, 0, 0, 0, 0, 0, 0)
    print(f"[FLIGHT] Mission current -> item {seq}")

    if not verify:
        return

    # Verify via COMMAND_ACK, not MISSION_CURRENT -- the latter is a
    # telemetry stream that may not broadcast promptly (or at all without
    # an explicit stream-rate request), whereas COMMAND_ACK is sent
    # immediately in direct response to this specific command.
    deadline = time.time() + verify_timeout
    while time.time() < deadline:
        msg = conn.recv_match(type="COMMAND_ACK", blocking=True, timeout=1)
        if msg is not None and msg.command == mavutil.mavlink.MAV_CMD_DO_SET_MISSION_CURRENT:
            if msg.result == mavutil.mavlink.MAV_RESULT_ACCEPTED:
                return
            raise RuntimeError(f"Setting mission current to item {seq} was rejected "
                              f"(MAV_RESULT={msg.result}).")
    raise RuntimeError(f"Setting mission current to item {seq} was not acknowledged "
                      f"within {verify_timeout:.0f}s.")


def set_speed(conn, speed_ms: float, speed_type: int = 1) -> None:
    """MAV_CMD_DO_CHANGE_SPEED -- changes GUIDED-mode flight speed in real
    time, no parameter/reboot cycle needed (unlike WPNAV_SPEED). This is
    what actually controls how fast fly_to() moves the vehicle.
    speed_type: 0=airspeed, 1=groundspeed (use groundspeed for a copter)."""
    print(f"[FLIGHT] Speed -> {speed_ms:.1f} m/s")
    conn.mav.command_long_send(conn.target_system, conn.target_component,
                               mavutil.mavlink.MAV_CMD_DO_CHANGE_SPEED,
                               0, speed_type, speed_ms, -1, 0, 0, 0, 0)


def set_servo(conn, channel: int, pwm: int) -> None:
    """Set a single servo/AUX output channel to a specific PWM value.

    Used as a simple broadcast signal to a downstream system (e.g. the
    companion Pi reading this channel to know its recording/processing
    state) — independent of any MAVLink message parsing on the receiving
    end. This is a one-off command send; safe to call from any thread
    without touching the shared recv_match() read loop.
    """
    print(f"[FLIGHT] Servo ch{channel} → {pwm} us")
    conn.mav.command_long_send(
        conn.target_system, conn.target_component,
        mavutil.mavlink.MAV_CMD_DO_SET_SERVO,
        0, channel, pwm, 0, 0, 0, 0, 0)


def set_fixed_home(conn, lat: float, lon: float, alt_msl: float) -> None:
    """Lock the RTL home to a fixed GPS point (not the takeoff position)."""
    print(f"[FLIGHT] HOME → {lat:.8f}, {lon:.8f}, {alt_msl:.1f} m MSL")
    conn.mav.command_long_send(conn.target_system, conn.target_component,
                               mavutil.mavlink.MAV_CMD_DO_SET_HOME,
                               0, 0, 0, 0, 0, lat, lon, alt_msl)
    deadline = time.time() + 5
    while time.time() < deadline:
        ack = conn.recv_match(type="COMMAND_ACK", blocking=True, timeout=0.5)
        if ack and ack.command == mavutil.mavlink.MAV_CMD_DO_SET_HOME:
            if ack.result not in (mavutil.mavlink.MAV_RESULT_ACCEPTED,
                                  mavutil.mavlink.MAV_RESULT_IN_PROGRESS):
                print(f"[FLIGHT] SET_HOME not accepted (result={ack.result}) — continuing")
                return
            print("[FLIGHT] Fixed HOME accepted ✓")
            return


def arm(conn) -> None:
    print("[FLIGHT] Arming...")
    conn.mav.command_long_send(conn.target_system, conn.target_component,
                               mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
                               0, 1, 0, 0, 0, 0, 0, 0)
    conn.motors_armed_wait()
    print("[FLIGHT] Armed ✓")


def takeoff(conn, alt: float = None) -> None:
    """Command GUIDED-mode takeoff and wait until the target altitude is reached."""
    alt = alt or config.MISSION_ALT
    print(f"[FLIGHT] Takeoff → {alt:.1f} m AGL")
    conn.mav.command_long_send(conn.target_system, conn.target_component,
                               mavutil.mavlink.MAV_CMD_NAV_TAKEOFF,
                               0, 0, 0, 0, 0, 0, 0, alt)
    deadline = time.time() + config.TAKEOFF_TIMEOUT
    while time.time() < deadline:
        msg = conn.recv_match(type="GLOBAL_POSITION_INT", blocking=True, timeout=1)
        if msg:
            rel = msg.relative_alt / 1000.0
            print(f"[FLIGHT] Alt {rel:.1f} / {alt:.1f} m   ", end="\r")
            if rel >= alt - 1.0:
                print(f"\n[FLIGHT] Airborne ✓")
                return
    raise TimeoutError("Takeoff timeout — check ArduPilot pre-arm checks.")


class FlightCancelled(Exception):
    """Raised by fly_to() when a cancel_event is set mid-flight -- lets
    callers distinguish 'deliberately cancelled' from a real timeout."""
    pass


def fly_to(conn, lat: float, lon: float, alt: float,
           radius: float = None, timeout: int = None,
           near_cb=None, near_threshold: float = 15.0,
           position_cb=None, cancel_event=None, mute_event=None) -> bool:
    """Send SET_POSITION_TARGET in GUIDED mode and wait for arrival.

    near_cb(dist_m): called once, the first time distance-to-target drops
        below near_threshold metres. Used for a "getting close" heads-up
        (e.g. "recording will stop soon") without any extra position reads
        beyond what this loop already does.
    position_cb(lat, lon): called on every position update this loop
        receives anyway. Used to piggyback other things that want "the
        current position, whenever we happen to have a fresh one" (e.g.
        recomputing pin distances) without opening a second reader on the
        same connection.
    cancel_event: a threading.Event checked every loop iteration (roughly
        once a second). If set, raises FlightCancelled immediately instead
    mute_event: a threading.Event checked every loop iteration. While set,
        this leg is fully paused: no new position-target is sent (so the
        website stops re-asserting its old target once a second, which
        would otherwise fight an operator's manual control in Mission
        Planner even while "muted"), and the leg's own timeout clock is
        held rather than continuing to run out during the pause. Resumes
        exactly where it left off once cleared -- the leg is not restarted.
    """
    radius  = radius  or config.WP_ACCEPT_RADIUS_M
    timeout = timeout or config.WAYPOINT_TIMEOUT
    print(f"[FLIGHT] → {lat:.7f}, {lon:.7f}  alt={alt:.1f} m")
    deadline, last_send = time.time() + timeout, 0
    near_fired = False
    was_muted = False
    while time.time() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            print("\n[FLIGHT] Cancelled mid-leg.")
            raise FlightCancelled(f"fly_to to {lat:.7f},{lon:.7f} cancelled")

        if mute_event is not None and mute_event.is_set():
            if not was_muted:
                print("\n[FLIGHT] Muted -- pausing, not sending further commands "
                     "until unmuted.")
                was_muted = True
            deadline += 0.5   # hold the timeout clock during the pause
            time.sleep(0.5)
            continue
        if was_muted:
            print("[FLIGHT] Unmuted -- resuming this leg.")
            was_muted = False
            last_send = 0   # force an immediate re-send on resume

        if time.time() - last_send >= 1.0:
            conn.mav.set_position_target_global_int_send(
                0, conn.target_system, conn.target_component,
                mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
                0b0000111111111000,
                int(lat * 1e7), int(lon * 1e7), alt,
                0, 0, 0, 0, 0, 0, 0, 0)
            last_send = time.time()
        msg = conn.recv_match(type="GLOBAL_POSITION_INT", blocking=True, timeout=1)
        if msg:
            cur_lat, cur_lon = msg.lat / 1e7, msg.lon / 1e7
            if position_cb:
                position_cb(cur_lat, cur_lon)
            d       = distance_m(cur_lat, cur_lon, lat, lon)
            alt_err = abs(msg.relative_alt/1000.0 - alt)
            print(f"[FLIGHT] dist={d:.1f} m  alt_err={alt_err:.1f} m   ", end="\r")
            if near_cb and not near_fired and d <= near_threshold:
                near_fired = True
                near_cb(d)
            if d <= radius and alt_err <= 1.5:
                print("\n[FLIGHT] Waypoint reached ✓")
                return True
    print("\n[FLIGHT] Waypoint timeout.")
    return False


def nudge_body(conn, forward_m: float, right_m: float, down_m: float = 0.0) -> None:
    """Relative move in GUIDED mode, in the drone's own body frame.

    Used for click-to-fly: after the search mapping pass, clicking a spot on
    the camera feed nudges the drone toward whatever was clicked. This is a
    single relative-offset command, not a closed-loop "go to this pixel"
    controller — click again to keep approaching.
    """
    print(f"[FLIGHT] Nudge → forward={forward_m:+.1f} m  right={right_m:+.1f} m")
    conn.mav.set_position_target_local_ned_send(
        0, conn.target_system, conn.target_component,
        mavutil.mavlink.MAV_FRAME_BODY_OFFSET_NED,
        0b0000111111111000,          # position only
        forward_m, right_m, down_m,  # x=forward, y=right, z=down
        0, 0, 0,                     # vx, vy, vz (ignored)
        0, 0, 0,                     # afx, afy, afz (ignored)
        0, 0)                        # yaw, yaw_rate (ignored)


def localize_pixel_click(conn, px: float, py: float, frame_w: int, frame_h: int):
    """Reads the drone's current GPS + yaw and turns a clicked pixel into
    an absolute GPS point. Returns (lat, lon, current_alt_agl) or None if
    GPS/attitude aren't available yet. Shared by both click-to-fly and
    click-to-pin — same math, different thing done with the result."""
    from geo import localize_click
    from connection import get_latest_position, get_latest_attitude

    pos = get_latest_position(conn)
    att = get_latest_attitude(conn)
    if pos is None or att is None:
        print("[FLIGHT] Click localization: missing GPS/attitude — ignoring click.")
        return None

    lat = pos.lat / 1e7
    lon = pos.lon / 1e7
    # get_latest_position() can non-deterministically return EITHER a
    # GPS_RAW_INT or GLOBAL_POSITION_INT (whichever arrived last) -- only
    # the latter has relative_alt. Ask for that one specifically rather
    # than assume, same fix as the search-line code needed.
    if pos.get_type() == "GLOBAL_POSITION_INT":
        current_alt = pos.relative_alt / 1000.0
    else:
        gpi = conn.recv_match(type="GLOBAL_POSITION_INT", blocking=True, timeout=1)
        current_alt = (gpi.relative_alt / 1000.0) if gpi is not None else config.MISSION_ALT
    yaw_deg = math.degrees(att.yaw)
    if yaw_deg < 0:
        yaw_deg += 360

    target_lat, target_lon = localize_click(lat, lon, yaw_deg, px, py, frame_w, frame_h,
                                            config.PIXELS_PER_METER_X, config.PIXELS_PER_METER_Y)
    return target_lat, target_lon, current_alt


def fly_to_clicked_point(conn, px: float, py: float, frame_w: int, frame_h: int,
                          alt: float = None) -> bool:
    """Click-to-fly via full localization — same approach as last year's script.

    Turns the clicked pixel into an absolute GPS point (localize_pixel_click)
    and flies straight there with fly_to(). Unlike nudge_body() (a relative
    body-frame offset), this computes one real target coordinate per click.

    Moves in the XY plane ONLY by default -- altitude stays at whatever
    the drone is currently flying at, it does NOT snap back to a fixed
    MISSION_ALT. Pass an explicit alt= if you actually want to change
    height as part of this click (the separate altitude field is the
    normal way to do that instead).

    Always uses full speed with yaw-face-waypoint disabled -- regardless
    of whatever phase came before this click. Without this, a click-to-fly
    after the search/mapping pass would silently inherit that pass's slow
    speed and yaw-facing behavior, since neither gets reset automatically
    once the pass ends.
    """
    set_param(conn, "WP_YAW_BEHAVIOR", config.WP_YAW_BEHAVIOR_LAPS)
    set_param(conn, "WP_SPD", config.LAP_SPEED_MS)
    set_speed(conn, config.LAP_SPEED_MS)

    result = localize_pixel_click(conn, px, py, frame_w, frame_h)
    if result is None:
        return False
    target_lat, target_lon, current_alt = result
    target_alt = alt if alt is not None else current_alt
    print(f"[FLIGHT] Click localized → {target_lat:.7f}, {target_lon:.7f}  "
         f"alt={target_alt:.1f}m ({'explicit' if alt is not None else 'unchanged'})")
    return fly_to(conn, target_lat, target_lon, target_alt)


def rtl_and_land(conn, home_lat=None, home_lon=None) -> None:
    """Switch to RTL mode and wait until the drone lands.
    home_lat/home_lon optional — used only for distance display.

    Forces full speed with yaw-face-waypoint disabled before RTL --
    WP_YAW_BEHAVIOR explicitly governs RTL yaw too (per its own ArduPilot
    description), so if the search/mapping pass left it at face-next-
    waypoint + 5 m/s, RTL would silently inherit that slow, yaw-facing
    behavior instead of returning home directly at full speed.
    """
    set_param(conn, "WP_YAW_BEHAVIOR", config.WP_YAW_BEHAVIOR_LAPS)
    set_param(conn, "WP_SPD", config.LAP_SPEED_MS)
    set_speed(conn, config.LAP_SPEED_MS)

    print("[FLIGHT] RTL → home")
    set_mode(conn, "RTL")
    deadline = time.time() + config.RTL_TIMEOUT
    while time.time() < deadline:
        msg = conn.recv_match(type="GLOBAL_POSITION_INT", blocking=True, timeout=1)
        if msg:
            alt = msg.relative_alt / 1000.0
            if home_lat is not None and home_lon is not None:
                d = distance_m(msg.lat/1e7, msg.lon/1e7, home_lat, home_lon)
                print(f"[FLIGHT] RTL dist={d:.1f} m  alt={alt:.1f} m   ", end="\r")
            else:
                print(f"[FLIGHT] RTL alt={alt:.1f} m   ", end="\r")
            if alt < 0.5:
                print("\n[FLIGHT] Landed ✓")
                return
    print("\n[FLIGHT] RTL timeout — check drone manually.")
