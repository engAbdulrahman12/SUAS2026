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
