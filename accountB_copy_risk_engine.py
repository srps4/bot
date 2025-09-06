
"""
accountB_copy_risk_engine.py  — Alnafie follower
- FTMO-style daily/overall clamps; percent daily optional
- Dynamic lot sizing from SL distance (master SL or synthetic ATR-based)
- Post-fill BE+/partial(50%@RR=1.0)/ATR trailing
- Session windows (killzones) toggleable via env
- Modes: MIRROR / REVERSE / ADAPT (via ALNAFIE_MODE)
- Env loader via DOTENV_FILE
"""

from __future__ import annotations
import os, math, time, logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo  # Py3.9+
import MetaTrader5 as mt5
import pandas as pd

# -----------------------
# minimal .env loader (before reading any env-dependent constants)
# -----------------------
def _load_env_from_file() -> None:
    path = os.getenv("DOTENV_FILE", "").strip()
    if not path:
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#") or "=" not in s:
                    continue
                k, v = s.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                os.environ[k] = v
        logging.getLogger("risk_engine").info(f"[env] loaded {path}")
    except Exception as e:
        logging.getLogger("risk_engine").warning(f"[env] could not read {path}: {e}")

_load_env_from_file()

# -----------------------
# ENV HELPERS
# -----------------------
def _get_str(k: str, default: str) -> str:
    v = os.getenv(k)
    return v if v not in (None, "") else default

def _get_float(k: str, default: float) -> float:
    v = _get_str(k, str(default))
    try:
        return float(v)
    except Exception:
        return float(default)

def _get_int(k: str, default: int) -> int:
    v = _get_str(k, str(default))
    try:
        return int(v)
    except Exception:
        return int(default)

def _get_bool(k: str, default: bool) -> bool:
    v = _get_str(k, "1" if default else "0").strip().lower()
    return v in ("1", "true", "yes", "y", "on")

def _parse_symbols(csv: str) -> set[str]:
    return {s.strip().upper() for s in csv.split(",") if s.strip()} if csv else set()

def _parse_killzones(s: str):
    """
    Input: '12:00-14:00,13:00-15:00,08:00-11:00,16:00-18:00'
    Output: [((12,0),(14,0)), ...]
    """
    out = []
    if not s:
        return out
    for token in s.split(","):
        token = token.strip()
        if not token or "-" not in token or ":" not in token:
            continue
        try:
            a, b = token.split("-")
            sh, sm = [int(x) for x in a.split(":")]
            eh, em = [int(x) for x in b.split(":")]
            out.append(((sh, sm), (eh, em)))
        except Exception:
            continue
    return out

# -----------------------
# CONFIG (defaults with ENV overrides)
# -----------------------
CEST = ZoneInfo("Europe/Berlin")

# Core budgets (10k defaults)
INITIAL_BALANCE              = _get_float("INITIAL_BALANCE", 10000.0)
DAILY_LOSS_LIMIT             = _get_float("DAILY_LOSS_LIMIT", 500.0)     # absolute USD daily cap
DAILY_LOSS_PCT               = _get_float("DAILY_LOSS_PCT",   0.0)       # optional %, of start-of-day equity
OVERALL_EQUITY_FLOOR         = _get_float("OVERALL_EQUITY_FLOOR", 9000.0)

BASE_RISK_PCT                = _get_float("BASE_RISK_PCT", 0.008)        # ~0.8% per trade
MDL_FRACTION_PER_TRADE       = _get_float("MDL_FRACTION_PER_TRADE", 0.20)
OVR_FRACTION_PER_TRADE       = _get_float("OVR_FRACTION_PER_TRADE", 0.20)
OPEN_RISK_FRACTION_OF_DAILY  = _get_float("OPEN_RISK_FRACTION_OF_DAILY", 0.50)

# Margin buffer for lot ceiling
MARGIN_FREE_BUFFER_PCT       = _get_float("MARGIN_FREE_BUFFER_PCT", 0.25)  # keep ≥25% equity as free margin

