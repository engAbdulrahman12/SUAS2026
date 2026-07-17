"""Build the mission, save the .waypoints file, and upload to the FC.

Altitude-change logic (requirement 4):
  When two consecutive waypoints have different altitudes, an intermediate
  "altitude adjustment" waypoint is inserted at the *same lat/lon as the
  current waypoint* but at the *next* waypoint's altitude.  This forces
  ArduPilot to climb or descend first, then translate — which gives a
  predictable, safe altitude profile.

.waypoints file format (QGC WPL 110):
  item 0  : HOME  frame=0 (GLOBAL/MSL)   cmd=16
  item 1  : TAKEOFF  frame=3 (REL_ALT)  cmd=22  current=1
  item 2+ : waypoints  frame=3  cmd=16
  last    : RTL  frame=3  cmd=20
"""

import os
import time
from pymavlink import mavutil
import config
from geo import distance_m, offset_lat_lon

# Frame constants (file format AND MAVLink wire for non-INT variant)
_MSL = 0   # MAV_FRAME_GLOBAL          — HOME item
_REL = 3   # MAV_FRAME_GLOBAL_RELATIVE_ALT — everything else

WP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mission.waypoints")


# ── Helpers ───────────────────────────────────────────────────

def _make_wp(seq, lat, lon, alt, cmd=16, frame=_REL, current=0,
             p1=0, p2=None, p3=0, p4=0):
    return dict(seq=seq, frame=frame, cmd=cmd, current=current,
                p1=p1, p2=p2 if p2 is not None else config.WP_ACCEPT_RADIUS_M,
                p3=p3, p4=p4, lat=lat, lon=lon, alt=alt)


# ── Public API ────────────────────────────────────────────────

def build_items(takeoff_lat: float, takeoff_lon: float,
                waypoints: list[tuple[float, float, float]],
                laps: int,
                home_lat: float = None,
                home_lon: float = None) -> list[dict]:
    """Build the ordered list of mission item dicts.

    home_lat / home_lon: RTL return point.  Pass the drone's actual GPS
                         position (resolved in main.py).  If None, falls
                         back to takeoff position — so RTL goes back to
                         where the drone launched, wherever that is.
    waypoints: list of (lat, lon, alt_agl).
    """
    home_lat = home_lat if home_lat is not None else takeoff_lat
    home_lon = home_lon if home_lon is not None else takeoff_lon
    home_alt = config.HOME_ALT_MSL if config.HOME_ALT_MSL is not None else 0.0

    for i, wp in enumerate(waypoints, 1):
        lat, lon = wp[0], wp[1]
        d = distance_m(home_lat, home_lon, lat, lon)
        if d > config.MAX_DISTANCE_FROM_HOME_M:
            raise ValueError(f"WP {i} is {d:.0f} m from HOME "
                             f"(limit {config.MAX_DISTANCE_FROM_HOME_M} m)")

    items = []

    # Item 0 — HOME (absolute MSL)
    items.append(_make_wp(0, home_lat, home_lon, home_alt,
                          cmd=16, frame=_MSL, current=0, p2=0))

    # Item 1 — TAKEOFF
    items.append(_make_wp(1, takeoff_lat, takeoff_lon, config.MISSION_ALT,
                          cmd=22, frame=_REL, current=1, p2=0))

    # Lap waypoints (with per-waypoint altitude + alt-change inserts)
    for _ in range(laps):
        prev_alt = config.MISSION_ALT
        for wp in waypoints:
            lat, lon = wp[0], wp[1]
            alt = wp[2] if len(wp) > 2 else config.MISSION_ALT

            if abs(alt - prev_alt) > 0.5:
                # Insert altitude-adjustment point: same position, new altitude
                items.append(_make_wp(len(items), lat, lon, alt))
            items.append(_make_wp(len(items), lat, lon, alt))
            prev_alt = alt

    # RTL
    items.append(_make_wp(len(items), home_lat, home_lon,
                          config.MISSION_ALT, cmd=20, p2=0))

    # Renumber
    for i, it in enumerate(items):
        it["seq"] = i
    return items


def save_waypoints_file(items: list[dict]) -> None:
    """Write QGC WPL 110 file readable by Mission Planner."""
    with open(WP_FILE, "w", encoding="utf-8", newline="\n") as f:
        f.write("QGC WPL 110\n")
        for it in items:
            f.write(f"{it['seq']}\t{it['current']}\t{it['frame']}\t{it['cmd']}\t"
                    f"{it['p1']}\t{it['p2']}\t{it['p3']}\t{it['p4']}\t"
                    f"{it['lat']:.8f}\t{it['lon']:.8f}\t{it['alt']:.8f}\t1\n")
    print(f"[MISSION] Saved → {WP_FILE}")


def upload_mission(conn, items: list[dict]) -> None:
    """Upload mission to flight controller via MAVLink."""
    print(f"[MISSION] Uploading {len(items)} items...")

    try:
        conn.mav.mission_clear_all_send(conn.target_system, conn.target_component,
                                        mavutil.mavlink.MAV_MISSION_TYPE_MISSION)
    except TypeError:
        conn.mav.mission_clear_all_send(conn.target_system, conn.target_component)
    time.sleep(0.5)

    try:
        conn.mav.mission_count_send(conn.target_system, conn.target_component,
                                    len(items), mavutil.mavlink.MAV_MISSION_TYPE_MISSION)
    except TypeError:
        conn.mav.mission_count_send(conn.target_system, conn.target_component, len(items))

    sent, deadline = set(), time.time() + 40
    while time.time() < deadline:
        msg = conn.recv_match(type=["MISSION_REQUEST_INT", "MISSION_REQUEST", "MISSION_ACK"],
                              blocking=True, timeout=2)
        if not msg:
            continue
        if msg.get_type() == "MISSION_ACK":
            if getattr(msg, "type", None) == mavutil.mavlink.MAV_MISSION_ACCEPTED:
                print("[MISSION] Upload accepted ✓")
                return
            raise RuntimeError(f"Mission rejected (ACK type={getattr(msg, 'type', None)})")
        seq = int(msg.seq)
        if 0 <= seq < len(items):
            it = items[seq]
            try:
                conn.mav.mission_item_int_send(
                    conn.target_system, conn.target_component,
                    seq, it["frame"], it["cmd"],
                    it["current"], 1,
                    it["p1"], it["p2"], it["p3"], it["p4"],
                    int(it["lat"] * 1e7), int(it["lon"] * 1e7), it["alt"],
                    mavutil.mavlink.MAV_MISSION_TYPE_MISSION)
            except TypeError:
                conn.mav.mission_item_int_send(
                    conn.target_system, conn.target_component,
                    seq, it["frame"], it["cmd"],
                    it["current"], 1,
                    it["p1"], it["p2"], it["p3"], it["p4"],
                    int(it["lat"] * 1e7), int(it["lon"] * 1e7), it["alt"])
            sent.add(seq)
            print(f"[MISSION] Sent {seq + 1}/{len(items)}", end="\r")

    raise TimeoutError(f"Upload timeout — sent {len(sent)}/{len(items)}")
