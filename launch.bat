@echo off
setlocal
cd /d "%~dp0"

if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" "%~dp0main.py" %*
    exit /b
)

python -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>&1
if not errorlevel 1 (
    python "%~dp0main.py" %*
    exit /b
)

py -3 -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>&1
if not errorlevel 1 (
    py -3 "%~dp0main.py" %*
    exit /b
)

echo Python 3.11 or later was not found.
echo Install Python, then follow the setup instructions in README.md.
pause
exit /b 1
