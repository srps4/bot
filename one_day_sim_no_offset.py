"""
invert_all_trades_v2.py
- Opens an opposite "mirror" to your manual position with dynamic SL/TP and risk-based lot sizing
- Optional 'replace' mode: mirror first, then close your manual (reduces slippage)
- FTMO/CFD/FX/crypto safe: risk in account currency, ATR-based stops with broker stop-distance clamp
"""

import os, time, math
from pathlib import Path
from datetime import datetime, timedelta
from typing import Tuple, Optional, Dict

import MetaTrader5 as mt5

# -------- env --------
from dotenv import load_dotenv

ENV_PATH = os.getenv("DOTENV_FILE", ".env")
load_dotenv(ENV_PATH)

# =========================
# CORE SWITCHES
# =========================
MAGIC = int(os.getenv("MAGIC", "909912"))  # make unique per version
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "1.0"))

ALLOW_TRADING = os.getenv("ALLOW_TRADING","NO").upper()=="YES"
REPLACE_MASTER = os.getenv("REPLACE_MASTER","YES").upper()=="YES"
REPLACE_SEQUENCE = os.getenv("REPLACE_SEQUENCE","MIRROR_THEN_CLOSE").upper()  # or CLOSE_THEN_MIRROR
NETTING_MODE = os.getenv("NETTING_MODE","OFFSET").upper()  # OFFSET or FLIP

# allow-list
def _parse_list(val: str) -> set:
    return set([s.strip() for s in val.split(",") if s.strip()]) if val else set()

SYMBOL_ALLOWLIST = _parse_list(os.getenv("SYMBOL_ALLOWLIST",""))

# =========================
# MIRROR RULES / STOPS
# =========================
USE_MASTER_SL_AS_TP = os.getenv("USE_MASTER_SL_AS_TP","YES").upper()=="YES"
USE_MASTER_TP_AS_SL = os.getenv("USE_MASTER_TP_AS_SL","YES").upper()=="YES"

APPLY_SAFETY_SL_TO_MASTER = os.getenv("APPLY_SAFETY_SL_TO_MASTER","YES").upper()=="YES"

MASTER_SAFETY_SL_POINTS = int(os.getenv("MASTER_SAFETY_SL_POINTS","2000"))
DEFAULT_MIRROR_SL_POINTS = int(os.getenv("DEFAULT_MIRROR_SL_POINTS","1500"))
DEFAULT_MIRROR_TP_POINTS = int(os.getenv("DEFAULT_MIRROR_TP_POINTS","0"))

STOPDIST_BUFFER_POINTS = int(os.getenv("STOPDIST_BUFFER_POINTS","80"))

# =========================
# EXECUTION TOLERANCES
# =========================
MAX_SPREAD_POINTS = int(os.getenv("MAX_SPREAD_POINTS","300"))
DEVIATION_POINTS_OPEN = int(os.getenv("DEVIATION_POINTS_OPEN","20"))
DEVIATION_POINTS_CLOSE = int(os.getenv("DEVIATION_POINTS_CLOSE","20"))
MIRROR_MULTIPLIER = float(os.getenv("MIRROR_MULTIPLIER","1.0"))

# =========================
# DYNAMIC RISK / SIZING
# =========================
USE_DYNAMIC_RISK_SL = os.getenv("USE_DYNAMIC_RISK_SL","YES").upper()=="YES"
# risk per trade in *account currency* (USD for your new 200k account)
MIRROR_RISK_PER_TRADE = float(os.getenv("MIRROR_RISK_PER_TRADE","0"))  # 0 -> disabled, fall back to multiplier
MASTER_SAFETY_RISK_PER_TRADE = float(os.getenv("MASTER_SAFETY_RISK_PER_TRADE","0"))

# ATR config
ATR_PERIOD = int(os.getenv("ATR_PERIOD","14"))
ATR_TIMEFRAME_STR = os.getenv("ATR_TIMEFRAME","M5").upper()
ATR_SL_MULT = float(os.getenv("ATR_SL_MULT","2.0"))
RR_TP_MULT = float(os.getenv("RR_TP_MULT","3.0"))  # e.g., risk 1 to make 3

# soft constraints for dynamic SL
MIN_SL_POINTS = int(os.getenv("MIN_SL_POINTS","200"))     # floor to avoid too-tight stops
MAX_SL_POINTS = int(os.getenv("MAX_SL_POINTS","10000"))   # ceiling to avoid absurd stops

