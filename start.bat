@echo off
REM ATLAS ONE - doble clic para instalar todo y abrir la plataforma.
setlocal
cd /d "%~dp0"
title ATLAS ONE

set "PY="
for %%V in (3.13 3.12 3.11) do (
  if not defined PY (
    py -%%V -c "import sys" >nul 2>&1 && set "PY=py -%%V"
  )
)
if not defined PY (
  python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1 && set "PY=python"
)

if not defined PY (
  echo.
  echo  No se encontro Python 3.11 o superior.
  echo.
  where winget >nul 2>&1
  if errorlevel 1 goto manual
  choice /C SN /M " Instalar Python 3.12 automaticamente con winget"
  if errorlevel 2 goto manual
  winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
  echo.
  echo  Python instalado. Cierra esta ventana y vuelve a hacer doble clic en start.bat
  pause
  exit /b 0
)

%PY% run.py %*
if errorlevel 1 pause
exit /b %errorlevel%

:manual
echo  Descargalo de https://www.python.org/downloads/ y marca "Add python.exe to PATH".
echo  Luego vuelve a hacer doble clic en start.bat
start "" https://www.python.org/downloads/
pause
exit /b 1
