@echo off
rem Start the webapp. Port is read from config.json (falls back to 8800).
setlocal
cd /d "%~dp0"

set "PORT=8800"
for /f "usebackq delims=" %%p in (`python -c "import json,pathlib;p=pathlib.Path('config.json');print(json.loads(p.read_text(encoding='utf-8')).get('port',8800) if p.exists() else 8800)" 2^>nul`) do set "PORT=%%p"

netstat -ano | findstr ":%PORT%" | findstr "LISTENING" >nul
if %errorlevel%==0 (
  echo Server is already running at http://127.0.0.1:%PORT%
  echo Opening browser...
  start http://127.0.0.1:%PORT%
  timeout /t 3 >nul
  exit /b 0
)

echo Starting ComfyUI Web App at http://127.0.0.1:%PORT% ...
echo (Make sure ComfyUI is running at http://127.0.0.1:8188)
python server.py
pause
