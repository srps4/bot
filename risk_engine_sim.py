# src/risk_engine_sim.py
import os, math
from dataclasses import dataclass
from datetime import datetime, timezone

def _truthy(s): return str(s).strip().upper() in {"1","TRUE","YES","Y"}
def _f(name, d): return float(os.getenv(name, str(d)))
def _i(name, d): return int(os.getenv(name, str(d)))
def _s(name, d): return os.getenv(name, d)

# --- env-backed config (same as live) ---
INITIAL_BALANCE       = _f("INITIAL_BALANCE", 100000)
DAILY_LOSS_LIMIT      = _f("DAILY_LOSS_LIMIT", 5000)
OVERALL_EQUITY_FLOOR  = _f("OVERALL_EQUITY_FLOOR", 90000)
BASE_RISK_PCT               = _f("BASE_RISK_PCT", 0.008)
MDL_FRACTION_PER_TRADE      = _f("MDL_FRACTION_PER_TRADE", 0.20)
OVR_FRACTION_PER_TRADE      = _f("OVR_FRACTION_PER_TRADE", 0.20)
OPEN_RISK_FRACTION_OF_DAILY = _f("OPEN_RISK_FRACTION_OF_DAILY", 0.50)
MIN_LOT   = _f("MIN_LOT", 0.01)
MAX_LOT   = _f("MAX_LOT", 50.0)
LOT_STEP  = _f("LOT_STEP", 0.01)
SESSION_FILTER_ON = _truthy(_s("SESSION_FILTER_ON", "1"))
TRAIL_ATR_MULT    = _f("TRAIL_ATR_MULT", 1.5)
ATR_PERIOD        = _i("ATR_PERIOD", 14)
MIN_SYN_SL_POINTS = _i("MIN_SYN_SL_POINTS", 150)  # fallback SL = 150 points if ATR not ready
# --- No-ATR mode settings (fixed points) ---
SYN_SL_POINTS   = int(os.getenv("SYN_SL_POINTS",   "600"))  # follower SL distance in points if master has no SL
TRAIL_POINTS    = int(os.getenv("TRAIL_POINTS",    "400"))  # fixed trailing distance in points (optional)
TRAIL_ENABLE    = str(os.getenv("TRAIL_ENABLE",    "1")).upper() in {"1","TRUE","YES"}

# symbol assumptions for sim (no MT5)
VPP_PER_LOT_PER_POINT = _f("VPP_PER_LOT_PER_POINT", 1.0)
POINT_SIZE            = _f("POINT_SIZE", 0.01)
DIGITS                = _i("DIGITS", 2)
STOPS_LEVEL_POINTS    = _i("STOPS_LEVEL_POINTS", 0)

@dataclass
class DayState:
    date_key: str
    start_equity: float
    losses_today: float = 0.0

def round_lot(vol: float) -> float:
    steps = math.floor(vol / LOT_STEP + 1e-9)
    return max(MIN_LOT, min(MAX_LOT, steps * LOT_STEP))

def compute_allowed_risk_usd(equity: float, day_state: DayState) -> float:
    used = max(0.0, day_state.start_equity - equity)
    rem_daily = max(0.0, DAILY_LOSS_LIMIT - used)
    rem_over = max(0.0, equity - OVERALL_EQUITY_FLOOR)
    base = BASE_RISK_PCT * INITIAL_BALANCE
    return max(0.0, min(base, MDL_FRACTION_PER_TRADE * rem_daily, OVR_FRACTION_PER_TRADE * rem_over))

def lots_for_risk(risk_usd: float, sl_points: float) -> float:
    if sl_points <= 0: return 0.0
    lots = risk_usd / (sl_points * VPP_PER_LOT_PER_POINT)
    return round_lot(lots)

def synthetic_sl_points(_: float = 0.0) -> int:
    # ignore ATR; just use fixed points with broker stop-level floor
    return max(SYN_SL_POINTS, STOPS_LEVEL_POINTS)