# =========================
# RISK CONTROLS
# =========================
DAILY_LOSS_LIMIT = float(os.getenv("DAILY_LOSS_LIMIT","0"))           # realized+optional floating (mirror) loss cap
DAILY_DRAWDOWN_LIMIT = float(os.getenv("DAILY_DRAWDOWN_LIMIT","0"))   # equity drop from today's start
ABS_MAX_DRAWDOWN = float(os.getenv("ABS_MAX_DRAWDOWN","0"))

PCT_DAILY_LOSS_LIMIT = float(os.getenv("PCT_DAILY_LOSS_LIMIT","0"))        # fraction of today's start
PCT_DAILY_DRAWDOWN_LIMIT = float(os.getenv("PCT_DAILY_DRAWDOWN_LIMIT","0"))# fraction of today's start

COUNT_FLOATING_IN_LOSS = os.getenv("COUNT_FLOATING_IN_LOSS","YES").upper()=="YES"
FLATTEN_ON_BREACH = os.getenv("FLATTEN_ON_BREACH","YES").upper()=="YES"
HALT_ON_BREACH = os.getenv("HALT_ON_BREACH","YES").upper()=="YES"

# =========================
# KILL / TOGGLES
# =========================
PANIC_FILE = Path(os.getenv("PANIC_FILE","panic_v2.txt"))
PANIC_FLATTEN_MANUAL = os.getenv("PANIC_FLATTEN_MANUAL","YES").upper()=="YES"
TOGGLE_FILE = Path(os.getenv("TRADING_TOGGLE_FILE","mirror_toggle_v2.txt"))

# =========================
# OPTIONAL LOGIN (usually unnecessary if MT5 terminal already logged in)
# =========================
LOGIN  = os.getenv("MT5_LOGIN")
PASSWORD = os.getenv("MT5_PASSWORD")
SERVER = os.getenv("MT5_SERVER")

# -------- session state --------
_SESSION_START_EQUITY: Optional[float] = None
_TODAY_START_EQUITY: Optional[float] = None
_LAST_DAY = None
_HARD_STOP_ON = False

# -------- utilities --------
def trading_enabled_now() -> bool:
    if not ALLOW_TRADING:
        return False
    if TOGGLE_FILE.exists():
        try:
            if "OFF" in TOGGLE_FILE.read_text(encoding="utf-8").strip().upper():
                return False
        except Exception:
            pass
    return True

def initialize():
    if LOGIN and PASSWORD and SERVER:
        ok = mt5.initialize(login=int(LOGIN), password=PASSWORD, server=SERVER)
    else:
        ok = mt5.initialize()
    if not ok:
        raise RuntimeError(f"MT5 init failed: {mt5.last_error()}")
    ai = mt5.account_info()
    if not ai:
        raise RuntimeError("No account_info(); is MT5 open & logged in?")
    mode = {0:"RETAIL_NETTING",1:"EXCHANGE",2:"RETAIL_HEDGING"}.get(getattr(ai,"margin_mode",0), str(getattr(ai,"margin_mode",0)))
    print(f"Connected: {ai.name} | {ai.server} | Mode={mode} | Equity={ai.equity:.2f}")
    print(f"Trading={'ON' if trading_enabled_now() else 'OFF'} | Multiplier={MIRROR_MULTIPLIER} | MaxSpread={MAX_SPREAD_POINTS} pts | NettingMode={NETTING_MODE} | ReplaceMaster={'YES' if REPLACE_MASTER else 'NO'} | Seq={REPLACE_SEQUENCE}")
    global _SESSION_START_EQUITY, _TODAY_START_EQUITY, _LAST_DAY
    _SESSION_START_EQUITY = float(ai.equity)
    _TODAY_START_EQUITY = float(ai.equity)
    _LAST_DAY = datetime.now().date()
    print(f"[Risk] Session start equity = {_SESSION_START_EQUITY:.2f}")
    print(f"[Risk] New day -> start equity snapshot = {_TODAY_START_EQUITY:.2f}")
    return ai

def ensure_symbol(sym: str) -> Tuple[str, mt5.SymbolInfo]:
    info = mt5.symbol_info(sym)
    if info is None:
        matches = [s.name for s in (mt5.symbols_get(sym+"*") or [])]
        if matches:
            sym = matches[0]
            info = mt5.symbol_info(sym)
            print(f"Using '{sym}' (matched symbol)")
        else:
            raise RuntimeError(f"Symbol not found: {sym}")
    if not info.visible and not mt5.symbol_select(sym, True):
        raise RuntimeError(f"Cannot select: {sym}")
    return sym, info

