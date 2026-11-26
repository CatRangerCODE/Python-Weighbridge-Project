@echo off
title Weighbridge
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 "%~dp0weighbridge.py" %*
) else (
    python "%~dp0weighbridge.py" %*
)
if errorlevel 1 pause
