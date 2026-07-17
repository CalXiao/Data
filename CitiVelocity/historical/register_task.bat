@echo off
REM One-time: register the daily refresh in Windows Task Scheduler (weekdays 07:00).
REM Run this once (double-click or from cmd). No admin needed for a per-user task.
schtasks /Create /TN "VelocityRatesDaily" /TR "\"%~dp0run_daily.bat\"" ^
  /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 07:00 /F
if %ERRORLEVEL%==0 (
  echo.
  echo Registered "VelocityRatesDaily" : weekdays 07:00 local.
  echo Edit time/days in Task Scheduler GUI if you like, or re-run with a different /ST.
) else (
  echo.
  echo Registration failed ^(exit %ERRORLEVEL%^). Try running cmd as your user and retry.
)
pause
