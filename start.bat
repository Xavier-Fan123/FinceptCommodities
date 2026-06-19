@echo off
REM Fincept Commodities - local dashboard launcher
REM Uses the FinceptTerminal venv Python (has yfinance/pandas/numpy/scipy).
REM To use a different Python, set PY below to its full path.

set "PY=%LOCALAPPDATA%\com.fincept.terminal\venv-numpy2\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo Starting Fincept Commodities local dashboard...
"%PY%" "%~dp0server.py" %1
pause
