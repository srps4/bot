# MT5 ➜ MT5 Python Trade Copier (Risk-Based Lot Size)

This starter project copies market positions from a **master** MetaTrader 5 account to a **slave** MT5 account **on a different broker**, adjusting the **lot size** to match a configured **risk % of the slave account equity** (requires a stop-loss on the master trade to size properly).

> ⚠️ **Use at your own risk.** Trading involves substantial risk. Test on demo accounts first.

## What it does
- Polls the **master** account for open positions (buy/sell).  
- Opens matching positions on the **slave** account using **risk-based position sizing**: `lot = (risk_amount) / (stop_distance_points * value_per_point_per_lot)`.
- Mirrors SL/TP on the slave (optionally remapped symbols).
- Closes slave positions when the master closes.
- Persists a local mapping between master tickets and slave tickets (`state.json`).

## What it doesn’t do (yet)
- Pending orders, netting account reconciliation, partial closes, or advanced hedging logic.
- Symbol contract differences (margin requirements, commissions) beyond basic tick-value math.
- High-frequency / sub-second copying. (Polling is interval-based.)

## Prerequisites
- Windows with **MetaTrader 5 terminals installed** for both brokers (you can use different installations/paths).
- Python 3.9+ and the official [`MetaTrader5`](https://pypi.org/project/MetaTrader5/) package.
- Both accounts must allow algorithmic trading and your terminal must be logged in at least once.

## Install
```bash
pip install -r requirements.txt
```

## Configure
Copy `config.example.json` to `config.json` and fill in your credentials/paths:

```json
{ 
  "master": {
    "login": 12345678,
    "password": "MASTER_PASSWORD",
    "server": "MasterBroker-Server",
    "path": "C:/Program Files/Master MetaTrader 5/terminal64.exe"
  },
  "slave": {
    "login": 87654321,
    "password": "SLAVE_PASSWORD",
    "server": "SlaveBroker-Server",
    "path": "C:/Program Files/Slave MetaTrader 5/terminal64.exe"
  },
  "risk": {
    "risk_percent": 1.0,
    "max_slippage_points": 50,
    "min_lot": 0.01,
    "lot_step": 0.01,
    "max_lot": 50.0,
    "skip_if_no_sl": true,
    "default_sl_points": 1000
  },
  "symbols_map": {
    "XAUUSD": "XAUUSD.a",
    "GER40": "DE40"
  },
  "copy": {
    "copy_open": true,
    "copy_close": true,
    "poll_interval_sec": 0.5,
    "sync_existing_positions_on_start": true,
    "magic": 333777,
    "comment_prefix": "copied:"
  }
}
```

**Notes:**
- If your master trade has **no SL**, the copier **skips** the trade by default (`skip_if_no_sl: true`). You can set a `default_sl_points` fallback, but that undermines true risk-based sizing—use with caution.
- Ensure symbol names are correctly remapped between brokers in `symbols_map`.
- If either account is **NETTING**, this script won’t reconcile net positions. Start with hedging accounts.

## Run
```bash
python trade_copier.py
```

Logs are printed to console and to `copier.log`.

## Tips for two brokers / terminals
- Use **separate MT5 installations** (different folders). Supply each terminal’s full `terminal64.exe` path in config.
- The script **re-initializes** the MT5 connection when switching between master and slave. For ultra-low latency, prefer two separate scripts/processes—one reader, one executor—communicating via a local queue.

## Safety checklist
- Test on **demo** first.
- Verify symbol mapping and minimum lot/step.
- Confirm SL/TP distances satisfy broker `SYMBOL_TRADE_STOPS_LEVEL` constraints.
- Watch the logs for rejects and adjust `max_slippage_points` and filling type as needed.
