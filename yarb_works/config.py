# ============================================================
#  SUAS 2026 — Configuration
#  Change TEST_FLAG to switch between SITL and real drone.
#  Change COM_PORT for the telemetry radio port.
# ============================================================

TEST_FLAG = 1           # 1 = SITL/simulation,  0 = real drone

# Serial port for real drone telemetry radio.
# Can also be overridden at runtime via the GUI port selector.
COM_PORT  = "COM6"
BAUD_RATE = 57600       # typical SiK radio baud; 115200 for direct USB

# SITL connection (Mission Planner secondary TCP port)
SITL_URI  = "tcp:127.0.0.1:5762"

# Derived URI — used when GUI doesn't override
def default_uri():
    if TEST_FLAG:
        return SITL_URI
    return f"com:{COM_PORT}"      # pymavlink serial syntax

# ----- HOME / RTL point ----------------------------------------
# Set to None → the drone's GPS position at connection time is used
# automatically as HOME (works anywhere, no hardcoding needed).
# Set to a specific lat/lon if you want RTL to go to a fixed point
# that is DIFFERENT from the takeoff spot (e.g. a recovery zone).
HOME_LAT     = None   # e.g. 21.49641679
HOME_LON     = None   # e.g. 39.24517840
HOME_ALT_MSL = None   # e.g. 39.13  (metres MSL — leave None to auto-detect)

# ----- Altitudes (AGL metres) --------------------------------
MISSION_ALT  = 10.0

# ----- Mission behaviour -------------------------------------
DEFAULT_LAPS           = 1
WP_ACCEPT_RADIUS_M     = 3.0
MAX_DISTANCE_FROM_HOME_M = 500.0

# ----- Search ---------------------------------------------------
# Search area is now defined in the GUI (Search tab) as two corner
# coordinates.  The drone flies corner-1 → corner-2 in a straight
# line, then RTL.  Nothing to configure here anymore.
ENABLE_SEARCH = bool(TEST_FLAG)   # kept for backwards compat; GUI controls search

# ----- GPS readiness (real drone only) -----------------------
MIN_GPS_FIX_TYPE = 3
MIN_SATELLITES   = 8

# ----- Timeouts (seconds) ------------------------------------
HEARTBEAT_TIMEOUT = 10
GPS_TIMEOUT       = 60
TAKEOFF_TIMEOUT   = 60
WAYPOINT_TIMEOUT  = 120
RTL_TIMEOUT       = 180