def get_fill_mode(info: mt5.SymbolInfo):
    fm = getattr(info, "filling_mode", None) or getattr(info, "trade_fill_mode", None)
    allowed = (mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_RETURN)
    return fm if fm in allowed else mt5.ORDER_FILLING_IOC

def spread_points(info: mt5.SymbolInfo) -> int:
    sp = getattr(info, "spread", None)
    return int(sp) if sp is not None else 0

def mirror_side(side: str) -> str:
    return "SELL" if side=="BUY" else "BUY"

def is_manual(p) -> bool:
    return getattr(p, "magic", 0) == 0

def mirror_comment(master_ticket: int) -> str:
    return f"mirror:{master_ticket}"

def min_stop_points(info: mt5.SymbolInfo) -> int:
    return int(getattr(info,"trade_stops_level",0) or 0) + STOPDIST_BUFFER_POINTS

def clamp_sl_tp_for_side(info: mt5.SymbolInfo, side: str, price: float, sl: Optional[float], tp: Optional[float]) -> Tuple[Optional[float], Optional[float]]:
    """Force SL/TP onto valid sides & beyond min stop distance. If invalid → drop."""
    pt = info.point
    need = min_stop_points(info) * pt
    if side == "BUY":
        if sl is not None and not (sl < price - need): sl = None
        if tp is not None and not (tp > price + need): tp = None
    else:
        if sl is not None and not (sl > price + need): sl = None
        if tp is not None and not (tp < price - need): tp = None
    return sl, tp

def timeframe_from_str(s: str) -> int:
    s = s.upper()
    tf_map = {
        "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15,
        "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4,
        "D1": mt5.TIMEFRAME_D1
    }
    return tf_map.get(s, mt5.TIMEFRAME_M5)

def calc_atr_points(symbol: str, info: mt5.SymbolInfo) -> Optional[float]:
    """Return ATR in *points* for the configured timeframe/period; None on failure."""
    tf = timeframe_from_str(ATR_TIMEFRAME_STR)
    count = ATR_PERIOD + 2
    now = datetime.now()
    rates = mt5.copy_rates_from(symbol, tf, now, count)
    if rates is None or len(rates) < ATR_PERIOD + 1:
        return None
    highs = [r['high'] for r in rates]
    lows  = [r['low']  for r in rates]
    closes= [r['close']for r in rates]
    trs = []
    for i in range(1, len(rates)):
        h, l, pc = highs[i], lows[i], closes[i-1]
        tr = max(h-l, abs(h-pc), abs(l-pc))
        trs.append(tr)
    atr_price = sum(trs[-ATR_PERIOD:]) / ATR_PERIOD
    # convert price distance to points
    return atr_price / info.point

def order_value_per_point_per_lot(symbol: str, info: mt5.SymbolInfo) -> Optional[float]:
    """
    Robust value-per-point-per-lot using order_calc_profit on a 1-point move.
    Returns profit (account currency) for +1 point move on 1.0 lot.
    """
    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        return None
    mid = (tick.bid + tick.ask) / 2.0
    p_open = mid
    p_close = mid + info.point
    try:
        val = mt5.order_calc_profit(mt5.ORDER_TYPE_BUY, symbol, 1.0, p_open, p_close)
        if val is None:
            return None
        return abs(float(val))
    except Exception:
        return None

def round_volume(info: mt5.SymbolInfo, vol: float) -> float:
    step = getattr(info, "volume_step", 0.01) or 0.01
    vmin = getattr(info, "volume_min", 0.01) or 0.01
    vmax = getattr(info, "volume_max", 100.0) or 100.0
    # snap to step
    steps = math.floor(vol / step)
    vol2 = max(vmin, min(vmax, round(steps * step, 6)))
    return vol2

def safety_level_from_points(info: mt5.SymbolInfo, side: str, ref_price: float, points: int, level_type="SL") -> Optional[float]:
    if points <= 0:
        return None
    dist = points * info.point
    if side == "BUY":
        return ref_price - dist if level_type=="SL" else ref_price + dist
    else:
        return ref_price + dist if level_type=="SL" else ref_price - dist

