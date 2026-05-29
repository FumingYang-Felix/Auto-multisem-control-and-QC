@echo off
REM One-click environment setup for the MultiSEM GUI (Windows).
REM Double-click this, or run from a terminal. Installs deps into the Python
REM on PATH. Pass --venv to use an isolated .venv instead.
python "%~dp0setup_env.py" %*
echo.
pause
