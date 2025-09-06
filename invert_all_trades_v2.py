# one_day_sim_no_offset.py
import pandas as pd, numpy as np, argparse, math
np.random.seed(7)

def detect_fractals(df, n=2):
    hi, lo = df['high'].values, df['low'].values
    bull = np.zeros(len(df), dtype=bool); bear = np.zeros(len(df), dtype=bool)
    for i in range(n, len(df)-n):
        if hi[i] > hi[i-1] and hi[i] > hi[i-2] and hi[i] > hi[i+1] and hi[i] > hi[i+2]: bear[i] = True
        if lo[i] < lo[i-1] and lo[i] < lo[i-2] and lo[i] < lo[i+1] and lo[i] < lo[i+2]: bull[i] = True
    return bull, bear

def round_step(x, step): return math.floor(x/step)*step

def run_day(df, date_str, tick_size=0.01, tick_val_1lot=1.0,
            tp_ticks_rng=(12,20), sl_ticks_rng=(10,18),
            tp_cash_rng=(10.0,50.0), sl_cash=40.0,
            use_trail=True, be_trigger=12, be_buffer=1, trail_gap=10, trail_step=2,
            max_concurrent=5, order_expiry_bars=120, market_on_touch=True, fees_per_lot=0.0):
    d = df.copy()
    d['time'] = pd.to_datetime(d['time']); d.sort_values('time', inplace=True)
    d['date'] = d['time'].dt.date
    d = d[d['date']==pd.to_datetime(date_str).date()].copy()
    if d.empty: raise SystemExit(f"No bars for {date_str}.")
    bull, bear = detect_fractals(d)
    d['bull_f'], d['bear_f'] = bull, bear

    orders, positions, trades = [], [], []

    for i in range(len(d)):
        r = d.iloc[i]; hi, lo = r['high'], r['low']
        # new confirmed fractals (no offset)
        if r['bear_f']:
            entry = r['high']  # SELL limit @ upper fractal price
            orders.append({'i':i,'exp':i+order_expiry_bars,'dir':'SHORT','entry':entry})
        if r['bull_f']:
            entry = r['low']   # BUY  limit @ lower fractal price
            orders.append({'i':i,'exp':i+order_expiry_bars,'dir':'LONG','entry':entry})

        # drop expired
        orders = [o for o in orders if o['exp']>i]

        # trigger logic (market-on-touch or limit-at-level behave the same with OHLC)
        cap = max(0, max_concurrent - len(positions))
        if cap and orders:
            hit = [j for j,o in enumerate(orders) if lo<=o['entry']<=hi]
            for j in sorted(hit[-cap:], reverse=True):
                o = orders.pop(j)
                tp_ticks = int(np.random.randint(tp_ticks_rng[0], tp_ticks_rng[1]+1))
                sl_ticks = int(np.random.randint(sl_ticks_rng[0], sl_ticks_rng[1]+1))
                tp_cash  = float(np.random.uniform(tp_cash_rng[0], tp_cash_rng[1]))
                lots = round_step(min(tp_cash/(tp_ticks*tick_val_1lot), sl_cash/(sl_ticks*tick_val_1lot)), 0.01)
                lots = max(0.01, min(lots, 10.0))
                if o['dir']=='LONG':
                    tp = o['entry'] + tp_ticks*tick_size; sl = o['entry'] - sl_ticks*tick_size
                else:
                    tp = o['entry'] - tp_ticks*tick_size; sl = o['entry'] + sl_ticks*tick_size
                positions.append({'dir':o['dir'],'entry':o['entry'],'tp':tp,'sl':sl,
                                  'tp_ticks':tp_ticks,'sl_ticks':sl_ticks,'lots':lots,
                                  'be':False,'best':o['entry'],'open_time':r['time'],'open_i':i})

        # trailing
        if use_trail and positions:
            for p in positions:
                p['best'] = max(p['best'], hi) if p['dir']=='LONG' else min(p['best'], lo)
                if not p['be']:
                    if (p['dir']=='LONG' and hi>=p['entry'] + be_trigger*tick_size) or \
                       (p['dir']=='SHORT' and lo<=p['entry'] - be_trigger*tick_size):
                        p['be']=True
                        p['sl'] = max(p['sl'], p['entry']+be_buffer*tick_size) if p['dir']=='LONG' else \
                                  min(p['sl'], p['entry']-be_buffer*tick_size)
                if p['be']:
                    if p['dir']=='LONG':
                        target = p['best'] - trail_gap*tick_size
                        steps = math.floor((target - p['sl'])/(trail_step*tick_size))
                        if steps>0: p['sl'] += steps*trail_step*tick_size
                    else:
                        target = p['best'] + trail_gap*tick_size
                        steps = math.floor((p['sl'] - target)/(trail_step*tick_size))
                        if steps>0: p['sl'] -= steps*trail_step*tick_size

        # exits
        keep=[]
        for p in positions:
            hit=None
            if p['dir']=='LONG':
                if lo<=p['sl']<=hi: hit=('SL', -p['sl_ticks']*tick_val_1lot*p['lots'])
                elif lo<=p['tp']<=hi: hit=('TP', p['tp_ticks']*tick_val_1lot*p['lots'])
            else:
                if lo<=p['tp']<=hi: hit=('TP', p['tp_ticks']*tick_val_1lot*p['lots'])
                elif lo<=p['sl']<=hi: hit=('SL', -p['sl_ticks']*tick_val_1lot*p['lots'])
            if hit:
                side, gross = hit
                comm = fees_per_lot*p['lots']
                trades.append({**p,'close_time':r['time'],'outcome':side,
                               'pnl_cash_gross':gross,'commission':comm,'pnl_cash_net':gross-comm})
            else:
                keep.append(p)
        positions = keep

    t = pd.DataFrame(trades)
    if t.empty: 
        print("No trades for the day."); return
    wins=t[t.pnl_cash_net>0]; losses=t[t.pnl_cash_net<0]
    gp=wins.pnl_cash_net.sum(); gl=-losses.pnl_cash_net.sum(); pf=(gp/gl) if gl>0 else float('inf')
    print(f"Trades: {len(t)}, WinRate: {len(wins)/len(t):.2%}, PF: {pf:.3f}, Net: £{t.pnl_cash_net.sum():.2f}")
    out = f"one_day_trades_{date_str}.csv"; t.to_csv(out, index=False); print("Saved:", out)

if __name__=="__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--date", required=True)
    ap.add_argument("--market", action="store_true", help="(kept for parity; OHLC touch = market/limit at level)")
    args = ap.parse_args()
    df = pd.read_csv(args.csv)
    assert set(['time','open','high','low','close']).issubset(df.columns)
    run_day(df, args.date)
