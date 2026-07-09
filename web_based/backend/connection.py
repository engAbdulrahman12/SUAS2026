"""MAVLink connection helpers.

Connection flow (real drone):
  1. start_mavproxy.bat (or start_mavproxy_com3.bat) → opens COM3, forwards to:
       udp:127.0.0.1:14550  → Mission Planner (read/monitor)
       udp:127.0.0.1:14552  → this script's control connection
       udp:127.0.0.1:14553  → dedicated read-only Pi STATUSTEXT listener
  2. Mission Planner  → connects to udp:127.0.0.1:14550
  3. This script      → connects to udp:0.0.0.0:14552

SITL flow:
  1. Mission Planner SITL running
  2. This script connects to tcp:127.0.0.1:5762
     (SITL's 3rd spare port, tcp:127.0.0.1:5763, serves the same role as
     14553 above for the Pi status listener — see config.default_status_uri())
"""
import time
from pymavlink import mavutil
import config


def connect(uri: str | None = None, baud: int | None = None) -> mavutil.mavfile:
    uri  = uri  or config.default_uri()
    baud = baud or config.BAUD_RATE

    print(f"[CONN] Connecting → {uri}")

    is_serial = uri.upper().startswith("COM") or uri.startswith("/dev/")
    kw = {"baud": baud} if is_serial else {}

    try:
        conn = mavutil.mavlink_connection(uri, autoreconnect=True,
                                          source_system=254, **kw)
    except Exception as e:
        raise ConnectionError(
            f"Cannot open {uri}: {e}\n"
            "  • Real drone: make sure start_mavproxy.bat is running first.\n"
            "  • SITL:       make sure Mission Planner SITL is running and\n"
            "    TEST_FLAG=1 in config.py."
        ) from e

    hb = conn.wait_heartbeat(timeout=config.HEARTBEAT_TIMEOUT)
    if hb is None:
        raise TimeoutError(
            f"No heartbeat on {uri} after {config.HEARTBEAT_TIMEOUT}s.\n"
            "  • Real drone: is start_mavproxy.bat running? Is drone powered?\n"
            "  • SITL:       is Mission Planner SITL actually running?"
        )

    print(f"[CONN] Heartbeat OK — sys {conn.target_system} comp {conn.target_component}")
    conn.mav.request_data_stream_send(
        conn.target_system, conn.target_component,
        mavutil.mavlink.MAV_DATA_STREAM_ALL, 10, 1)
    return conn


def wait_gps(conn, simulation: bool = False, timeout: int = None) -> tuple[float, float]:
    """Block until a valid GPS fix is available.

    Waits for GLOBAL_POSITION_INT with a non-zero position.
    Uses GPS_RAW_INT for fix/satellite quality checks when available.
    Position 0,0 is always rejected — it means no fix yet.
    """
    timeout  = timeout or config.GPS_TIMEOUT
    deadline = time.time() + timeout
    last_gps = None   # GPS_RAW_INT
    last_pos = None   # GLOBAL_POSITION_INT

    print("[GPS] Waiting for position fix...")
    while time.time() < deadline:
        # Drain all available messages
        for _ in range(20):
            msg = conn.recv_match(
                type=["GPS_RAW_INT", "GLOBAL_POSITION_INT"],
                blocking=False)
            if msg is None:
                break
            t = msg.get_type()
            if t == "GPS_RAW_INT":
                last_gps = msg
            elif t == "GLOBAL_POSITION_INT":
                last_pos = msg

        # If nothing yet, block briefly
        if last_pos is None and last_gps is None:
            msg = conn.recv_match(
                type=["GPS_RAW_INT", "GLOBAL_POSITION_INT"],
                blocking=True, timeout=1)
            if msg:
                t = msg.get_type()
                if t == "GPS_RAW_INT":
                    last_gps = msg
                elif t == "GLOBAL_POSITION_INT":
                    last_pos = msg

        # SITL: accept any position immediately
        if last_pos and simulation:
            print("[GPS] Simulation position ready ✓")
            return last_pos.lat / 1e7, last_pos.lon / 1e7

        if last_pos:
            lat = last_pos.lat / 1e7
            lon = last_pos.lon / 1e7
            has_pos = abs(lat) > 0.001 or abs(lon) > 0.001

            if last_gps:
                fix  = last_gps.fix_type
                sats = getattr(last_gps, "satellites_visible", 0)
                print(f"[GPS] fix={fix}  sats={sats}  pos={lat:.5f},{lon:.5f}  "
                      f"need fix>={config.MIN_GPS_FIX_TYPE} sats>={config.MIN_SATELLITES}   ",
                      end="\r")
                # Accept only when position is real AND thresholds met
                if has_pos and fix >= config.MIN_GPS_FIX_TYPE and sats >= config.MIN_SATELLITES:
                    print(f"\n[GPS] Fix ready  fix={fix}  sats={sats} ✓")
                    return lat, lon
            else:
                if not has_pos:
                    print("[GPS] pos=0,0 — waiting for real GPS fix...   ", end="\r")
                elif config.MIN_GPS_FIX_TYPE == 0 and config.MIN_SATELLITES == 0:
                    print(f"\n[GPS] Position ready ({lat:.6f}, {lon:.6f}) ✓")
                    return lat, lon
                else:
                    print("[GPS] Have position, waiting for GPS_RAW_INT...   ", end="\r")
        else:
            print("[GPS] No GLOBAL_POSITION_INT yet...   ", end="\r")

        time.sleep(0.1)

    raise TimeoutError(
        f"GPS not ready after {timeout}s.\n"
        f"  GLOBAL_POSITION_INT: {'received, pos=' + str(round(last_pos.lat/1e7,5)) + ',' + str(round(last_pos.lon/1e7,5)) if last_pos else 'NOT received'}\n"
        f"  GPS_RAW_INT: {'fix=' + str(last_gps.fix_type) + ' sats=' + str(getattr(last_gps,'satellites_visible',0)) if last_gps else 'NOT received'}\n"
        f"  Thresholds: MIN_GPS_FIX_TYPE={config.MIN_GPS_FIX_TYPE}  MIN_SATELLITES={config.MIN_SATELLITES}\n"
        "  → Take the drone outside and wait for GPS lock before running."
    )


def get_latest_position(conn, timeout: float = 0.5):
    """Drain incoming position messages and return the most recent one.
    Used for click-to-fly localization — same approach as last year's
    get_latest_position(): non-blocking drain for `timeout` seconds so we
    don't act on a stale GPS fix.
    """
    deadline = time.time() + timeout
    msg = None
    while time.time() < deadline:
        m = conn.recv_match(type=["GPS_RAW_INT", "GLOBAL_POSITION_INT"], blocking=False)
        if m:
            msg = m
    return msg


def get_latest_attitude(conn, timeout: float = 0.5):
    """Drain incoming ATTITUDE messages and return the most recent one.
    Same approach as last year's get_latest_attitude()."""
    deadline = time.time() + timeout
    msg = None
    while time.time() < deadline:
        m = conn.recv_match(type="ATTITUDE", blocking=False)
        if m:
            msg = m
    return msg