MIN_LOT                      = _get_float("MIN_LOT", 0.01)
MAX_LOT                      = _get_float("MAX_LOT", 5.0)
LOT_STEP                     = _get_float("LOT_STEP", 0.01)

SYMBOL_ALLOW                 = _parse_symbols(_get_str("SYMBOL_ALLOW", "XAUUSD"))
SESSION_FILTER_ON            = _get_bool("SESSION_FILTER_ON", False)

_KZ_DEFAULT = [
    ((12, 0), (14, 0)),  # Asian (placeholder CEST)
    ((13, 0), (15, 0)),  # NY
    ((8, 0),  (11, 0)),  # London open
    ((16, 0), (18, 0)),  # London close
]
KILLZONES_CEST               = _parse_killzones(_get_str("KILLZONES_CEST", "")) or _KZ_DEFAULT

_TF_MAP = {
    "M1": mt5.TIMEFRAME_M1,  "M2": mt5.TIMEFRAME_M2,  "M3": mt5.TIMEFRAME_M3,  "M4": mt5.TIMEFRAME_M4,
    "M5": mt5.TIMEFRAME_M5,  "M6": mt5.TIMEFRAME_M6,  "M10": mt5.TIMEFRAME_M10,"M12": mt5.TIMEFRAME_M12,
    "M15": mt5.TIMEFRAME_M15,"M20": mt5.TIMEFRAME_M20,"M30": mt5.TIMEFRAME_M30,
    "H1": mt5.TIMEFRAME_H1,  "H2": mt5.TIMEFRAME_H2,  "H3": mt5.TIMEFRAME_H3,  "H4": mt5.TIMEFRAME_H4
}
MANAGE_TIMEFRAME             = _TF_MAP.get(_get_str("MANAGE_TIMEFRAME", "M5").upper(), mt5.TIMEFRAME_M5)

# Modes
ALNAFIE_MODE                 = _get_str("ALNAFIE_MODE", "REVERSE").upper()  # MIRROR | REVERSE | ADAPT

# logger
log = logging.getLogger("risk_engine")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log.info(f"[cfg] MODE={ALNAFIE_MODE} SESSION_FILTER_ON={SESSION_FILTER_ON} KILLZONES_CEST={KILLZONES_CEST}")

# -----------------------
# TIME / STATE
# -----------------------
@dataclass
class DayState:
    date_key: str
    start_equity: float
    losses_today: float = 0.0
    cooldown_until: datetime | None = None

day_state: DayState | None = None

def now_cest() -> datetime:
    return datetime.now(CEST)

# -----------------------
# MT5 BASICS
# -----------------------
def ensure_mt5() -> None:
    if not mt5.initialize():
        raise RuntimeError(f"MT5 init failed: {mt5.last_error()}")

def get_equity() -> float:
    acct = mt5.account_info()
    if acct is None:
        raise RuntimeError("No account_info()")
    return float(acct.equity)

def get_symbol_info(symbol: str):
    info = mt5.symbol_info(symbol)
    if not info or not info.visible:
        mt5.symbol_select(symbol, True)
        info = mt5.symbol_info(symbol)
    if not info:
        raise RuntimeError(f"Symbol not available: {symbol}")
    return info

def round_lot(volume: float) -> float:
    # round down to lot step
    steps = math.floor(volume / LOT_STEP)
    vol = steps * LOT_STEP
    return max(MIN_LOT, min(MAX_LOT, vol))

# -----------------------
# DAILY / OVERALL LIMITS
# -----------------------
def daily_reset_if_needed() -> None:
    global day_state
    cest = now_cest()
    date_key = cest.strftime("%Y-%m-%d")
    if day_state is None or day_state.date_key != date_key:
        start_eq = get_equity()
        day_state = DayState(date_key=date_key, start_equity=start_eq, losses_today=0.0)
        log.info(f"[reset] New trading day (CEST). start_equity={start_eq:.2f}")

