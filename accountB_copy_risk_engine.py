"""
accountB_copy_risk_engine.py
- FTMO 100k risk engine with daily/overall clamps
- Dynamic lot sizing from SL distance (master SL or synthetic ATR-based)
- Post-fill BE+/partial(50%@RR=1.0)/ATR trailing
- Session windows (killzones) auto-shifted via broker server time
"""

import math, time, logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo  # Python 3.9+
import MetaTrader5 as mt5
import pandas as pd

# -----------------------
# CONFIG
# -----------------------
INITIAL_BALANCE = 100_000.0
DAILY_LOSS_LIMIT = 5_000.0    # FTMO MDL (conservative: fixed 5k/day)
OVERALL_EQUITY_FLOOR = 90_000.0

BASE_RISK_PCT = 0.008         # 0.8% of initial per trade
MDL_FRACTION_PER_TRADE = 0.20 # max 20% of remaining daily
OVR_FRACTION_PER_TRADE = 0.20 # max 20% of remaining overall

OPEN_RISK_FRACTION_OF_DAILY = 0.50  # sum open risk ≤ 50% of remaining MDL
MIN_LOT = 0.01
MAX_LOT = 50.0              # adjust to broker
LOT_STEP = 0.01

SYMBOL_ALLOW = {"XAUUSD"}
SESSION_FILTER_ON = True

# Killzones in CEST (FTMO reset tz); supply tuples of (hh,mm)-(hh,mm)
KILLZONES_CEST = [
    ((12,0), (14,0)),  # Asian -> 02:00–06:00 BST shifts; keep placeholders if you want
    ((13,0), (15,0)),  # NY 13:00–15:00 BST approx; align per your server if needed
    ((8,0),  (11,0)),  # London open
    ((16,0), (18,0)),  # London close
]

TRAIL_ATR_MULT = 1.5
ATR_PERIOD = 14
MANAGE_TIMEFRAME = mt5.TIMEFRAME_M5

CEST = ZoneInfo("Europe/Berlin")  # FTMO resets here at 00:00

log = logging.getLogger("risk_engine")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

@dataclass
class DayState:
    date_key: str
    start_equity: float
    losses_today: float = 0.0
    cooldown_until: datetime | None = None

day_state: DayState | None = None

def now_cest() -> datetime:
    return datetime.now(CEST)

def ensure_mt5():
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
    # round down to broker lot step
    steps = math.floor(volume / LOT_STEP)
    return max(MIN_LOT, min(MAX_LOT, steps * LOT_STEP))

def daily_reset_if_needed():
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
    used = max(0.0, day_state.start_equity - eq)  # equity drop from start
    return max(0.0, DAILY_LOSS_LIMIT - used)

def remaining_overall_loss() -> float:
    eq = get_equity()
    return max(0.0, eq - OVERALL_EQUITY_FLOOR)

def in_session_window(server_time: datetime) -> bool:
    if not SESSION_FILTER_ON:
        return True
    # server_time is assumed CEST for simplicity; adapt if server tz differs
    h, m = server_time.hour, server_time.minute
    for (s_hm, e_hm) in KILLZONES_CEST:
        (sh, sm), (eh, em) = s_hm, e_hm
        start = server_time.replace(hour=sh, minute=sm, second=0, microsecond=0)
        end   = server_time.replace(hour=eh, minute=em, second=0, microsecond=0)
        if start <= server_time <= end:
            return True
    return False

def get_atr_points(symbol: str, tf=MANAGE_TIMEFRAME, period=ATR_PERIOD) -> float:
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, period+1)
    if rates is None or len(rates) < period+1:
        return 0.0
    df = pd.DataFrame(rates)
    # TR = max(H-L, abs(H-prevC), abs(L-prevC)) in price units
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        (df["high"] - df["low"]).abs(),
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs()
    ], axis=1).max(axis=1)
    atr = tr.rolling(period).mean().iloc[-1]
    # convert ATR price to points:
    pnt = get_symbol_info(symbol).point
    return float(atr / pnt)

def value_per_point(symbol: str) -> float:
    """
    Infer USD value per 1 point for 1.0 lot by using order_calc_profit on a 1 point move.
    """
    info = get_symbol_info(symbol)
    price = mt5.symbol_info_tick(symbol).bid or info.last
    point = info.point
    vol = 1.0
    # simulate 1-point loss on a BUY
    loss = mt5.order_calc_profit(mt5.ORDER_TYPE_BUY, symbol, vol, price, price - point)
    return abs(loss)  # USD per point per 1.0 lot

def lots_for_risk(symbol: str, risk_usd: float, sl_points: float) -> float:
    if sl_points <= 0:
        return 0.0
    vpp = value_per_point(symbol)
    if vpp <= 0:
        return 0.0
    lots = risk_usd / (sl_points * vpp)
    return round_lot(lots)

def synthetic_sl_points(symbol: str) -> float:
    info = get_symbol_info(symbol)
    min_stop = info.stops_level or 0
    atr_pts = get_atr_points(symbol, period=ATR_PERIOD)
    syn = max(int(TRAIL_ATR_MULT * atr_pts), min_stop)
    return max(syn, min_stop)

def compute_allowed_risk_usd() -> float:
    rem_daily = remaining_daily_loss()
    rem_overall = remaining_overall_loss()
    base = BASE_RISK_PCT * INITIAL_BALANCE
    cap_mdl = MDL_FRACTION_PER_TRADE * rem_daily
    cap_ovr = OVR_FRACTION_PER_TRADE * rem_overall
    return max(0.0, min(base, cap_mdl, cap_ovr))

