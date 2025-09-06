import os, json, socket, time
from pathlib import Path
import MetaTrader5 as mt5
from _env import load_env_from_dotenv, get_path

load_env_from_dotenv()
MT5_PATH = get_path("MT5_PATH", r"C:\deadzone\mt5_trade_copier-ruman\mt5 terminal\MetaTrader 5_A.lnk")
BRIDGE_HOST = os.getenv("BRIDGE_HOST","127.0.0.1")
BRIDGE_PORT = int(os.getenv("BRIDGE_PORT","5555"))
POLL_SECONDS = max(0.01, int(os.getenv("POLL_MS","100"))/1000)

def ensure_mt5():
    # If .lnk, start it and attach without path; if .exe, init with path
    if MT5_PATH.lower().endswith(".lnk"):
        try:
            os.startfile(MT5_PATH)   # launches the shortcut
        except Exception:
            pass
        if not mt5.initialize():     # attach to running terminal
            raise RuntimeError(f"MT5 A init failed: {mt5.last_error()}")
    else:
        if not mt5.initialize(path=MT5_PATH):
            raise RuntimeError(f"MT5 A init failed: {mt5.last_error()}")

# ... (rest identical to your watcher; no path changes needed)
