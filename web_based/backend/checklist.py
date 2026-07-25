"""
checklist.py

Pre-flight safety checklist. Each check is a plain function with the
signature (telemetry: dict, cfg: dict) -> CheckResult -- to add a new
check, write a function matching that signature and add it to the
CHECKS list at the bottom. Nothing else needs to change; this list IS
the "modular" mechanism here (no dynamic plugin discovery -- that's more
machinery than this actually needs, and would cost more in complexity
than it returns in value at this scale).

Telemetry is populated by controller.py's status listener from real
MAVLink messages (HEARTBEAT, SYS_STATUS, GPS_RAW_INT, BATTERY_STATUS,
EKF_STATUS_REPORT, STATUSTEXT, PARAM_VALUE) -- see _on_telemetry_message()
there. This module has no MAVLink dependency at all, just reads a dict.
"""

import time
from dataclasses import dataclass, field


@dataclass
class CheckResult:
    name: str
    category: str
    status: str                # "pass" | "fail" | "warn" | "waiting"
    current_value: str = ""
    required_value: str = ""
    reason: str = ""
    recommendation: str = ""
    can_override: bool = True
    overridden: bool = False


def _fresh(telemetry: dict, key: str, max_age: float = 3.0) -> bool:
    """True if telemetry[key] was updated within the last max_age seconds."""
    ts = telemetry.get(key + "_ts")
    return ts is not None and (time.time() - ts) <= max_age


# ============================================================
# Individual checks
# ============================================================

def check_heartbeat(t, cfg):
    if not _fresh(t, "heartbeat"):
        return CheckResult("Heartbeat", "Connection", "waiting",
                          recommendation="Waiting for vehicle telemetry.")
    return CheckResult("Heartbeat", "Connection", "pass", "receiving", "receiving")


def check_ekf(t, cfg):
    if not _fresh(t, "sys_status"):
        return CheckResult("EKF Healthy", "Flight Controller", "waiting")
    healthy = t.get("ekf_healthy", False)
    return CheckResult(
        "EKF Healthy", "Flight Controller", "pass" if healthy else "fail",
        "healthy" if healthy else "unhealthy", "healthy",
        reason="" if healthy else "EKF reports an unhealthy position/attitude estimate.",
        recommendation="" if healthy else "Do not fly. Check GPS, compass, and vibration levels.")


def check_ahrs(t, cfg):
    if not _fresh(t, "sys_status"):
        return CheckResult("AHRS Healthy", "Flight Controller", "waiting")
    healthy = t.get("ahrs_healthy", False)
    return CheckResult(
        "AHRS Healthy", "Flight Controller", "pass" if healthy else "fail",
        "healthy" if healthy else "unhealthy", "healthy",
        reason="" if healthy else "AHRS (attitude estimate) reports unhealthy.",
        recommendation="" if healthy else "Do not fly. Recalibrate or investigate sensor faults.")


def check_prearm(t, cfg):
    """ArduPilot re-sends "PreArm: ..." STATUSTEXT periodically while a
    check is actually failing, so treating this as "clear once no new
    PreArm message has arrived recently" (rather than latching the first
    one forever) reflects the vehicle's current real state."""
    if _fresh(t, "prearm", max_age=8.0):
        text = t.get("prearm_text", "")
        return CheckResult(
            "Pre-Arm Checks", "Flight Controller", "fail", text, "no PreArm failures",
            reason=text,
            recommendation="Resolve the reported PreArm failure on the flight controller itself.")
    return CheckResult("Pre-Arm Checks", "Flight Controller", "pass", "none", "none")


def check_gps_fix(t, cfg):
    if not _fresh(t, "gps"):
        return CheckResult("GPS 3D Fix", "GPS", "waiting")
    fix = t.get("gps_fix_type", 0)
    ok = fix >= 3
    return CheckResult(
        "GPS 3D Fix", "GPS", "pass" if ok else "fail", str(fix), ">=3 (3D fix)",
        reason="" if ok else "No 3D GPS fix.",
        recommendation="" if ok else "Move to open sky and wait for a 3D fix.")


def check_gps_satellites(t, cfg):
    if not _fresh(t, "gps"):
        return CheckResult("Satellite Count", "GPS", "waiting")
    sats = t.get("satellites_visible", 0)
    minimum = cfg.get("min_satellites", 10)
    ok = sats >= minimum
    return CheckResult(
        "Satellite Count", "GPS", "pass" if ok else "fail", str(sats), f">={minimum}",
        reason="" if ok else f"Only {sats} satellites visible.",
        recommendation="" if ok else "Wait for a better GPS fix before takeoff.")


def check_hdop(t, cfg):
    if not _fresh(t, "gps"):
        return CheckResult("HDOP", "GPS", "waiting")
    hdop = t.get("hdop", 99.0)
    maximum = cfg.get("max_hdop", 2.0)
    ok = hdop <= maximum
    return CheckResult(
        "HDOP", "GPS", "pass" if ok else "fail", f"{hdop:.2f}", f"<={maximum}",
        reason="" if ok else "GPS position dilution of precision is too high.",
        recommendation="" if ok else "Wait for better satellite geometry.")


