"""
reverse_mirror_simple.py — v1.4
Same-account reverse/mirror for manual phone trades (MT5 + Python)

Key behavior:
- REVERSE (default): BUY→SELL, SELL→BUY
- If manual had SL:    TP = old SL  (SL = old TP if it existed)
- If manual had NO SL: capped fallback SL (70 'pips' by default) and TP = 1R
- Replacement lots are ALWAYS computed by risk % of **equity** and capped by **remaining daily drawdown**.
- Safer switch default: OPEN_THEN_CLOSE (hedge first)
- Phone pause: keep a tiny position open on CONTROL_SYMBOL (e.g., EURUSD) to pause; close it to resume.
"""

import os, time, csv, math, datetime as dt
from pathlib import Path
from typing import Optional, Tuple, List

from dotenv import load_dotenv
import MetaTrader5 as mt5

# =========================
# CONFIG
# =========================
load_dotenv()

MAGIC                 = int(os.getenv("MAGIC", "909912"))
POLL_SECONDS          = float(os.getenv("POLL_SECONDS", "0.5"))

MODE                  = os.getenv("MODE", "REVERSE").upper()                       # REVERSE | MIRROR
REPLACE_SEQUENCE      = os.getenv("REPLACE_SEQUENCE", "OPEN_THEN_CLOSE").upper()  # OPEN_THEN_CLOSE | CLOSE_THEN_OPEN

SYMBOL_ALLOWLIST      = [s.strip() for s in os.getenv("SYMBOL_ALLOWLIST","XAUUSD,US100,US100.cash,USTECm").split(",") if s.strip()]
RISK_PCT              = float(os.getenv("RISK_PCT", "1.0"))                        # base per-trade risk (% of equity)
MAX_FALLBACK_PIPS     = int(os.getenv("MAX_FALLBACK_PIPS", "70"))                  # used only if manual had no SL

# Daily risk guard (your FTMO rules)
DAILY_DD_LIMIT_PCT    = float(os.getenv("DAILY_DD_LIMIT_PCT", "5.0"))              # e.g., 5%
CAP_TO_DAILY          = os.getenv("CAP_TO_DAILY","YES").upper()=="YES"             # cap per-trade risk to remaining daily budget

COMMENT_PREFIX        = os.getenv("COMMENT_PREFIX", "REV_MIRROR")
LOG_DIR               = Path(os.getenv("LOG_DIR", "./logs"))
DRY_RUN               = os.getenv("DRY_RUN", "NO").upper() == "YES"

CONTROL_SYMBOL        = os.getenv("CONTROL_SYMBOL", "EURUSD")
PAUSE_WHEN_CONTROL_PRESENT = os.getenv("PAUSE_WHEN_CONTROL_PRESENT", "YES").upper() == "YES"

# Optional creds (if terminal not already logged in)
LOGIN     = os.getenv("MT5_LOGIN")
PASSWORD  = os.getenv("MT5_PASSWORD")
SERVER    = os.getenv("MT5_SERVER")

# =========================
# Helpers
# =========================
def now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def today_range() -> Tuple[dt.datetime, dt.datetime]:
    t0 = dt.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    t1 = dt.datetime.now()
    return t0, t1

def ensure_mt5():
    if not mt5.initialize():
        if LOGIN and PASSWORD and SERVER:
            if not mt5.initialize():
                raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
            if not mt5.login(int(LOGIN), PASSWORD, SERVER):
                raise RuntimeError(f"MT5 login failed: {mt5.last_error()}")
        else:
            raise RuntimeError(f"MT5 initialize failed (no creds): {mt5.last_error()}")

def digits_round(symbol: str, price: float) -> float:
    info = mt5.symbol_info(symbol)
    if not info: return price
    try:
        d = int(info.digits)
    except Exception:
        return price
    return float(round(price, d))

def pip_points(symbol: str) -> float:
    info = mt5.symbol_info(symbol)
    if not info: return 0.0
    return 10.0 * info.point  # safe generic default

def allowed_symbol(symbol: str) -> bool:
    s = symbol.upper()
    for a in SYMBOL_ALLOWLIST:
        au = a.upper()
        if s == au or s.startswith(au) or au in s:
            return True
    return False