def dynamic_sl_tp_points(symbol: str, info: mt5.SymbolInfo) -> Tuple[int, int]:
    """Compute (sl_points, tp_points) using ATR×mult and RR; fall back to defaults."""
    sl_pts = None
    if USE_DYNAMIC_RISK_SL:
        atr_pts = calc_atr_points(symbol, info)
        if atr_pts:
            sl_pts = int(max(MIN_SL_POINTS, min(MAX_SL_POINTS, atr_pts * ATR_SL_MULT)))
    if sl_pts is None or sl_pts <= 0:
        sl_pts = DEFAULT_MIRROR_SL_POINTS if DEFAULT_MIRROR_SL_POINTS>0 else MIN_SL_POINTS
    tp_pts = 0
    if RR_TP_MULT > 0:
        tp_pts = int(max(0, sl_pts * RR_TP_MULT))
    elif DEFAULT_MIRROR_TP_POINTS > 0:
        tp_pts = DEFAULT_MIRROR_TP_POINTS
    return sl_pts, tp_pts

def build_mirror_plan(master, info: mt5.SymbolInfo, mirror_side_str: str, entry_price: float) -> Tuple[Optional[float], Optional[float], float]:
    """
    Returns (mirror_sl_price, mirror_tp_price, mirror_volume)
    - map from master if present and allowed
    - else build dynamic SL/TP in points
    - size position to MIRROR_RISK_PER_TRADE if set; else use multiplier*master.volume
    """
    # 1) desired SL/TP from mapping
    m_sl = float(getattr(master,"sl",0.0) or 0.0)
    m_tp = float(getattr(master,"tp",0.0) or 0.0)

    desired_tp = m_sl if (USE_MASTER_SL_AS_TP and m_sl>0) else None
    desired_sl = m_tp if (USE_MASTER_TP_AS_SL and m_tp>0) else None


    # 2) if missing, compute dynamic points
    dyn_sl_pts = dyn_tp_pts = None
    if desired_sl is None or desired_tp is None:
        sl_pts, tp_pts = dynamic_sl_tp_points(master.symbol, info)
        dyn_sl_pts, dyn_tp_pts = sl_pts, tp_pts
        if desired_sl is None and dyn_sl_pts>0:
            desired_sl = safety_level_from_points(info, mirror_side_str, entry_price, dyn_sl_pts, "SL")
        if desired_tp is None and dyn_tp_pts>0:
            desired_tp = safety_level_from_points(info, mirror_side_str, entry_price, dyn_tp_pts, "TP")

    # 3) clamp to broker stop rules
    desired_sl, desired_tp = clamp_sl_tp_for_side(info, mirror_side_str, entry_price, desired_sl, desired_tp)

    # 4) size by risk if configured, else mirror multiplier
    vol = master.volume * MIRROR_MULTIPLIER
    vpppl = order_value_per_point_per_lot(master.symbol, info)

    if MIRROR_RISK_PER_TRADE > 0 and vpppl and (desired_sl is not None):
        # compute stop distance in points (price → points)
        sl_points = int(abs(entry_price - desired_sl) / info.point)
        risk_per_lot = vpppl * sl_points
        if risk_per_lot > 0:
            vol = MIRROR_RISK_PER_TRADE / risk_per_lot

    vol = round_volume(info, max(vol, 0.0))
    return desired_sl, desired_tp, vol

def set_master_safety_sl_if_needed(master, info: mt5.SymbolInfo, side: str):
    """If your manual has no SL, optionally add a safety SL based on risk per trade or points."""
    if not APPLY_SAFETY_SL_TO_MASTER:
        return
    if float(getattr(master,"sl",0.0) or 0.0) > 0:
        return  # already has SL
    tick = mt5.symbol_info_tick(master.symbol)
    if not tick:
        return
    ref = tick.bid if side=="BUY" else tick.ask

    # Prefer risk-based SL distance if configured (for the manual's current lot)
    info = mt5.symbol_info(master.symbol)
    vpppl = order_value_per_point_per_lot(master.symbol, info)
    sl_pts_plan = None

    if MASTER_SAFETY_RISK_PER_TRADE > 0 and vpppl and master.volume > 0:
        pts = MASTER_SAFETY_RISK_PER_TRADE / (vpppl * master.volume)
        sl_pts_plan = int(max(MIN_SL_POINTS, min(MAX_SL_POINTS, pts)))
    elif MASTER_SAFETY_SL_POINTS > 0:
        sl_pts_plan = MASTER_SAFETY_SL_POINTS
    else:
        # last resort: ATR-driven
        pts = dynamic_sl_tp_points(master.symbol, info)[0]
        sl_pts_plan = pts

    sl_price = safety_level_from_points(info, side, ref, sl_pts_plan, "SL")
    sl_price, _ = clamp_sl_tp_for_side(info, side, ref, sl_price, None)
    if sl_price is not None:
        modify_position_sl_tp(master, new_sl=sl_price)

