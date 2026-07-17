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

# ----- Camera feed -------------------------------------------------
# CAMERA_MODE is independent of TEST_FLAG on purpose — you can be flying
# the REAL drone (TEST_FLAG=0) while the camera/RTSP link isn't wired up
# yet, and still want the webcam + no-AI feed. Flip this once the real
# camera is ready, regardless of SITL vs real drone.
#   "webcam" → local webcam, no AI (bench-safe default)
#   "rtsp"   → RTSP feed + AI detection (tents / mannequins)
CAMERA_MODE = "webcam"

WEBCAM_INDEX   = 0
RTSP_URL       = "rtsp://192.168.144.25:8554/main.264"   # TODO: set to your VTX/companion RTSP URL
AI_MODEL_PATH  = "best.pt"     # trained YOLOv11s weights from the Colab pipeline
AI_CONF_THRESH = 0.4
AI_INFER_EVERY_N = 3           # run the model every Nth frame (perf)

# ----- Click-to-fly (pixel -> real-world GPS localization) --------
# Same approach as last year's script: a click on the camera feed is
# converted into an absolute GPS point using the drone's current position
# and yaw, then the drone flies straight to it.
#
# PIXELS_PER_METER_X/Y are calibrated for a specific altitude + camera
# FOV + frame resolution — last year's values are kept as the default, but
# RECALIBRATE these if altitude, camera, or resolution changes this year.
PIXELS_PER_METER_X = 68.6
PIXELS_PER_METER_Y = 67.5

# Altitude step (metres) per key press during manual click-to-fly search.
# Press 'D' to descend, 'U' to climb, each press moves this many metres.
CLICK_ALT_STEP_M = 2.0

# NOTE: water-bottle / beacon release controls are intentionally NOT
# implemented yet — that's a later step per the current plan.