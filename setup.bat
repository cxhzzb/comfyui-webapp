@echo off
rem ============================================================
rem  ComfyUI Webapp - one-click setup (Windows)
rem    1) install Python dependencies
rem    2) clone custom nodes + download models (~140 GB, resumable)
rem    3) verify
rem  Usage:  double-click this file
rem          setup.bat "D:\ComfyUI"
rem  NOTE: kept ASCII-only on purpose. Chinese messages are printed
rem        by the Python tools, which handle UTF-8 correctly.
rem ============================================================
setlocal
cd /d "%~dp0"

set "COMFY_ROOT=%~1"
if not "%COMFY_ROOT%"=="" goto have_root

if not exist "config.json" goto no_root
for /f "usebackq delims=" %%p in (`python tools\read_root.py`) do set "COMFY_ROOT=%%p"
if "%COMFY_ROOT%"=="" goto no_root
goto have_root

:no_root
echo.
echo [ERROR] ComfyUI install directory not found.
echo   Usage: setup.bat "D:\path\to\ComfyUI"
echo   Or copy config.example.json to config.json and fill in comfyui_root.
echo.
pause
exit /b 2

:have_root
echo.
echo === Step 1/3: install webapp Python dependencies ===
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo [ERROR] dependency install failed. Check your Python environment.
  pause
  exit /b 1
)

echo.
echo === Step 2/3: deploy custom nodes and models ===
echo ComfyUI dir: %COMFY_ROOT%
echo Models are large (~140 GB). Interrupted runs resume where they left off.
echo.
python tools\deploy.py --comfy-root "%COMFY_ROOT%"
if errorlevel 1 (
  echo.
  echo [WARN] some items could not be fetched automatically - see output above.
  echo        Fetch them manually and re-run, or continue to see what is missing.
  echo.
  pause
)

echo.
echo === Step 3/3: verify ===
python tools\deploy.py --comfy-root "%COMFY_ROOT%" --check

echo.
echo ============================================================
echo  Done. Next steps:
echo    1) if a custom node ships requirements.txt, pip install it in ComfyUI's Python env
echo    2) restart ComfyUI
echo    3) copy auth_config.example.json to auth_config.json and set your own credentials
echo    4) start this service: python server.py
echo ============================================================
pause