# -------- trade I/O --------
def open_order(symbol: str, side: str, volume: float, info: mt5.SymbolInfo, link_ticket=None, sl=None, tp=None):
    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        print(f"[SKIP] no tick for {symbol}")
        return None
    price = tick.ask if side=="BUY" else tick.bid
    digits = getattr(info, "digits", 5) or 5
    if sl is not None: sl = round(float(sl), digits)
    if tp is not None: tp = round(float(tp), digits)
    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(volume),
        "type": mt5.ORDER_TYPE_BUY if side=="BUY" else mt5.ORDER_TYPE_SELL,
        "price": price,
        "sl": float(sl) if sl else 0.0,
        "tp": float(tp) if tp else 0.0,
        "deviation": DEVIATION_POINTS_OPEN,
        "magic": MAGIC,
        "comment": mirror_comment(link_ticket) if link_ticket else "mirror",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": get_fill_mode(info),
    }
    if not trading_enabled_now():
        print("[DRY RUN] Would open:", {"symbol":symbol,"side":side,"vol":volume,"sl":sl,"tp":tp,"link":link_ticket})
        return None
    res = mt5.order_send(req)
    if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
        print(f"[ERR] Open failed: {getattr(res,'retcode',None)} {getattr(res,'comment','')}")
        return None
    print(f"[OK] Opened {symbol} {side} vol={volume} sl={sl} tp={tp} link={link_ticket} order={res.order}")
    return res

def close_position(pos, comment_suffix="_close"):
    symbol = pos.symbol
    info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if not info or not tick:
        print(f"[ERR] cannot close: {symbol} info/tick missing")
        return None
    price = tick.bid if pos.type == mt5.POSITION_TYPE_BUY else tick.ask
    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(pos.volume),
        "type": mt5.ORDER_TYPE_SELL if pos.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY,
        "position": pos.ticket,
        "price": price,
        "deviation": DEVIATION_POINTS_CLOSE,
        "magic": getattr(pos,"magic",MAGIC),  # preserve owner
        "comment": (getattr(pos,"comment","") or "mirror") + comment_suffix,
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": get_fill_mode(info),
    }
    if not trading_enabled_now():
        print("[DRY RUN] Would close:", {"symbol":symbol,"ticket":pos.ticket,"vol":pos.volume})
        return None
    res = mt5.order_send(req)
    if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
        print(f"[ERR] Close failed: {getattr(res,'retcode',None)} {getattr(res,'comment','')}")
        return None
    print(f"[OK] Closed {symbol} ticket={pos.ticket}")
    return res

def modify_position_sl_tp(pos, new_sl=None, new_tp=None):
    if new_sl is None and new_tp is None:
        return None
    info = mt5.symbol_info(pos.symbol)
    if not info:
        return None
    digits = getattr(info,"digits",5) or 5
    sl = round(float(new_sl), digits) if new_sl is not None else 0.0
    tp = round(float(new_tp), digits) if new_tp is not None else 0.0
    req = {
        "action": mt5.TRADE_ACTION_SLTP,
        "symbol": pos.symbol,
        "position": pos.ticket,
        "sl": sl,
        "tp": tp,
        "magic": getattr(pos,"magic",0),
        "comment": (getattr(pos,"comment","") or "manual") + "_safety",
    }
    if not trading_enabled_now():
        print("[DRY RUN] Would modify SL/TP:", {"symbol":pos.symbol,"ticket":pos.ticket,"sl":sl,"tp":tp})
        return None
    r = mt5.order_send(req)
    if r is None or r.retcode != mt5.TRADE_RETCODE_DONE:
        print(f"[ERR] Modify failed: {getattr(r,'retcode',None)} {getattr(r,'comment','')}")
        return None
    print(f"[OK] Set SL/TP on {pos.symbol} ticket={pos.ticket} sl={sl} tp={tp}")
    return r

def index_manual_positions() -> Dict[int, object]:
    poss = mt5.positions_get() or []
    # symbol allow-list filter (if provided)
    if SYMBOL_ALLOWLIST:
        return {p.ticket: p for p in poss if is_manual(p) and p.symbol in SYMBOL_ALLOWLIST}
    return {p.ticket: p for p in poss if is_manual(p)}