def order_filling(info) -> Optional[int]:
    fm = getattr(info, "filling_mode", None)
    if isinstance(fm, int) and fm in (mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_RETURN):
        return fm
    return None

def clamp_to_stops(symbol: str, order_type: int, sl: Optional[float], tp: Optional[float]) -> Tuple[Optional[float], Optional[float]]:
    info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if not info or not tick:
        return sl, tp

    stops_pts  = (getattr(info, "trade_stops_level", 0) or 0) * info.point
    freeze_pts = (getattr(info, "freeze_level",      0) or 0) * info.point
    min_dist_points = max(stops_pts, freeze_pts)

    if min_dist_points > 0:
        bid, ask = tick.bid, tick.ask
        if order_type == mt5.ORDER_TYPE_BUY:
            if sl is not None: sl = min(sl, bid - min_dist_points)
            if tp is not None: tp = max(tp, ask + min_dist_points)
        else:  # SELL
            if sl is not None: sl = max(sl, ask + min_dist_points)
            if tp is not None: tp = min(tp, bid - min_dist_points)

    if sl is not None: sl = digits_round(symbol, sl)
    if tp is not None: tp = digits_round(symbol, tp)
    return sl, tp

def todays_realized_pnl() -> float:
    """Sum realized PnL (including commission & swap) for today."""
    t0, t1 = today_range()
    deals = mt5.history_deals_get(t0, t1)
    total = 0.0
    if deals:
        for d in deals:
            total += float(getattr(d,"profit",0.0) or 0.0)
            total += float(getattr(d,"commission",0.0) or 0.0)
            total += float(getattr(d,"swap",0.0) or 0.0)
    return total

def todays_floating_pnl_opened_today() -> float:
    """Floating PnL for positions OPENED today (to estimate today's PnL)."""
    t0, _ = today_range()
    pos = mt5.positions_get()
    if not pos: return 0.0
    total = 0.0
    for p in pos:
        opentime = dt.datetime.fromtimestamp(getattr(p,"time",0))
        if opentime >= t0:
            total += float(getattr(p,"profit",0.0) or 0.0)
    return total

def remaining_daily_risk_pct_equity() -> float:
    """
    Compute remaining daily loss budget as a % of current EQUITY.
    start_equity_today ≈ equity - realized_today - floating_today_opened_today
    remaining_amount   = start_equity_today * (DAILY_DD_LIMIT_PCT/100) - max(0, - (realized_today + floating_today))
    remaining_pct_of_equity = remaining_amount / equity * 100
    """
    acc = mt5.account_info()
    if not acc: return RISK_PCT
    equity = float(getattr(acc,"equity", acc.balance))
    realized_today = todays_realized_pnl()
    floating_today = todays_floating_pnl_opened_today()
    start_eq_today = equity - realized_today - floating_today
    daily_cap_amt  = max(0.0, start_eq_today * (DAILY_DD_LIMIT_PCT / 100.0))
    total_today_pnl = realized_today + floating_today
    consumed = max(0.0, -total_today_pnl)  # only count losses
    remaining_amt = max(0.0, daily_cap_amt - consumed)
    if equity <= 0: 
        return 0.0
    return (remaining_amt / equity) * 100.0

def effective_risk_pct() -> float:
    if not CAP_TO_DAILY:
        return RISK_PCT
    rem = remaining_daily_risk_pct_equity()
    eff = max(0.0, min(RISK_PCT, rem))
    return eff

