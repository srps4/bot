import os, json, socket, threading, time
from datetime import datetime, timedelta
import MetaTrader5 as mt5

from _env import load_env_from_dotenv, get_path
from accountB_copy_risk_engine import (
    prepare_copy_trade, send_order, manage_positions_loop, daily_reset_if_needed
)

# ML bandit (optional, safe to include if present)
try:
    from ml.ts_agent import choose_and_write_active, update_from_trade
except Exception:
    def choose_and_write_active(): return {"name": "na"}
    def update_from_trade(*args, **kwargs): pass

load_env_from_dotenv()
MT5_PATH    = get_path("MT5_PATH", r"C:\deadzone\mt5_trade_copier-ruman\mt5_B\terminal64.exe")
LISTEN_ADDR = (os.getenv("BRIDGE_HOST","127.0.0.1"), int(os.getenv("BRIDGE_PORT","5555")))

COPY_MODE   = os.getenv("COPY_MODE","REVERSE").upper()
COMMENT_MARK= os.getenv("COMMENT_PREFIX","B_COPY_RISK")

MASTER2FOLLOW: dict[int,int] = {}
ACTIVE_ARM: dict | None = None

def initialize():
    ok = False
    # Attach to a running terminal if path not given
    if MT5_PATH and str(MT5_PATH).lower().endswith(".exe"):
        ok = mt5.initialize(path=MT5_PATH)
    else:
        ok = mt5.initialize()
    if not ok:
        raise RuntimeError(f"MT5 B init failed: {mt5.last_error()}")

def select_arm_for_today():
    global ACTIVE_ARM
    ACTIVE_ARM = choose_and_write_active()
    print(f"[ml] using arm: {ACTIVE_ARM.get('name','na')}")

def tcp_server():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(LISTEN_ADDR)
    srv.listen(1)
    print(f"[exec] listening on {LISTEN_ADDR} …")
    conn, _ = srv.accept()
    print("[exec] connected to watcher")
    buf = b""
    while True:
        chunk = conn.recv(65536)
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                handle_message(json.loads(line.decode()))
            except Exception as e:
                print("[exec] bad msg:", e)
    conn.close()

def handle_message(m: dict):
    e = m.get("event")
    if e == "OPEN":
        on_open(m)
    elif e == "MODIFY":
        on_modify(m)
    elif e == "CLOSE":
        on_close(m)

def on_open(m):
    req = prepare_copy_trade(
        master_symbol=m["symbol"],
        master_type=m["type"],
        master_entry=float(m.get("entry", 0.0)),
        master_sl=float(m.get("sl") or 0.0) or None,
        master_tp=float(m.get("tp") or 0.0) or None
    )
    if not req:
        print("[copy] skip: prepare_copy_trade returned None")
        return
    # tag with arm for learning
    arm_name = ACTIVE_ARM["name"] if ACTIVE_ARM else "na"
    # ensure follower comment contains watcher marker
    base_comment = req.get("comment") or COMMENT_MARK
    if COMMENT_MARK not in base_comment:
        base_comment = f"{COMMENT_MARK}|{base_comment}"
    req["comment"] = f"{base_comment}|arm={arm_name}"
    res = send_order(req)

    # link master->follower if order actually placed
    if getattr(res, "retcode", None) == mt5.TRADE_RETCODE_DONE:
        time.sleep(0.1)
        # REVERSE support: follower type is opposite if COPY_MODE=REVERSE
        master_type = m["type"].lower()
        follower_type = (mt5.POSITION_TYPE_SELL if master_type == "buy" else mt5.POSITION_TYPE_BUY) \
                        if COPY_MODE == "REVERSE" else \
                        (mt5.POSITION_TYPE_BUY if master_type == "buy" else mt5.POSITION_TYPE_SELL)
        for p in mt5.positions_get(symbol=req["symbol"]) or []:
            if p.type == follower_type and p.comment and COMMENT_MARK in p.comment:
                MASTER2FOLLOW[int(m["ticket"])] = p.ticket
                print(f"[copy] linked master {m['ticket']} -> follower {p.ticket}")
                break
    else:
        print(f"[copy] order_send ret={getattr(res,'retcode',None)} {getattr(res,'comment',None)} last_error={mt5.last_error()}")

def on_modify(m):
    follower_ticket = MASTER2FOLLOW.get(int(m["ticket"]))
    if not follower_ticket:
        return
    for p in mt5.positions_get() or []:
        if p.ticket == follower_ticket:
            mt5.order_send({
                "action": mt5.TRADE_ACTION_SLTP,
                "symbol": p.symbol,
                "position": p.ticket,
                "sl": float(m.get("sl") or 0.0),
                "tp": float(m.get("tp") or 0.0),
            })
            print(f"[copy] MODIFY applied to follower {p.ticket}")
            break

def on_close(m):
    follower_ticket = MASTER2FOLLOW.pop(int(m["ticket"]), None)
    if not follower_ticket:
        return
    pos = None
    for p in mt5.positions_get() or []:
        if p.ticket == follower_ticket:
            pos = p
            break
    if not pos:
        return
    # compute price unconditionally
    tick = mt5.symbol_info_tick(pos.symbol)
    price = tick.bid if pos.type == mt5.POSITION_TYPE_BUY else tick.ask
    arm_name = "na"
    if pos.comment and "arm=" in pos.comment:
        try:
            arm_name = pos.comment.split("arm=")[1].split("|")[0]
        except Exception:
            pass
    r = mt5.order_send({
        "action": mt5.TRADE_ACTION_DEAL,
        "position": pos.ticket,
        "symbol": pos.symbol,
        "volume": pos.volume,
        "type": (mt5.ORDER_TYPE_SELL if pos.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY),
        "price": price,
        "deviation": 10,
        "magic": 909999,
        "comment": pos.comment,
    })
    # realized pnl
    pnl = 0.0
    try:
        deals = mt5.history_deals_get(datetime.now()-timedelta(days=1), datetime.now())
        if deals:
            rel = [d for d in deals if getattr(d, "position_id", 0) == pos.ticket]
            pnl = float(sum(getattr(d, "profit", 0.0) for d in rel))
    except Exception:
        pass
    update_from_trade(arm_name, pnl)
    print(f"[copy] CLOSE follower {pos.ticket} pnl={pnl:.2f}")

def manager_loop():
    last_day = datetime.now().strftime("%Y-%m-%d")
    select_arm_for_today()
    while True:
        daily_reset_if_needed()
        d = datetime.now().strftime("%Y-%m-%d")
        if d != last_day:
            select_arm_for_today()
            last_day = d
        manage_positions_loop()
        time.sleep(0.5)

if __name__ == "__main__":
    initialize()
    t1 = threading.Thread(target=tcp_server, daemon=True)
    t2 = threading.Thread(target=manager_loop, daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
