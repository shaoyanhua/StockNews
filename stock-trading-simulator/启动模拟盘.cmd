@echo off
cd /d "%~dp0"
where node >nul 2>nul
if errorlevel 1 (
  start "" "%~dp0dist\index.html"
  exit /b
)
node server.cjs --open
pause
