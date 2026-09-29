@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8

rem Started every 5 minutes by a scheduled task. Must exit on its own (no pause).
rem Do NOT redirect output here: cmd would hold the log file handle, and one
rem stuck run would make every later run fail with "file in use".
rem watchdog.py writes watchdog.log itself.

"F:\zhixia\venv\Scripts\pythonw.exe" "F:\zhixia\pet\watchdog.py"
