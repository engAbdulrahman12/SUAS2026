"""GPS geometry helpers."""
import math

_R = 6_378_137.0  # Earth radius in metres

def distance_m(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dlon/2)**2
    return 2 * _R * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def offset_lat_lon(lat, lon, north_m, east_m):
    return (lat + math.degrees(north_m / _R),
            lon + math.degrees(east_m / (_R * math.cos(math.radians(lat)))))

def latlon_to_local(origin_lat, origin_lon, lat, lon):
    """Inverse of offset_lat_lon — flat-earth (north_m, east_m) relative to origin.
    Good enough for search-area-sized boxes (tens to hundreds of metres)."""
    north = math.radians(lat - origin_lat) * _R
    east  = math.radians(lon - origin_lon) * _R * math.cos(math.radians(origin_lat))
    return north, east


# ── Click-to-fly localization (pixel -> absolute GPS) ────────────────
# Same approach as last year's script: a clicked pixel is converted to a
# body-frame (right, forward) offset in metres, rotated into world-frame
# (east, north) using the drone's current yaw, then turned into an
# absolute lat/lon.

def pixel_offset_to_meters(px, py, frame_w, frame_h, ppm_x, ppm_y):
    """Clicked pixel -> (right_m, forward_m) offset from the image centre."""
    cx, cy = frame_w / 2.0, frame_h / 2.0
    right_m   = (px - cx) / ppm_x
    forward_m = (cy - py) / ppm_y
    return right_m, forward_m


def rotate_body_to_ne(right_m, forward_m, yaw_deg):
    """Rotate a body-frame (right, forward) offset into world-frame
    (east, north) using the drone's yaw heading — same rotation last
    year's script used."""
    yaw = math.radians(yaw_deg)
    east  = right_m * math.cos(yaw) + forward_m * math.sin(yaw)
    north = -right_m * math.sin(yaw) + forward_m * math.cos(yaw)
    return east, north


def localize_click(lat, lon, yaw_deg, px, py, frame_w, frame_h, ppm_x, ppm_y):
    """Turn a clicked pixel into an absolute (lat, lon), given the drone's
    current position and yaw — same idea as last year's localizee():
    pixel -> body-frame offset -> rotate by yaw -> world-frame offset ->
    absolute GPS point.
    """
    right_m, forward_m = pixel_offset_to_meters(px, py, frame_w, frame_h, ppm_x, ppm_y)
    east_m, north_m = rotate_body_to_ne(right_m, forward_m, yaw_deg)
    return offset_lat_lon(lat, lon, north_m, east_m)
