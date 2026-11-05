@echo off
title ARGOS EPP - Instalacion
cd /d "%~dp0"
net session >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Ejecuta este archivo como ADMINISTRADOR ^(clic derecho - Ejecutar como administrador^).
  pause & exit /b 1
)
echo === ARGOS EPP: instalacion en %CD% ===
where python >nul 2>&1 || (echo [ERROR] No encuentro Python. Instala Python 3.13 y marca "Add to PATH". & pause & exit /b 1)
if not exist .venv (
  echo Creando entorno virtual...
  python -m venv .venv || (echo [ERROR] No pude crear .venv & pause & exit /b 1)
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip --cache-dir .pip-cache
pip install -r requirements.txt --cache-dir .pip-cache || (echo [ERROR] Fallo pip. Revisa internet y vuelve a ejecutar. & pause & exit /b 1)
for %%D in (evidencias live config modelos) do if not exist %%D mkdir %%D
echo Abriendo puertos del firewall ^(panel 8000 y camara del celular 8443^)...
netsh advfirewall firewall delete rule name="ARGOS Panel" >nul 2>&1
netsh advfirewall firewall add rule name="ARGOS Panel" dir=in action=allow protocol=TCP localport=8000,8443 profile=any
echo.
echo === LISTO ===  Ahora ejecuta iniciar_todo.bat
pause