def index_mirror_positions() -> Dict[int, object]:
    poss = mt5.positions_get() or []
    out = {}
    for p in poss:
        if getattr(p,"magic",0)==MAGIC and (getattr(p,"comment","") or "").startswith("mirror:"):
            try:
                link = int(p.comment.split(":")[1])
                out[link] = p
            except Exception:
                pass
    return out

# -------- risk accounting --------
def now_local():
    return datetime.now()

def today_bounds_local():
    n = now_local()
    start = n.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start, end

def sum_bot_realized_today():
    start, end = today_bounds_local()
    deals = mt5.history_deals_get(start, end) or []
    realized = 0.0
    for d in deals:
        try:
            if getattr(d,"magic",0)==MAGIC:
                realized += float(getattr(d,"profit",0.0) or 0.0)
                realized += float(getattr(d,"commission",0.0) or 0.0)
                realized += float(getattr(d,"swap",0.0) or 0.0)
        except Exception:
            continue
    return realized

def sum_bot_floating_now():
    poss = mt5.positions_get() or []
    return sum(float(getattr(p,"profit",0.0) or 0.0) for p in poss if getattr(p,"magic",0)==MAGIC)

def reset_day_if_needed():
    global _TODAY_START_EQUITY, _LAST_DAY, _HARD_STOP_ON
    day = now_local().date()
    if _LAST_DAY is None or day != _LAST_DAY:
        ai = mt5.account_info()
        _TODAY_START_EQUITY = float(ai.equity) if ai else None
        _LAST_DAY = day
        _HARD_STOP_ON = False
        print(f"[Risk] New day -> start equity snapshot = {_TODAY_START_EQUITY:.2f}")

def current_limits_state():
    reset_day_if_needed()
    ai = mt5.account_info()
    if not ai:
        return False, "no account_info"

    equity_now = float(ai.equity)

    realized_today = sum_bot_realized_today()
    floating_now  = sum_bot_floating_now() if COUNT_FLOATING_IN_LOSS else 0.0
    loss_today = -min(0.0, realized_today + floating_now)  # positive when losing

    dd_today = max(0.0, (_TODAY_START_EQUITY - equity_now)) if _TODAY_START_EQUITY is not None else 0.0
    dd_abs = max(0.0, (_SESSION_START_EQUITY - equity_now)) if _SESSION_START_EQUITY is not None else 0.0

    # thresholds (MIN of absolute and % caps when %>0)
    loss_limit = DAILY_LOSS_LIMIT if DAILY_LOSS_LIMIT > 0 else float("inf")
    dd_limit   = DAILY_DRAWDOWN_LIMIT if DAILY_DRAWDOWN_LIMIT > 0 else float("inf")

    if PCT_DAILY_LOSS_LIMIT > 0 and _TODAY_START_EQUITY is not None:
        loss_limit = min(loss_limit, PCT_DAILY_LOSS_LIMIT * _TODAY_START_EQUITY)
    if PCT_DAILY_DRAWDOWN_LIMIT > 0 and _TODAY_START_EQUITY is not None:
        dd_limit = min(dd_limit, PCT_DAILY_DRAWDOWN_LIMIT * _TODAY_START_EQUITY)

    breaches = []
    if loss_limit < float("inf") and loss_today >= loss_limit:
        breaches.append(f"DailyLoss {loss_today:.2f}≥{loss_limit:.2f}")
    if dd_limit < float("inf") and dd_today >= dd_limit:
        breaches.append(f"DailyDD {dd_today:.2f}≥{dd_limit:.2f}")
    if ABS_MAX_DRAWDOWN > 0 and dd_abs >= ABS_MAX_DRAWDOWN:
        breaches.append(f"AbsDD {dd_abs:.2f}≥{ABS_MAX_DRAWDOWN:.2f}")

    if breaches:
        return True, " | ".join(breaches)
    return False, f"OK (loss_today={loss_today:.2f}, dd_today={dd_today:.2f}, dd_abs={dd_abs:.2f})"