def remaining_daily_loss() -> float:
    assert day_state
    eq = get_equity()
    # derive percent budget if set; use the TIGHTER of absolute vs percent
    percent_budget = day_state.start_equity * DAILY_LOSS_PCT if DAILY_LOSS_PCT > 0 else float("inf")
    abs_budget     = DAILY_LOSS_LIMIT
    budget         = min(abs_budget, percent_budget)
    used           = max(0.0, day_state.start_equity - eq)  # equity drop from day start
    return max(0.0, budget - used)

def remaining_overall_loss() -> float:
    eq = get_equity()
    return max(0.0, eq - OVERALL_EQUITY_FLOOR)

def in_session_window(server_time: datetime) -> bool:
    if not SESSION_FILTER_ON:
        return True
    for (s_hm, e_hm) in KILLZONES_CEST:
        (sh, sm), (eh, em) = s_hm, e_hm
        start = server_time.replace(hour=sh, minute=sm, second=0, microsecond=0)
        end   = server_time.replace(hour=eh, minute=em, second=0, microsecond=0)
        if start <= server_time <= end:
            return True
    return False

# -----------------------
# INDICATORS / PRICING
# -----------------------
def get_atr_points(symbol: str, tf=MANAGE_TIMEFRAME, period: int = 14) -> float:
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, period + 1)
    if rates is None or len(rates) < period + 1:
        return 0.0
    df = pd.DataFrame(rates)
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        (df["high"] - df["low"]).abs(),
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(period).mean().iloc[-1]
    pnt = get_symbol_info(symbol).point
    return float(atr / pnt)

def value_per_point(symbol: str) -> float:
    """
    USD value per 1 point for 1.0 lot using order_calc_profit on a 1-point move.
    """
    info = get_symbol_info(symbol)
    price = mt5.symbol_info_tick(symbol).bid or info.last
    point = info.point
    vol = 1.0
    loss = mt5.order_calc_profit(mt5.ORDER_TYPE_BUY, symbol, vol, price, price - point)
    return abs(loss)  # USD per point per 1.0 lot

# -----------------------
# LOT SIZING / RISK
# -----------------------
def lots_for_risk(symbol: str, risk_usd: float, sl_points: float) -> float:
    if sl_points <= 0 or risk_usd <= 0:
        return 0.0
    vpp = value_per_point(symbol)
    if vpp <= 0:
        return 0.0
    lots = risk_usd / (sl_points * vpp)
    return round_lot(lots)

def synthetic_sl_points(symbol: str) -> float:
    info = get_symbol_info(symbol)
    # broker minimum stop distance (points)
    min_stop_pts = float(getattr(info, "trade_stops_level", 0) or 0)
    atr_period = int(_get_float("ATR_PERIOD", 14))
    trail_mult = _get_float("TRAIL_ATR_MULT", 1.5)
    atr_pts = get_atr_points(symbol, period=atr_period)
    syn_pts = max(int(trail_mult * atr_pts), int(min_stop_pts))
    return max(syn_pts, int(min_stop_pts))

def compute_allowed_risk_usd() -> float:
    rem_daily = remaining_daily_loss()
    rem_overall = remaining_overall_loss()
    base = BASE_RISK_PCT * INITIAL_BALANCE
    cap_mdl = MDL_FRACTION_PER_TRADE * rem_daily
    cap_ovr = OVR_FRACTION_PER_TRADE * rem_overall
    return max(0.0, min(base, cap_mdl, cap_ovr))

def sum_open_risk_usd() -> float:
    total = 0.0
    positions = mt5.positions_get() or []
    for pos in positions:
        sym = pos.symbol
        vpp = value_per_point(sym)
        info = get_symbol_info(sym)
        point = info.point
        price = pos.price_open
        sl = pos.sl
        if not sl or sl <= 0:
            sl_pts = synthetic_sl_points(sym)
        else:
            sl_pts = abs((price - sl) / point)
        total += abs(pos.volume) * sl_pts * vpp
    return total

