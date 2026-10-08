@echo off
setlocal
cd /d "%~dp0"
python app.py
if errorlevel 1 (
  echo.
  echo PS3ToPC non e' stato avviato correttamente.
  echo Controlla che Python 3.10+ sia installato.
  pause
)
