@echo off
REM Velocity rates daily refresh -- pulls last N business days, upserts, rebuilds
REM DuckDB views + analytics presets. Logs to logs\daily_YYYYMMDD.log.
cd /d "%~dp0"
if not exist logs mkdir logs
for /f "delims=" %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set TODAY=%%i
echo ===== %DATE% %TIME% : starting daily refresh ===== >> "logs\daily_%TODAY%.log"
python rates_pipeline.py daily >> "logs\daily_%TODAY%.log" 2>&1
echo ===== %DATE% %TIME% : finished (exit %ERRORLEVEL%) ===== >> "logs\daily_%TODAY%.log"
