@echo off
rem ASCII only. cmd reads .bat as GBK; UTF-8 Chinese can break line boundaries.
rem TEMPLATE ONLY: copy this file to run_pet.bat, then fill in the values below.
rem run_pet.bat is gitignored on purpose (it holds your server IP and key path).

rem ---- Fill these in for your own machine ----
set PROJECT_DIR=C:\path\to\your\zhixia
set PYTHONW=%PROJECT_DIR%\venv\Scripts\pythonw.exe
set CLOUD_HOST=your.server.ip
set CLOUD_USER=your_ssh_user
set SSH_KEY=C:\path\to\your\ssh\private_key

rem Her brain lives on the cloud server (config.json -> brain_remote
rem = http://127.0.0.1:18787). Without the SSH tunnel the pet shell starts,
rem but every reply fails -- it looks like "she won't open / won't talk".
rem So: bring the tunnel up first, then the shell.

rem 1) tunnel: local 18787 -> cloud 8788. Skip if already listening,
rem    so watchdog launches and double-clicks don't stack ssh processes.
netstat -ano | findstr "127.0.0.1:18787" | findstr "LISTENING" >nul
if errorlevel 1 (
  start "Vigil Tunnel" /min ssh -i "%SSH_KEY%" -N -L 18787:127.0.0.1:8788 -o BatchMode=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=4 -o StrictHostKeyChecking=no %CLOUD_USER%@%CLOUD_HOST%
  timeout /t 4 /nobreak >nul
)

rem 2) local pet shell (portrait, bubbles, chat window, activity watcher)
start "Vigil Pet" "%PYTHONW%" "%PROJECT_DIR%\pet\pet.py"
