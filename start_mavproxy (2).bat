@echo off
echo ============================================================
echo   SUAS 2026 - MAVProxy Launcher
echo   Drone on COM15 -> MP (14550) + Python (14552)
echo ============================================================
echo.
echo Starting MAVProxy... (leave this window open during flight)
echo.
py "C:\Users\kingf\AppData\Local\Programs\Python\Python313\Scripts\mavproxy.py" --master=COM15 --baudrate=57600 --out=udp:127.0.0.1:14550 --out=udp:127.0.0.1:14552
pause
