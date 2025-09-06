# probe_mt5_master.py — prints account, last deals/orders, open positions
import time, MetaTrader5 as mt5
from datetime import datetime, timedelta

assert mt5.initialize(), f"MT5 init failed: {mt5.last_error()}"
acct = mt5.account_info(); assert acct, "No account_info()"
print(f"[acct] {acct.login} | {acct.company} | balance={acct.balance}")

since = datetime.now() - timedelta(hours=6)
deals = mt5.history_deals_get(since, datetime.now()) or []
orders= mt5.history_orders_get(since, datetime.now()) or []
pos   = mt5.positions_get() or []
print(f"[counts] deals={len(deals)} orders={len(orders)} open_positions={len(pos)}")

def row(d):
    return f"{d.time:>10} {d.symbol:<10} {d.type} vol={d.volume:.2f} price={d.price:.2f} magic={getattr(d,'magic',0)} comment={getattr(d,'comment','')}"
print("\n[last 10 deals]")
for d in deals[-10:]:
    print(row(d))
print("\n[open positions]")
for p in pos:
    print(f"{p.symbol:<10} {p.type} vol={p.volume:.2f} open@{p.price_open:.2f} magic={p.magic} comment={p.comment}")

mt5.shutdown()