def remaining_daily_risk_budget() -> float:
    """A rough cap to avoid oversizing when near the limits."""
    reset_day_if_needed()
    ai = mt5.account_info()
    if not ai or _TODAY_START_EQUITY is None:
        return float("inf")
    # compute remaining vs each active limit and take the minimum positive remainder
    equity_now = float(ai.equity)
    realized_today = sum_bot_realized_today()
    floating_now  = sum_bot_floating_now() if COUNT_FLOATING_IN_LOSS else 0.0
    loss_today = -min(0.0, realized_today + floating_now)
    dd_today = max(0.0, (_TODAY_START_EQUITY - equity_now))

    candidates = []

    if DAILY_LOSS_LIMIT > 0:
        candidates.append(max(0.0, DAILY_LOSS_LIMIT - loss_today))
    if PCT_DAILY_LOSS_LIMIT > 0:
        candidates.append(max(0.0, PCT_DAILY_LOSS_LIMIT*_TODAY_START_EQUITY - loss_today))

    if DAILY_DRAWDOWN_LIMIT > 0:
        candidates.append(max(0.0, DAILY_DRAWDOWN_LIMIT - dd_today))
    if PCT_DAILY_DRAWDOWN_LIMIT > 0:
        candidates.append(max(0.0, PCT_DAILY_DRAWDOWN_LIMIT*_TODAY_START_EQUITY - dd_today))

    # if no limits active, infinite budget
    return min(candidates) if candidates else float("inf")

def flatten_all(mirror_only=False):
    poss = mt5.positions_get() or []
    to_close = []
    for p in poss:
        if mirror_only:
            if getattr(p,"magic",0)==MAGIC: to_close.append(p)
        else:
            to_close.append(p)
    if not to_close:
        return
    print(f"[PANIC] Flattening {len(to_close)} position(s) (mirror_only={mirror_only})")
    for p in to_close:
        close_position(p, comment_suffix="_panic")

# -------- core actions --------
def handle_new_manual(master):
    # Stop immediately if hard-stopped
    if _HARD_STOP_ON:
        print("[HARD STOP] New manual trade detected; skipping mirror.")
        return

    # respect allow-list if provided
    if SYMBOL_ALLOWLIST and master.symbol not in SYMBOL_ALLOWLIST:
        print(f"[SKIP] {master.symbol} not in SYMBOL_ALLOWLIST")
        return

    symbol, info = ensure_symbol(master.symbol)
    sp = spread_points(info)
    if sp > MAX_SPREAD_POINTS:
        print(f"[SKIP] {symbol} spread too wide ({sp} > {MAX_SPREAD_POINTS} pts)")
        return

    # ⤵ these lines must be inside the function (same indent level as above)
    step = (info.volume_step or 0.01)
    vol  = max(info.volume_min, min(master.volume, info.volume_max))
    vol  = round(vol / step) * step  # snap to broker step

    side = "BUY" if master.type == mt5.POSITION_TYPE_BUY else "SELL"
    opp  = mirror_side(side)

    # master safety SL (mostly useful when not replacing)
    if not REPLACE_MASTER:
        set_master_safety_sl_if_needed(master, info, side)

    # compute mirror plan
    tick = mt5.symbol_info_tick(symbol)
    price = tick.ask if opp == "BUY" else tick.bid
    sl, tp, vol = build_mirror_plan(master, info, opp, price)

    # cap by remaining daily risk
    if MIRROR_RISK_PER_TRADE > 0:
        vpppl = order_value_per_point_per_lot(symbol, info)
        if vpppl and sl is not None:
            sl_points = int(abs(price - sl) / info.point)
            planned_risk = vpppl * sl_points * max(vol, 0.0)
            remaining = remaining_daily_risk_budget()
            if planned_risk > remaining and remaining > 0:
                scale = remaining / planned_risk
                vol = round_volume(info, max(0.0, vol * scale))
                print(f"[Risk] Volume scaled by {scale:.2f} to respect daily budget. New vol={vol}")

    # Replace logic
    if REPLACE_MASTER:
        # Sequence = open mirror first, then close manual (default), or reverse if configured
        if REPLACE_SEQUENCE == "MIRROR_THEN_CLOSE":
            opened = open_order(symbol, opp, vol, info, link_ticket=master.ticket, sl=sl, tp=tp)
            if opened is None:
                print("[ERR] Could not open mirror; abort closing master.")
                return
            close_position(master, comment_suffix="_replace")
        else:  # CLOSE_THEN_MIRROR
            res = close_position(master, comment_suffix="_replace")
            if res is None:
                print("[ERR] Could not close master for replacement; aborting open.")
                return
            open_order(symbol, opp, vol, info, link_ticket=master.ticket, sl=sl, tp=tp)
        return

    # Hedge mode: keep both
    open_order(symbol, opp, vol, info, link_ticket=master.ticket, sl=sl, tp=tp)