def can_add_more_risk(additional_risk: float) -> bool:
    rem_daily = remaining_daily_loss()
    if rem_daily <= 0:
        return False
    open_risk_limit = OPEN_RISK_FRACTION_OF_DAILY * rem_daily
    return (sum_open_risk_usd() + additional_risk) <= open_risk_limit

# --- margin-aware max lots ---
def max_lots_by_margin(symbol: str, order_type: int, price: float, keep_free_frac: float = 0.25) -> float:
    acc = mt5.account_info()
    if acc is None:
        return MAX_LOT
    free   = float(acc.margin_free)
    equity = float(acc.equity)
    keep   = max(0.0, min(0.95, keep_free_frac)) * equity
    avail  = max(0.0, free - keep)
    if avail <= 0:
        return MIN_LOT
    m1 = mt5.order_calc_margin(order_type, symbol, 1.0, price)
    if m1 is None or m1 <= 0:
        return MAX_LOT
    steps = math.floor((avail / m1) / LOT_STEP)
    return max(MIN_LOT, min(MAX_LOT, steps * LOT_STEP))

# -----------------------
# ADAPT (for ADAPT mode)
# -----------------------
def _adapt_multiplier(now_cest_val: datetime) -> float:
    if ALNAFIE_MODE != "ADAPT":
        return 1.0
    try:
        base = float(_get_str("ADAPT_RISK_MULT_DEFAULT", "1.0"))
    except Exception:
        base = 1.0
    mult = base
    if _get_bool("SESSION_FILTER_ON", False):
        if 3 <= now_cest_val.hour < 7:  # crude Asian window in CEST
            try:
                mult = min(mult, float(_get_str("ADAPT_RISK_MULT_ASIAN", str(base))))
            except Exception:
                pass
    try:
        nfp_mult = float(_get_str("ADAPT_RISK_MULT_NFP", str(mult)))
        mult = min(mult, nfp_mult)
    except Exception:
        pass
    return max(0.1, min(mult, 2.0))

