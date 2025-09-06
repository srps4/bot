import os, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List
import pandas as pd
import pytz
import MetaTrader5 as mt5
from dotenv import load_dotenv

# ---------- env ----------
load_dotenv()
SYMBOLS         = [s.strip() for s in os.getenv("SYMBOLS","XAUUSD").split(",") if s.strip()]
TIMEFRAMES      = [t.strip().upper() for t in os.getenv("TIMEFRAMES","M1,M15").split(",")]
BARS_PER_PULL   = int(os.getenv("BARS_PER_PULL","3000"))
TICKS_LOOKBACK  = int(os.getenv("TICKS_LOOKBACK_MIN","180"))
POLL_SECONDS    = float(os.getenv("POLL_SECONDS","60"))
OUT_DIR         = Path(os.getenv("OUT_DIR","./data"))
MAGIC           = int(os.getenv("MAGIC","990015"))

OUT_DIR.mkdir(parents=True, exist_ok=True)

# ---------- helpers ----------
MT5_TF = {
    "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4,
    "D1": mt5.TIMEFRAME_D1
}

def init_mt5():
    if not mt5.initialize():
        raise RuntimeError(f"MT5 init failed: {mt5.last_error()}")
    acc = mt5.account_info()
    if not acc:
        raise RuntimeError(f"Not logged in: {mt5.last_error()}")
    print(f"[init] connected: {acc.login} | broker={acc.server} | balance={acc.balance}")

def day_stamp_utc(ts: pd.Timestamp) -> str:
    # If timestamp has no timezone → localize, else convert
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts.strftime("%Y-%m-%d")

def out_bars_path(source: str, symbol: str, tf: str, ts_utc: pd.Timestamp) -> Path:
    day = day_stamp_utc(ts_utc)
    return OUT_DIR / f"bars_{source}_{symbol}_{tf}_UTC_{day}.csv"

def out_ticks_path(source: str, symbol: str, ts_utc: pd.Timestamp) -> Path:
    day = day_stamp_utc(ts_utc)
    return OUT_DIR / f"ticks_{source}_{symbol}_UTC_{day}.csv"

def ensure_header(path: Path, cols: List[str]):
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(columns=cols).to_csv(path, index=False)

# ---- Killzone tagging (London time) ----
LONDON = pytz.timezone("Europe/London")
KZ_RULES = [
    ("Asian",    2,  6),
    ("Ldn_open", 8, 11),
    ("NY",       13, 15),
    ("Ldn_close",16, 18),
]
def killzone_tag(ts) -> str:
    # accept anything (naive/aware), normalize to UTC, then convert to London
    ts = pd.Timestamp(ts)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    ts_ldn = ts.tz_convert(LONDON)
    h = ts_ldn.hour
    for name, h1, h2 in KZ_RULES:
        if (h >= h1) and (h < h2):
            return name
    return "None"

def append_csv(path: Path, rows: pd.DataFrame):
    if rows.empty:
        return
    rows.to_csv(path, mode="a", header=not path.exists(), index=False)

def fetch_bars(symbol: str, tf: str) -> pd.DataFrame:
    rates = mt5.copy_rates_from_pos(symbol, MT5_TF[tf], 0, BARS_PER_PULL)
    if rates is None:
        print(f"[warn] copy_rates None for {symbol} {tf}: {mt5.last_error()}")
        return pd.DataFrame()
    df = pd.DataFrame(rates)
    if df.empty:
        return df
    df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.rename(columns={"tick_volume":"volume"})
    df["symbol"] = symbol
    df["timeframe"] = tf
    df["source"] = "MT5"
    df["killzone"] = df["timestamp"].apply(killzone_tag)
    keep = ["timestamp","symbol","open","high","low","close","volume","timeframe","source","killzone"]
    return df[keep].sort_values("timestamp").drop_duplicates(subset=["timestamp","symbol","timeframe"])

def fetch_ticks(symbol: str) -> pd.DataFrame:
    now_utc = datetime.now(timezone.utc)
    start   = now_utc - timedelta(minutes=TICKS_LOOKBACK)
    ticks = mt5.copy_ticks_range(symbol, start, now_utc, mt5.COPY_TICKS_ALL)
    if ticks is None:
        print(f"[warn] copy_ticks None for {symbol}: {mt5.last_error()}")
        return pd.DataFrame()
    df = pd.DataFrame(ticks)
    if df.empty:
        return df
    df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
    if "volume_real" in df.columns:
        df["volume"] = df["volume_real"]
    df["symbol"] = symbol
    df["source"] = "MT5"
    keep = ["timestamp","symbol","bid","ask","last","volume","source"]
    for col in keep:
        if col not in df.columns:
            df[col] = None
    return df[keep].sort_values("timestamp").drop_duplicates(subset=["timestamp","symbol"])

def loop():
    init_mt5()
    while True:
        try:
            for sym in SYMBOLS:
                for tf in TIMEFRAMES:
                    bars = fetch_bars(sym, tf)
                    if not bars.empty:
                        p = out_bars_path("MT5", sym, tf, bars["timestamp"].iloc[-1].tz_convert("UTC"))
                        ensure_header(p, ["timestamp","symbol","open","high","low","close","volume","timeframe","source","killzone"])
                        append_csv(p, bars)
                        print(f"[bars] {sym} {tf}: wrote {len(bars)} → {p.name}")

                ticks = fetch_ticks(sym)
                if not ticks.empty:
                    p2 = out_ticks_path("MT5", sym, ticks["timestamp"].iloc[-1].tz_convert("UTC"))
                    ensure_header(p2, ["timestamp","symbol","bid","ask","last","volume","source"])
                    append_csv(p2, ticks)
                    print(f"[ticks] {sym}: wrote {len(ticks)} → {p2.name}")

        except Exception as e:
            print(f"[error] {e}")
            try:
                mt5.shutdown()
            except:
                pass
            time.sleep(3)
            try:
                init_mt5()
            except Exception as e2:
                print(f"[reinit-failed] {e2}")

        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    loop()
