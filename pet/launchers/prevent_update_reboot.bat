@echo off
chcp 65001 >nul
title Stop Windows Update Auto-Reboot

rem ASCII only. Windows Update reboots the PC after patches, she goes offline.
rem 1) no auto reboot while a user is logged on
rem 2) active hours 08:00 - 23:00
rem Needs admin: right-click this file, "Run as administrator".

reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU" /v NoAutoRebootWithLoggedOnUsers /t REG_DWORD /d 1 /f
reg add "HKLM\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings" /v ActiveHoursStart /t REG_DWORD /d 8 /f
reg add "HKLM\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings" /v ActiveHoursEnd /t REG_DWORD /d 23 /f

echo.
echo Done.
echo If you saw "Access is denied" above, right-click this file
echo and choose "Run as administrator".
echo.
pause
