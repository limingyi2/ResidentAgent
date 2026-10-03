@echo off
chcp 65001 >nul
title Vigil Autostart
set PYTHONIOENCODING=utf-8

rem ASCII only in this file. cmd reads .bat as GBK; UTF-8 Chinese breaks
rem line boundaries and can kill the commands below it.

set PYW="F:\zhixia\venv\Scripts\pythonw.exe"

rem 1) start the pet first (her brain lives there, other entries forward to it)
start "Vigil Pet" %PYW% "F:\zhixia\pet\pet.py"

rem 2) give it time to load memory + life engine (silent wait, no window)
ping -n 36 127.0.0.1 >nul

rem 3) then bring up the cloud-brain server (server.py)
start "Vigil Brain" %PYW% "F:\zhixia\brain\server.py"

rem autostart itself exits now; nothing left to show.
