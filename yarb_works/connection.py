"""MAVLink connection helpers.

Real-drone telemetry radio: pymavlink serial syntax  →  "com:COM6"
SITL TCP:                                            →  "tcp:127.0.0.1:5762"

NOTE on sharing the port with Mission Planner / ArduPilot GCS:
  pymavlink opens the serial port exclusively. If Mission Planner is
  also connected to the same telemetry radio you will get a
  "port busy" / access-denied error. Options:
    A) Disconnect Mission Planner before running this script (recommended
       for autonomous flights — GCS link not needed).
    B) Use Mission Planner's MAVLink output forwarding: connect MP first,
       then in MP go to  Ctrl+F → Mavlink → Output1 → UDP 14550,
       and change COM_PORT / SITL_URI in config.py to "udp:0.0.0.0:14550".
       Both tools then share the link via MP's proxy.
"""
import time
from pymavlink import mavutil
import config


def connect(uri: str | None = None, baud: int | None = None) -> mavutil.mavfile:
    """Open a MAVLink connection and wait for the first heartbeat.

    Args:
        uri:  Connection string. Defaults to config.default_uri().
        baud: Baud rate for serial connections. Defaults to config.BAUD_RATE.
    """
    uri  = uri  or config.default_uri()
    baud = baud or config.BAUD_RATE

    print(f"[CONN] Connecting → {uri}")

    is_serial = uri.lower().startswith(("com:", "/dev/"))
    kw = {"baud": baud} if is_serial else {}

    try:
        conn = mavutil.mavlink_connection(uri, autoreconnect=True,
                                          source_system=255, **kw)
    except Exception as e:
        raise ConnectionError(
            f"Cannot open {uri}: {e}\n"
            "  • Real drone: check COM port, baud rate, and that Mission Planner\n"
            "    is NOT using the same serial port at the same time.\n"
            "  • SITL:       make sure Mission Planner SITL is running and\n"
            "    TEST_FLAG=1 in config.py."
        ) from e

    hb = conn.wait_heartbeat(timeout=config.HEARTBEAT_TIMEOUT)
    if hb is None:
        raise TimeoutError(
            f"No heartbeat on {uri} after {config.HEARTBEAT_TIMEOUT}s.\n"
            "  • Real drone: is the drone powered and telemetry LED solid?\n"
            "  • SITL:       is Mission Planner SITL actually running?"
        )

    print(f"[CONN] Heartbeat OK — sys {conn.target_system} comp {conn.target_component}")
    conn.mav.request_data_stream_send(
        conn.target_system, conn.target_component,
        mavutil.mavlink.MAV_DATA_STREAM_ALL, config.DATA_RATE_HZ if hasattr(config, "DATA_RATE_HZ") else 10, 1)
    return conn


def wait_gps(conn, simulation: bool = False, timeout: int = None) -> tuple[float, float]:
    """Block until a valid GPS fix is available.

    Returns (lat, lon) in decimal degrees.
    In simulation mode any position is accepted immediately.
    """
    timeout = timeout or config.GPS_TIMEOUT
    deadline = time.time() + timeout
    last_gps = None

    print("[GPS] Waiting for position fix...")
    while time.time() < deadline:
        gps = conn.recv_match(type="GPS_RAW_INT", blocking=False)
        pos = conn.recv_match(type="GLOBAL_POSITION_INT", blocking=True, timeout=1)
        if gps:
            last_gps = gps
        if pos and simulation:
            print("[GPS] Simulation position ready ✓")
            return pos.lat / 1e7, pos.lon / 1e7
        if pos and last_gps:
            if (last_gps.fix_type  >= config.MIN_GPS_FIX_TYPE and
                    last_gps.satellites_visible >= config.MIN_SATELLITES):
                print(f"[GPS] Fix ready  fix={last_gps.fix_type}  sats={last_gps.satellites_visible} ✓")
                return pos.lat / 1e7, pos.lon / 1e7
            print(f"[GPS] fix={last_gps.fix_type}  sats={getattr(last_gps, 'satellites_visible', '?')}   ", end="\r")
    raise TimeoutError(f"GPS not ready after {timeout}s.")
