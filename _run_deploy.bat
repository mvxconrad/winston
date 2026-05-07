@echo off
echo Running Winston deploy from WSL...
echo.
wsl bash -c "cd ~/projects/sysmonitor && chmod +x deploy.sh && ./deploy.sh"
echo.
echo ========================================
echo Deploy finished. Press any key to close.
echo ========================================
pause