def sum_open_risk_usd() -> float:
    """
    Approx worst-case: sum (lots * sl_points * value_per_point) for each open position
    where SL exists; if no SL, assume synthetic_sl_points().
    """
    total = 0.0
    for pos in mt5.positions_get():
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

# -----------------------
# ENTRY & MANAGEMENT
# -----------------------

def prepare_copy_trade(master_symbol: str, master_type: str, master_entry: float, master_sl: float | None, master_tp: float | None):
    daily_reset_if_needed()

    if master_symbol not in SYMBOL_ALLOW:
        log.info(f"[skip] {master_symbol} not in allowlist")
        return None

    server_now = now_cest()
    if not in_session_window(server_now):
        log.info(f"[skip] outside session window {server_now}")
        return None

    eq = get_equity()
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

    # Can we add this risk into open-risk budget?
    vpp = value_per_point(master_symbol)
    add_risk = lots * sl_points * vpp
    if not can_add_more_risk(add_risk):
        # try shrink lot to fit
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

    # Build order request (do not send yet)
    if master_type.lower() == "buy":
        order_type = mt5.ORDER_TYPE_BUY
        sl_price = master_entry - sl_points * pnt
        tp_price = master_tp if (master_tp and master_tp > 0) else 0.0
    else:
        order_type = mt5.ORDER_TYPE_SELL
        sl_price = master_entry + sl_points * pnt
        tp_price = master_tp if (master_tp and master_tp > 0) else 0.0

    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": master_symbol,
        "type": order_type,
        "volume": lots,
        "price": mt5.symbol_info_tick(master_symbol).ask if order_type == mt5.ORDER_TYPE_BUY else mt5.symbol_info_tick(master_symbol).bid,
        "sl": round(sl_price, info.digits),
        "tp": round(tp_price, info.digits) if tp_price else 0.0,
        "deviation": 10,
        "magic": 909999,
        "comment": "B_COPY_RISK",
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    log.info(f"[copy-ready] {master_type.upper()} {master_symbol} lots={lots} risk≈${add_risk:.2f} sl_pts={sl_points:.0f}")
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
    if pos.sl is None or pos.sl == 0:
        sl_pts = synthetic_sl_points(pos.symbol)
    else:
        sl_pts = abs((pos.price_open - pos.sl) / point)
    if sl_pts <= 0:
        return 0.0
    # reward so far (unrealized) in points
    tick = mt5.symbol_info_tick(pos.symbol)
    cur = tick.bid if pos.type == mt5.POSITION_TYPE_BUY else tick.ask
    r_pts = (cur - pos.price_open)/point if pos.type == mt5.POSITION_TYPE_BUY else (pos.price_open - cur)/point
    return float(r_pts / sl_pts)

def move_to_be_plus(pos):
    info = get_symbol_info(pos.symbol)
    spread = abs(mt5.symbol_info_tick(pos.symbol).ask - mt5.symbol_info_tick(pos.symbol).bid)
    if pos.type == mt5.POSITION_TYPE_BUY:
        new_sl = min(pos.price_open + spread, mt5.symbol_info_tick(pos.symbol).bid)  # be + spread, not above current bid
    else:
        new_sl = max(pos.price_open - spread, mt5.symbol_info_tick(pos.symbol).ask)
    req = {
        "action": mt5.TRADE_ACTION_SLTP,
        "symbol": pos.symbol,
        "position": pos.ticket,
        "sl": round(new_sl, info.digits),
        "tp": pos.tp,
    }
    mt5.order_send(req)

def partial_close(pos, fraction=0.5):
    vol = max(MIN_LOT, round_lot(pos.volume * fraction))
    if vol <= 0 or vol >= pos.volume: 
        return
    price = mt5.symbol_info_tick(pos.symbol).bid if pos.type == mt5.POSITION_TYPE_BUY else mt5.symbol_info_tick(pos.symbol).ask
    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "position": pos.ticket,
        "symbol": pos.symbol,
        "volume": vol,
        "type": mt5.ORDER_TYPE_SELL if pos.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY,
        "price": price,
        "deviation": 10,
        "magic": 909999,
        "comment": "B_COPY_PARTIAL",
    }
    mt5.order_send(req)

def trail_atr(pos):
    info = get_symbol_info(pos.symbol)
    p = mt5.symbol_info_tick(pos.symbol)
    last = p.bid if pos.type == mt5.POSITION_TYPE_BUY else p.ask
    atr_pts = get_atr_points(pos.symbol, period=ATR_PERIOD)
    if atr_pts <= 0:
        return
    point = info.point
    offset = atr_pts * TRAIL_ATR_MULT * point
    new_sl = (last - offset) if pos.type == mt5.POSITION_TYPE_BUY else (last + offset)
    if pos.sl and ((pos.type == mt5.POSITION_TYPE_BUY and new_sl <= pos.sl) or (pos.type == mt5.POSITION_TYPE_SELL and new_sl >= pos.sl)):
        return
    req = {"action": mt5.TRADE_ACTION_SLTP, "symbol": pos.symbol, "position": pos.ticket, "sl": round(new_sl, info.digits), "tp": pos.tp}
    mt5.order_send(req)

def manage_positions_loop():
    # call this every ~5–15 seconds in your main loop
    for pos in mt5.positions_get():
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

    # Example: when master sends an event, call prepare_copy_trade(...)
    # master_event = {"symbol":"XAUUSD","type":"buy","entry":3330.50,"sl":3324.50,"tp":0.0}
    # req = prepare_copy_trade(master_event["symbol"], master_event["type"], master_event["entry"], master_event["sl"], master_event["tp"])
    # if req:
    #     send_order(req)
    #
    # while True:
    #     daily_reset_if_needed()
    #     manage_positions_loop()
    #     time.sleep(5)