def check_battery_voltage(t, cfg):
    if not _fresh(t, "battery"):
        return CheckResult("Battery Voltage", "Power", "waiting")
    v = t.get("battery_voltage", 0.0)
    minimum = cfg.get("min_battery_voltage", 22.0)
    ok = v >= minimum
    return CheckResult(
        "Battery Voltage", "Power", "pass" if ok else "fail",
        f"{v:.1f}V", f">={minimum}V",
        reason="" if ok else "Battery voltage is below the safe threshold.",
        recommendation="" if ok else "Charge or replace the battery before flight.")


def check_battery_remaining(t, cfg):
    if not _fresh(t, "battery"):
        return CheckResult("Battery Remaining", "Power", "waiting")
    pct = t.get("battery_remaining", -1)
    if pct < 0:
        return CheckResult("Battery Remaining", "Power", "warn", "unknown", ">=30%",
                          recommendation="This vehicle isn't reporting battery percentage.")
    minimum = cfg.get("min_battery_pct", 30)
    ok = pct >= minimum
    return CheckResult(
        "Battery Remaining", "Power", "pass" if ok else "fail",
        f"{pct}%", f">={minimum}%",
        reason="" if ok else "Battery remaining is below the safe threshold.",
        recommendation="" if ok else "Charge or replace the battery before flight.")


def check_battery_failsafe(t, cfg):
    if not _fresh(t, "sys_status"):
        return CheckResult("Battery Failsafe", "Power", "waiting")
    active = t.get("battery_failsafe", False)
    return CheckResult(
        "Battery Failsafe", "Power", "fail" if active else "pass",
        "active" if active else "clear", "clear",
        reason="Battery failsafe is currently active." if active else "",
        recommendation="Land and address the battery before continuing." if active else "")


def check_mode(t, cfg):
    if not _fresh(t, "heartbeat"):
        return CheckResult("Vehicle Mode", "General", "waiting")
    mode = t.get("mode", "UNKNOWN")
    acceptable = cfg.get("acceptable_modes", ["GUIDED", "LOITER", "STABILIZE"])
    ok = mode in acceptable
    return CheckResult(
        "Vehicle Mode", "General", "pass" if ok else "warn", mode, "/".join(acceptable),
        reason="" if ok else f"Mode '{mode}' is not in the expected list for mission start.",
        recommendation="" if ok else "Confirm this mode is intentional before proceeding.")


def check_not_armed(t, cfg):
    armed = t.get("armed", False)
    return CheckResult(
        "Not Already Armed", "General", "fail" if armed else "pass",
        "armed" if armed else "disarmed", "disarmed",
        reason="Vehicle is already armed." if armed else "",
        recommendation="Disarm before starting a new mission." if armed else "",
        can_override=False)   # too fundamental to override -- if it's already armed, stop


def check_arming_check_param(t, cfg):
    """This is the check that matters most given the actual crash this
    system exists because of: ARMING_CHECK=0 on the flight controller
    means the RC can arm the vehicle regardless of anything this website
    (or Mission Planner) reports. Deliberately NOT overridable."""
    val = t.get("arming_check_param")
    if val is None:
        return CheckResult("ARMING_CHECK Parameter", "Configuration", "waiting")
    ok = val != 0
    return CheckResult(
        "ARMING_CHECK Parameter", "Configuration", "pass" if ok else "fail",
        str(int(val)), "nonzero (enabled)",
        reason="" if ok else "ARMING_CHECK is disabled on the flight controller. The vehicle "
                             "can be armed -- including from the RC -- with NONE of "
                             "ArduPilot's own safety checks enforced.",
        recommendation="" if ok else "Set ARMING_CHECK on the flight controller itself (Mission "
                                     "Planner's Full Parameter List), not just here.",
        can_override=False)


def check_rc(t, cfg):
    if not _fresh(t, "rc"):
        return CheckResult("RC Connected", "RC", "warn", "not detected", "connected",
                          recommendation="No RC_CHANNELS data seen yet -- confirm the "
                                        "transmitter is on if you intend to use it as backup.")
    return CheckResult("RC Connected", "RC", "pass", "connected", "connected")


CHECKS = [
    check_heartbeat,
    check_ekf, check_ahrs, check_prearm,
    check_gps_fix, check_gps_satellites, check_hdop,
    check_battery_voltage, check_battery_remaining, check_battery_failsafe,
    check_mode, check_not_armed, check_arming_check_param,
    check_rc,
]


def run_checks(telemetry: dict, cfg: dict, overrides: dict) -> list:
    results = []
    for fn in CHECKS:
        r = fn(telemetry, cfg)
        if r.status == "fail" and r.can_override and overrides.get(r.name):
            r.overridden = True
            r.status = "warn"
        results.append(r)
    return results


def overall_ready(results: list) -> bool:
    """READY only if nothing is still failing (waiting/warn/pass are all
    fine -- warn covers both genuine warnings and overridden failures)."""
    return all(r.status != "fail" for r in results)


def has_unresolved_hard_fails(results: list) -> bool:
    """True if anything non-overridable is failing -- this can NEVER be
    bypassed by engineering mode, no matter what."""
    return any(r.status == "fail" and not r.can_override for r in results)
