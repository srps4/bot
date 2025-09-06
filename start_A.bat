@e@echo off
cd /d C:\deadzone\mt5_trade_copier-ruman
set DOTENV_FILE=C:\deadzone\mt5_trade_copier-ruman\env\A.env
set PYTHONPATH=C:\deadzone\mt5_trade_copier-ruman\src
set PYTHONUNBUFFERED=1

py -3 -u .\src\watcher\master_watcher.py
pause

