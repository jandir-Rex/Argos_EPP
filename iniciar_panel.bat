@echo off
title ARGOS EPP - Panel
cd /d "%~dp0"
call .venv\Scripts\activate.bat
cd panel
python servidor.py
pause