# -----------------------
# ENTRY & MANAGEMENT
# -----------------------
def prepare_copy_trade(master_symbol: str, master_type: str, master_entry: float, master_sl: float | None, master_tp: float | None):
    daily_reset_if_needed()

    if SYMBOL_ALLOW and master_symbol.upper() not in SYMBOL_ALLOW:
        log.info(f"[skip] {master_symbol} not in allowlist")
        return None

    server_now = now_cest()
    if not in_session_window(server_now):
        log.info(f"[skip] outside session window {server_now}")
        return None

    eq = get_equity()
    log.info(f"[budget] eq={eq:.2f} floor={OVERALL_EQUITY_FLOOR:.2f}")
    if eq <= OVERALL_EQUITY_FLOOR:
        log.warning("[BLOCK] equity at/under floor; stop trading")
        return None

    rem_daily = remaining_daily_loss()
    rem_overall = remaining_overall_loss()
    if rem_daily <= 0 or rem_overall <= 0:
        log.warning("[BLOCK] no remaining loss budget")
        return None

    risk_usd = compute_allowed_risk_usd()
    if risk_usd <= 0:
        log.info("[skip] zero allowed risk now")
        return None

    # If ADAPT mode, adjust allowed risk dynamically
    risk_usd = risk_usd * _adapt_multiplier(server_now)

    # Determine SL points
    info = get_symbol_info(master_symbol)
    pnt = info.point

    if master_sl and master_sl > 0:
        sl_points = abs((master_entry - master_sl) / pnt)
    else:
        sl_points = synthetic_sl_points(master_symbol)

    lots = lots_for_risk(master_symbol, risk_usd, sl_points)
    if lots < MIN_LOT:
        log.info(f"[skip] calc lots too small: {lots}")
        return None

    # --- Mode transform (mirror / reverse) ---
    m_side = master_type.lower()
    if ALNAFIE_MODE == "REVERSE":
        eff_side = "sell" if m_side == "buy" else "buy"
    else:
        eff_side = m_side  # MIRROR & ADAPT mirror side

    # Pull live price for clamp & margin
    tick = mt5.symbol_info_tick(master_symbol)
    if tick is None:
        log.error("[skip] no tick data")
        return None
    order_type = mt5.ORDER_TYPE_BUY if eff_side == "buy" else mt5.ORDER_TYPE_SELL
    price = tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid

    # Baseline SL from entry
    if eff_side == "buy":
        sl_price = master_entry - sl_points * pnt
    else:
        sl_price = master_entry + sl_points * pnt

    # Clamp SL to broker min distance from CURRENT price (stops + freeze + cushion)
    stops_pts  = float(getattr(info, "trade_stops_level", 0) or 0)
    freeze_pts = float(getattr(info, "freeze_level",      0) or 0)
    min_dist   = max(stops_pts, freeze_pts) * pnt
    if min_dist > 0:
        min_dist += 5 * pnt  # small cushion
        if order_type == mt5.ORDER_TYPE_BUY:
            max_allowed_sl = price - min_dist
            if sl_price > max_allowed_sl:
                sl_price = max_allowed_sl
        else:
            min_allowed_sl = price + min_dist
            if sl_price < min_allowed_sl:
                sl_price = min_allowed_sl

    # TP as per master if provided
    tp_price = master_tp if (master_tp and master_tp > 0) else 0.0

    # Open-risk cap
    vpp = value_per_point(master_symbol)
    add_risk = lots * sl_points * vpp
    if not can_add_more_risk(add_risk):
        fit_lots = lots
        while fit_lots >= MIN_LOT:
            add_risk_fit = fit_lots * sl_points * vpp
            if can_add_more_risk(add_risk_fit):
                lots = fit_lots
                add_risk = add_risk_fit
                break
            fit_lots = round_lot(fit_lots - LOT_STEP)
        else:
            log.info("[skip] cannot fit risk into open-risk cap")
            return None

    # Margin cap
    max_by_margin = max_lots_by_margin(master_symbol, order_type, price, keep_free_frac=MARGIN_FREE_BUFFER_PCT)
    if lots > max_by_margin:
        log.info(f"[cap] margin reduces lots {lots} -> {max_by_margin}")
        lots = max_by_margin
    if lots < MIN_LOT:
        log.info(f"[skip] margin left too small lot ({lots} < {MIN_LOT})")
        return None

    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": master_symbol,
        "type": order_type,
        "volume": float(lots),
        "price": price,
        "sl": round(sl_price, info.digits),
        "tp": round(tp_price, info.digits) if tp_price else 0.0,
        "deviation": 10,
        "magic": 909999,
        "comment": "B_COPY_RISK",
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    log.info(f"[copy-ready] {eff_side.upper()} {master_symbol} lots={lots} risk≈${add_risk:.2f} sl_pts={sl_points:.0f}")
    return req

def send_order(req: dict):
    res = mt5.order_send(req)
    if res is None:
        log.error(f"order_send failed: {mt5.last_error()}")
        return None
    if res.retcode != mt5.TRADE_RETCODE_DONE:
        log.error(f"order_send retcode={res.retcode} comment={res.comment}")
        return None
    log.info(f"[filled] ticket={res.order} vol={req['volume']} price={req['price']}")
    return res

