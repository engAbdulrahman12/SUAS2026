#!/usr/bin/env python3
"""
SUAS 2026 — Main mission script.

Flow:
  Phase 1 (GUI):
    • Mission tab  — load or type lap waypoints.
    • Search tab   — enter two rectangle corners (or leave blank to skip).
    • Choose URI, click Start → mission.waypoints saved.
    • Verify in Mission Planner, press Enter.

  Phase 2 (drone):
    • Connect, read GPS → resolve HOME.
    • Upload mission, arm, GUIDED takeoff, fly laps.
    • If search corners provided: fly corner-1 → corner-2, then RTL.

COM PORT NOTE:
    pymavlink owns the serial port exclusively.
    Option A: disconnect Mission Planner before running.
    Option B: MP → Ctrl+F → Mavlink → Output1 → UDP 14550,
              then set URI = udp:0.0.0.0:14550 in GUI.
"""

import sys
import config
from connection import connect, wait_gps
from flight import arm, fly_to, rtl_and_land, set_fixed_home, set_mode, set_param, takeoff
from geo import distance_m
from gui import get_mission_params
from mission import WP_FILE, build_items, save_waypoints_file, upload_mission


def _resolve_home(gps_lat, gps_lon, gps_alt_msl):
    lat = config.HOME_LAT     if config.HOME_LAT     is not None else gps_lat
    lon = config.HOME_LON     if config.HOME_LON     is not None else gps_lon
    alt = config.HOME_ALT_MSL if config.HOME_ALT_MSL is not None else gps_alt_msl
    return lat, lon, alt


def _read_alt_msl(conn):
    msg = conn.recv_match(type="GLOBAL_POSITION_INT", blocking=True, timeout=5)
    return (msg.alt / 1000.0) if msg else 0.0


def main():
    mode = "SIMULATION" if config.TEST_FLAG else "REAL DRONE"
    print(f"\n{'='*60}")
    print(f"  SUAS 2026  |  {mode}  |  Alt={config.MISSION_ALT} m")
    print(f"{'='*60}\n")

    # ── Phase 1: GUI ────────────────────────────────────────────
    params = get_mission_params()
    if not params.confirmed:
        print("Mission cancelled.")
        return

    # Phase-1 preview file (placeholder takeoff = first waypoint)
    preview_lat, preview_lon = params.waypoints[0][0], params.waypoints[0][1]
    items = build_items(preview_lat, preview_lon, params.waypoints, params.laps,
                        home_lat=preview_lat, home_lon=preview_lon)
    save_waypoints_file(items)
    print(f"\n✓ {WP_FILE} saved  |  URI: {params.uri}")
    if params.search_start:
        print(f"  Search: {params.search_start[:2]} → {params.search_end[:2]}")
    else:
        print("  Search: skipped")
    input("\n  Verify in Mission Planner, then press Enter to connect → ")

    # ── Phase 2: connect ────────────────────────────────────────
    conn = connect(uri=params.uri)
    take_lat, take_lon = wait_gps(conn, simulation=bool(config.TEST_FLAG))
    alt_msl = _read_alt_msl(conn)
    home_lat, home_lon, home_alt_msl = _resolve_home(take_lat, take_lon, alt_msl)
    print(f"[MAIN] Takeoff: {take_lat:.8f}, {take_lon:.8f}")
    print(f"[MAIN] HOME  : {home_lat:.8f}, {home_lon:.8f}"
          + (" (config)" if config.HOME_LAT else " (auto GPS)"))

    items = build_items(take_lat, take_lon, params.waypoints, params.laps,
                        home_lat=home_lat, home_lon=home_lon)
    save_waypoints_file(items)
    set_param(conn, "RTL_ALT", config.MISSION_ALT * 100)
    upload_mission(conn, items)
    print("\n✓ Mission uploaded.\n")

    # ── Phase 3: fly ────────────────────────────────────────────
    armed = False
    try:
        set_mode(conn, "GUIDED")
        arm(conn)
        armed = True
        takeoff(conn, config.MISSION_ALT)
        set_fixed_home(conn, home_lat, home_lon, home_alt_msl)

        # Lap waypoints
        for lap in range(1, params.laps + 1):
            print(f"\n[MAIN] ── Lap {lap}/{params.laps} ──")
            prev_alt = config.MISSION_ALT
            for i, wp in enumerate(params.waypoints, 1):
                lat, lon, alt = wp
                if abs(alt - prev_alt) > 0.5:
                    print(f"[MAIN] Alt adjust {prev_alt:.1f} → {alt:.1f} m")
                    if not fly_to(conn, lat, lon, alt):
                        raise TimeoutError(f"Alt-adjust timeout lap {lap} WP {i}")
                if not fly_to(conn, lat, lon, alt):
                    raise TimeoutError(f"Timeout lap {lap} WP {i}")
                prev_alt = alt

        # Search: straight line corner-1 → corner-2
        if params.search_start:
            s_lat, s_lon, s_alt = params.search_start
            e_lat, e_lon, e_alt = params.search_end
            print(f"\n[MAIN] ── Search ──")
            print(f"[MAIN] Corner 1: {s_lat:.7f}, {s_lon:.7f}  alt={s_alt:.1f} m")
            if not fly_to(conn, s_lat, s_lon, s_alt):
                raise TimeoutError("Timeout reaching search corner 1")
            print(f"[MAIN] Corner 2: {e_lat:.7f}, {e_lon:.7f}  alt={e_alt:.1f} m")
            if not fly_to(conn, e_lat, e_lon, e_alt):
                raise TimeoutError("Timeout reaching search corner 2")
            print("[MAIN] Search complete ✓")

        rtl_and_land(conn, home_lat, home_lon)
        print("\n[MAIN] Mission complete ✓")

    except KeyboardInterrupt:
        print("\n[MAIN] Interrupted — RTL")
        if armed:
            rtl_and_land(conn, home_lat, home_lon)
    except Exception as e:
        print(f"\n[MAIN] Error: {e}")
        if armed:
            print("[MAIN] Commanding RTL for safety.")
            try:
                rtl_and_land(conn, home_lat, home_lon)
            except Exception as rtl_err:
                print(f"[MAIN] RTL failed: {rtl_err}")
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(130)
