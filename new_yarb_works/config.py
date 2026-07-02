# ============================================================
#  SUAS 2026 — Configuration
#
#  WORKFLOW:
#    1. Double-click start_mavproxy.bat  (connects to drone, proxies to UDP)
#    2. Connect Mission Planner → UDP → 127.0.0.1:14550
#    3. Run this Python script           → connects to UDP:14551
#
#  To switch modes, change TEST_FLAG only:
#    TEST_FLAG = 1  →  SITL  (tcp:127.0.0.1:5762)
#    TEST_FLAG = 0  →  Real drone via MAVProxy UDP
# ============================================================

TEST_FLAG = 1      # 1 = SITL,  0 = real drone via MAVProxy

# --- Serial port (MAVProxy uses this — NOT the Python script) ---
# Only needed in start_mavproxy.bat. Shown here for reference.
COM_PORT  = "COM6"
BAUD_RATE = 57600

# --- Connection URIs ---
SITL_URI  = "tcp:127.0.0.1:5762"
DRONE_URI = "udp:0.0.0.0:14552"   # MAVProxy forwards drone data here

def default_uri():
    return SITL_URI if TEST_FLAG else DRONE_URI

# ----- HOME / RTL point ----------------------------------------
HOME_LAT     = None
HOME_LON     = None
HOME_ALT_MSL = None

# ----- Altitudes (AGL metres) ----------------------------------
MISSION_ALT  = 5

# ----- Mission behaviour ---------------------------------------
DEFAULT_LAPS             = 1
WP_ACCEPT_RADIUS_M       = 3.0
MAX_DISTANCE_FROM_HOME_M = 500.0

# ----- GPS readiness -------------------------------------------
MIN_GPS_FIX_TYPE = 0
MIN_SATELLITES   = 0

# ----- Timeouts (seconds) --------------------------------------
HEARTBEAT_TIMEOUT = 10
GPS_TIMEOUT       = 120
TAKEOFF_TIMEOUT   = 60
WAYPOINT_TIMEOUT  = 120
RTL_TIMEOUT       = 180
