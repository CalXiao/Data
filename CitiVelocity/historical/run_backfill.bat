@echo off
REM ============================================================
REM  run_backfill.bat  -  ONE-TIME historical backfill of the full curated
REM  selection (now includes the extended 1y-forward chain 11y1y..20y1y).
REM  Needs the Citi entitlement + internet. Safe to re-run: it upserts
REM  (last write wins), so it won't duplicate existing data.
REM  Just double-click this file. A window opens, shows progress, and stays
REM  open at the end so you can read any messages. Takes a few minutes.
REM ============================================================
cd /d "%~dp0"
where py >nul 2>nul && (set "PY=py") || (set "PY=python")
if not exist logs mkdir logs
for /f "delims=" %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set STAMP=%%i

echo(
echo Backfilling full history for all selected tags (incl 11y1y..20y1y)...
echo This pulls from 2016 and may take a few minutes. Logging to logs\backfill_%STAMP%.log
echo(
%PY% rates_pipeline.py backfill 2>&1 | powershell -NoProfile -Command "$input | Tee-Object -FilePath 'logs\backfill_%STAMP%.log'"

echo(
echo ============================================================
echo Finished (exit code %ERRORLEVEL%). Scroll up or open logs\backfill_%STAMP%.log
echo to check for errors. You can close this window now.
echo ============================================================
pause
