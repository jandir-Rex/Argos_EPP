@echo off
echo Cerrando motor y panel de ARGOS...
taskkill /FI "WINDOWTITLE eq ARGOS Motor*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq ARGOS Panel*" /T /F >nul 2>&1
echo Listo.
pause