# ------------- Post-fill management -------------
def rr_for_position(pos) -> float:
    info = get_symbol_info(pos.symbol)
    point = info.point
    if not pos.sl or pos.sl == 0:
        sl_pts = synthetic_sl_points(pos.symbol)
    else:
        sl_pts = abs((pos.price_open - pos.sl) / point)
    if sl_pts <= 0:
        return 0.0
    # reward so far (unrealized) in points
    tick = mt5.symbol_info_tick(pos.symbol)
    cur = tick.bid if pos.type == mt5.POSITION_TYPE_BUY else tick.ask
    r_pts = (cur - pos.price_open) / point if pos.type == mt5.POSITION_TYPE_BUY else (pos.price_open - cur) / point
    return float(r_pts / sl_pts)

def move_to_be_plus(pos) -> None:
    info = get_symbol_info(pos.symbol)
    ticks = mt5.symbol_info_tick(pos.symbol)
    spread = abs(ticks.ask - ticks.bid)
    if pos.type == mt5.POSITION_TYPE_BUY:
        new_sl = min(pos.price_open + spread, ticks.bid)
    else:
        new_sl = max(pos.price_open - spread, ticks.ask)
    req = {"action": mt5.TRADE_ACTION_SLTP, "symbol": pos.symbol, "position": pos.ticket, "sl": round(new_sl, info.digits), "tp": pos.tp}
    mt5.order_send(req)

def partial_close(pos, fraction: float = 0.5) -> None:
    vol = max(MIN_LOT, round_lot(pos.volume * fraction))
    if vol <= 0 or vol >= pos.volume:
        return
    tick = mt5.symbol_info_tick(pos.symbol)
    price = tick.bid if pos.type == mt5.POSITION_TYPE_BUY else tick.ask
    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "position": pos.ticket,
        "symbol": pos.symbol,
        "volume": float(vol),
        "type": mt5.ORDER_TYPE_SELL if pos.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY,
        "price": price,
        "deviation": 10,
        "magic": 909999,
        "comment": "B_COPY_PARTIAL",
    }
    mt5.order_send(req)

def trail_atr(pos) -> None:
    info = get_symbol_info(pos.symbol)
    tick = mt5.symbol_info_tick(pos.symbol)
    last = tick.bid if pos.type == mt5.POSITION_TYPE_BUY else tick.ask
    atr_period = int(_get_float("ATR_PERIOD", 14))
    atr_pts = get_atr_points(pos.symbol, period=atr_period)
    if atr_pts <= 0:
        return
    point = info.point
    trail_mult = _get_float("TRAIL_ATR_MULT", 1.5)
    offset = atr_pts * trail_mult * point
    new_sl = (last - offset) if pos.type == mt5.POSITION_TYPE_BUY else (last + offset)
    if pos.sl and ((pos.type == mt5.POSITION_TYPE_BUY and new_sl <= pos.sl) or (pos.type == mt5.POSITION_TYPE_SELL and new_sl >= pos.sl)):
        return
    req = {"action": mt5.TRADE_ACTION_SLTP, "symbol": pos.symbol, "position": pos.ticket, "sl": round(new_sl, info.digits), "tp": pos.tp}
    mt5.order_send(req)

def manage_positions_loop() -> None:
    # call this every ~5–15 seconds
    positions = mt5.positions_get() or []
    for pos in positions:
        if pos.comment != "B_COPY_RISK":
            continue
        rr = rr_for_position(pos)
        if rr >= 1.0 and pos.sl and pos.volume > MIN_LOT:
            move_to_be_plus(pos)
            partial_close(pos, fraction=0.5)
        trail_atr(pos)

# -----------------------
# Example wiring
# -----------------------
if __name__ == "__main__":
    ensure_mt5()
    log.info("Risk engine ready.")
    # Example:
    # master = {"symbol":"XAUUSD","type":"buy","entry":3550.00,"sl":0.0,"tp":0.0}
    # req = prepare_copy_trade(master["symbol"], master["type"], master["entry"], master["sl"], master["tp"])
    # if req: send_order(req)
    # while True:
    #     daily_reset_if_needed()
    #     manage_positions_loop()
    #     time.sleep(5)
