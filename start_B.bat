@echo off
cd /d C:\deadzone\mt5_trade_copier-ruman
set DOTENV_FILE=C:\deadzone\mt5_trade_copier-ruman\env\B.env
set PYTHONPATH=C:\deadzone\mt5_trade_copier-ruman\src
set PYTHONUNBUFFERED=1

rem show logs live (no file lock issues)
py -3 -u .\src\follower_executor.py
pause
