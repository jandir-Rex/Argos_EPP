@echo off
cd /d "%~dp0"
start "ARGOS Motor" cmd /k "%~dp0iniciar_motor.bat"
start "ARGOS Panel" cmd /k "%~dp0iniciar_panel.bat"
timeout /t 6 >nul
start http://localhost:8000