def risk_lots(symbol: str, order_type: int, sl_price: float, base_risk_pct: float) -> float:
    """
    Compute lots from risk % of equity vs SL distance.
    Includes fallback if trade_tick_value/size are missing: use point/contract approximations.
    """
    acc  = mt5.account_info()
    info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if not acc or not info or not tick:
        return 0.0

    equity = float(getattr(acc,"equity", acc.balance))
    # cap by remaining daily budget if enabled
    risk_pct = effective_risk_pct()

    risk_money = equity * (risk_pct / 100.0)

    price   = tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid
    sl_dist = abs(price - sl_price)
    if sl_dist <= 0:
        return 0.0

    tick_val = float(getattr(info, "trade_tick_value", 0.0) or 0.0)
    tick_sz  = float(getattr(info, "trade_tick_size",  0.0) or 0.0)

    # Fallbacks if broker fields are zeros
    if tick_sz <= 0:
        tick_sz = float(getattr(info,"point", 0.0) or 0.0)
    if tick_val <= 0 and tick_sz > 0:
        # rough fallback using contract size & point (assumes quote currency ~= account currency)
        contract = float(getattr(info,"trade_contract_size", 0.0) or 0.0)
        tick_val = contract * tick_sz if contract > 0 else 0.0

    if tick_val <= 0 or tick_sz <= 0:
        return 0.0

    money_per_lot = (sl_dist / tick_sz) * tick_val
    if money_per_lot <= 0:
        return 0.0

    lots_raw = risk_money / money_per_lot

    # clamp & quantize to broker step
    minlot = float(getattr(info, "volume_min",  0.01) or 0.01)
    maxlot = float(getattr(info, "volume_max",  100.0) or 100.0)
    step   = float(getattr(info, "volume_step", 0.01) or 0.01)

    lots = max(minlot, min(maxlot, lots_raw))
    # quantize to step
    decimals = max(0, len(str(step).split(".")[1]) if "." in str(step) else 0)
    lots = math.floor(lots / step) * step
    return round(lots, decimals)

def deal(symbol: str, order_type: int, lots: float, sl: Optional[float], tp: Optional[float], comment: str, position_ticket: Optional[int]=None) -> bool:
    info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if not info or not tick:
        print(f"[{now()}] ERROR: symbol/tick unavailable for {symbol}")
        return False
    if not info.visible:
        mt5.symbol_select(symbol, True)

    price = tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid
    sl, tp = clamp_to_stops(symbol, order_type, sl, tp)

    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(lots),
        "type": order_type,
        "price": digits_round(symbol, price),
        "sl": sl if sl else 0.0,
        "tp": tp if tp else 0.0,
        "deviation": 50,
        "magic": MAGIC,
        "comment": comment,
        "type_time": mt5.ORDER_TIME_GTC,
    }
    fm = order_filling(info)
    if fm is not None:
        req["type_filling"] = fm

    if DRY_RUN:
        print(f"[{now()}] DRY-RUN: order_send {req}")
        return True

    res = mt5.order_send(req)
    ok = res is not None and res.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED)
    side = "BUY" if order_type == mt5.ORDER_TYPE_BUY else "SELL"
    print(f"[{now()}] SEND {symbol} {side} lots={lots} sl={sl} tp={tp} -> ret={getattr(res,'retcode',None)}")
    if not ok:
        print(f"   details: {res}")
    return ok

def close_position(pos) -> bool:
    symbol = pos.symbol
    vol    = pos.volume
    order_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
    return deal(symbol, order_type, vol, None, None, f"{COMMENT_PREFIX} close_orig", position_ticket=pos.ticket)

