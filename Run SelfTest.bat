@echo off
rem Self-test: keeps the window open so you can read the result.
cd /d "%~dp0"
chcp 65001 >nul 2>nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
echo Running 61 self-test cases, please wait (a window flashes for the GUI ones)...
echo.
"SelfTest.exe" --selftest %*
echo.
echo ---------------------------------------------------------------
echo Exit code: %ERRORLEVEL%   (0 = all passed)
pause
