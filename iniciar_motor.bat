@echo off
title ARGOS EPP - Motor de IA
cd /d "%~dp0"
call .venv\Scripts\activate.bat
cd motor-ia
python motor_epp.py --no-view --ppe-imgsz 416 %*
pause