def adjust_volume_if_needed(master, mirror):
    """Keep hedge size near (master.volume * multiplier) on hedging accounts when NOT risk-sizing."""
    if MIRROR_RISK_PER_TRADE > 0:
        return  # risk-sized; don't chase master volume changes
    ai = mt5.account_info()
    is_hedge = getattr(ai,"margin_mode",0) == 2
    if not is_hedge:
        return
    desired = round(master.volume * MIRROR_MULTIPLIER, 3)
    diff = round(desired - mirror.volume, 3)
    info = mt5.symbol_info(mirror.symbol)
    step = (info.volume_step or 0.01)
    if abs(diff) < step:
        return

    link_ticket = None
    try:
        link_ticket = int((mirror.comment or "").split(":")[1])
    except Exception:
        pass

    if diff > 0:
        side = "BUY" if mirror.type == mt5.POSITION_TYPE_BUY else "SELL"
        open_order(mirror.symbol, side, abs(diff), info, link_ticket=link_ticket)
    else:
        tick = mt5.symbol_info_tick(mirror.symbol)
        price = tick.bid if mirror.type == mt5.POSITION_TYPE_BUY else tick.ask
        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": mirror.symbol,
            "volume": float(abs(diff)),
            "type": mt5.ORDER_TYPE_SELL if mirror.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY,
            "position": mirror.ticket,
            "price": price,
            "deviation": DEVIATION_POINTS_CLOSE,
            "magic": MAGIC,
            "comment": (mirror.comment or "mirror") + "_partial",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": get_fill_mode(info),
        }
        if not trading_enabled_now():
            print("[DRY RUN] Would partial-close:", {"symbol":mirror.symbol,"vol":abs(diff)})
        else:
            r = mt5.order_send(req)
            if r is None or r.retcode != mt5.TRADE_RETCODE_DONE:
                print(f"[ERR] Partial close failed: {getattr(r,'retcode',None)} {getattr(r,'comment','')}")
            else:
                print(f"[OK] Adjusted mirror by {abs(diff)} on {mirror.symbol}")

# -------- main loop --------
def main():
    try:
        initialize()
        seen = set()
        while True:
            # ---- panic kill ----
            if PANIC_FILE.exists():
                try:
                    txt = PANIC_FILE.read_text(encoding="utf-8").strip().upper()
                except Exception:
                    txt = ""
                if "KILL" in txt:
                    if trading_enabled_now():
                        flatten_all(mirror_only=not PANIC_FLATTEN_MANUAL)
                    globals()['_HARD_STOP_ON'] = True
                    time.sleep(POLL_SECONDS)
                    continue

            # ---- risk gate ----
            breach, msg = current_limits_state()
            if breach and not globals()['_HARD_STOP_ON']:
                print(f"[HARD STOP] {msg}")
                if FLATTEN_ON_BREACH and trading_enabled_now():
                    flatten_all(mirror_only=True)  # close bot positions only
                globals()['_HARD_STOP_ON'] = True

            # If hard-stopped: only allow closing mirrors when master closes (no new opens)
            if _HARD_STOP_ON and HALT_ON_BREACH:
                masters = index_manual_positions()
                mirrors = index_mirror_positions()
                if not REPLACE_MASTER:
                    for link_ticket, mir in list(mirrors.items()):
                        if link_ticket not in masters:
                            close_position(mir)
                time.sleep(POLL_SECONDS)
                continue

            # ---- normal loop ----
            masters = index_manual_positions()
            mirrors = index_mirror_positions()

            # detect new masters → open mirror/replace
            for ticket, mpos in masters.items():
                if ticket not in mirrors and ticket not in seen:
                    handle_new_manual(mpos)
                    seen.add(ticket)

            # adjust volumes (hedging only & not risk-sized)
            for ticket, mpos in masters.items():
                mir = mirrors.get(ticket)
                if mir:
                    adjust_volume_if_needed(mpos, mir)

            # in classic mirror mode: master closed → close mirror
            if not REPLACE_MASTER:
                for link_ticket, mir in list(mirrors.items()):
                    if link_ticket not in masters:
                        close_position(mir)

            time.sleep(POLL_SECONDS)
    finally:
        mt5.shutdown()

if __name__ == "__main__":
    main()
