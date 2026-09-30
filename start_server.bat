@echo off
chcp 65001 >nul
cd /d %~dp0
REM ===== ตั้งค่า (แก้ได้) =====
set RM_DELAY_DATA=F:\RM_Delay_Data
set RM_DELAY_PORT=8443
set RM_DELAY_ADMIN_PIN=0000
REM ============================
python server.py
pause
