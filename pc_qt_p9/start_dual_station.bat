@echo off
cd /d "%~dp0"
python "%~dp0dual_station.py" %*
if errorlevel 1 pause