def open_replacement(pos, reverse: bool, use_swap_tp_sl: bool=True, max_fallback_pips: int=70) -> bool:
    symbol = pos.symbol
    info   = mt5.symbol_info(symbol)
    tick   = mt5.symbol_info_tick(symbol)
    if not info or not tick:
        print(f"[{now()}] ERROR: missing symbol info/tick for {symbol}")
        return False

    # Direction
    new_type = (
        mt5.ORDER_TYPE_SELL if pos.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
    ) if reverse else (
        mt5.ORDER_TYPE_BUY if pos.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_SELL
    )

    # Build SL/TP from your rules
    newSL, newTP = None, None
    if use_swap_tp_sl and pos.sl and pos.sl > 0:
        newTP = float(pos.sl)
        if pos.tp and pos.tp > 0:
            newSL = float(pos.tp)

    # If still no SL, create capped fallback SL
    if newSL is None:
        pips = pip_points(symbol)
        max_dist = max_fallback_pips * pips if pips > 0 else 0.0
        price = tick.ask if new_type == mt5.ORDER_TYPE_BUY else tick.bid
        newSL = price - max_dist if new_type == mt5.ORDER_TYPE_BUY else price + max_dist

    # If still no TP, set 1R TP
    if newTP is None:
        price = tick.ask if new_type == mt5.ORDER_TYPE_BUY else tick.bid
        if new_type == mt5.ORDER_TYPE_BUY:
            newTP = price + (price - newSL)
        else:
            newTP = price - (newSL - price)

    # === ALWAYS compute lots by risk vs newSL and DAILY cap ===
    lots_final = risk_lots(symbol, new_type, newSL, RISK_PCT)
    if lots_final <= 0:
        lots_final = pos.volume  # last-resort fallback

    reverse_str = "YES" if reverse else "NO"
    comment = f"{COMMENT_PREFIX} inv={reverse_str}"

    if REPLACE_SEQUENCE == "CLOSE_THEN_OPEN":
        if not close_position(pos):
            print(f"[{now()}] ERROR: close original failed {pos.ticket}")
            return False
        ok = deal(symbol, new_type, lots_final, newSL, newTP, comment)
        return ok
    else:
        ok = deal(symbol, new_type, lots_final, newSL, newTP, comment)
        if not ok:
            return False
        if not close_position(pos):
            print(f"[{now()}] WARN: hedged but close original failed {pos.ticket}. You’re flat overall; close manually if needed.")
        return True

def journal_writer():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"reverse_journal_{dt.datetime.now():%Y%m%d}.csv"
    new = not path.exists()
    f = open(path, "a", newline="", encoding="utf-8")
    w = csv.writer(f)
    if new:
        w.writerow(["ts","event","ticket","symbol","type","vol","sl","tp","comment"])
    return f, w, path

def control_pause_active() -> bool:
    if not PAUSE_WHEN_CONTROL_PRESENT or not CONTROL_SYMBOL:
        return False
    try:
        positions = mt5.positions_get(symbol=CONTROL_SYMBOL)
        return bool(positions)
    except Exception:
        return False

# =========================
# Main
# =========================
def main():
    ensure_mt5()
    print(f"[{now()}] ReverseMirror v1.4 running. MODE={MODE} SEQ={REPLACE_SEQUENCE}")
    print(f"[{now()}] ALLOW={SYMBOL_ALLOWLIST}  RISK_PCT={RISK_PCT}%  DAILY_CAP={DAILY_DD_LIMIT_PCT}%  CAP_TO_DAILY={CAP_TO_DAILY}")
    if PAUSE_WHEN_CONTROL_PRESENT:
        print(f"[{now()}] Phone toggle: OPEN a tiny {CONTROL_SYMBOL} position to PAUSE. Close it to RESUME.")

    seen = set()
    f, w, path = journal_writer()
    last_pause_notice = 0.0

    try:
        while True:
            # Pause from phone
            if control_pause_active():
                t = time.time()
                if t - last_pause_notice > 10:
                    print(f"[{now()}] PAUSED (control position on {CONTROL_SYMBOL} present). Close it to resume.")
                    last_pause_notice = t
                time.sleep(0.5)
                continue

            positions = mt5.positions_get()
            if positions:
                for p in positions:
                    if p.ticket in seen:
                        continue
                    if p.magic == MAGIC or (p.comment or "").startswith(COMMENT_PREFIX):
                        continue
                    if not allowed_symbol(p.symbol):
                        continue

                    side = "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL"
                    print(f"[{now()}] DETECTED manual {side} {p.symbol} vol={p.volume} sl={p.sl} tp={p.tp} ticket={p.ticket}")

                    reverse = (MODE == "REVERSE")
                    try:
                        ok = open_replacement(p, reverse=reverse, use_swap_tp_sl=True, max_fallback_pips=MAX_FALLBACK_PIPS)
                    except Exception as e:
                        print(f"[{now()}] ERROR during replace for ticket {p.ticket}: {e}")
                        ok = False

                    seen.add(p.ticket)
                    w.writerow([now(), "replace", p.ticket, p.symbol, side, p.volume, p.sl, p.tp, p.comment])
                    f.flush()

            time.sleep(POLL_SECONDS)
    finally:
        f.close()
        print(f"[{now()}] Journal saved: {path}")
        mt5.shutdown()

if __name__ == "__main__":
    main()
