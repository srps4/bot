#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MT5 -> MT5 Trade Copier (risk-based lot sizing)
- Copies master positions to slave account with risk-adjusted lots.
- Requires MetaTrader5 Python package and two MT5 terminals.
"""
import json
import os
import sys
import time
import math
import logging
from dataclasses import dataclass
from typing import Dict, Optional

import MetaTrader5 as mt5


LOG_NAME = "copier.log"
STATE_FILE = "state.json"  # master_ticket -> slave_ticket mapping


# ---------------------- Utilities ----------------------
def setup_logger():
    logger = logging.getLogger("copier")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    fh = logging.FileHandler(LOG_NAME, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger


logger = setup_logger()


def load_json(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def load_state() -> Dict[str, int]:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        return load_json(STATE_FILE)
    except Exception as e:
        logger.error(f"Failed to load state: {e}")
        return {}


def save_state(state: Dict[str, int]):
    try:
        save_json(STATE_FILE, state)
    except Exception as e:
        logger.error(f"Failed to save state: {e}")


# ---------------------- Config dataclasses ----------------------
@dataclass
class AccountConfig:
    login: int
    password: str
    server: str
    path: str


@dataclass
class RiskConfig:
    risk_percent: float
    max_slippage_points: int
    min_lot: float
    lot_step: float
    max_lot: float
    skip_if_no_sl: bool
    default_sl_points: int


@dataclass
class CopyConfig:
    copy_open: bool
    copy_close: bool
    poll_interval_sec: float
    sync_existing_positions_on_start: bool
    magic: int
    comment_prefix: str


@dataclass
class Config:
    master: AccountConfig
    slave: AccountConfig
    risk: RiskConfig
    symbols_map: Dict[str, str]
    copy: CopyConfig


def read_config(path="config.json") -> Config:
    data = load_json(path)
    master = AccountConfig(**data["master"])
    slave = AccountConfig(**data["slave"])
    risk = RiskConfig(**data["risk"])
    copy = CopyConfig(**data["copy"])
    symbols_map = data.get("symbols_map", {})
    return Config(master, slave, risk, symbols_map, copy)


# ---------------------- MT5 Session Wrapper ----------------------
class MT5Session:
    def __init__(self, cfg: AccountConfig, name=""):
        self.cfg = cfg
        self.name = name

    def connect(self) -> bool:
        # Re-initialize to this terminal and login
        if mt5.initialize(path=self.cfg.path, login=self.cfg.login,
                          password=self.cfg.password, server=self.cfg.server):
            acc = mt5.account_info()
            if acc is None:
                logger.error(f"[{self.name}] login failed (no account info).")
                return False
            logger.info(f"[{self.name}] Connected as {acc.login} on {acc.server}")
            return True
        else:
            logger.error(f"[{self.name}] initialize() failed, code={mt5.last_error()}")
            return False

    def shutdown(self):
        try:
            mt5.shutdown()
        except Exception:
            pass

    def select_symbol(self, symbol: str) -> bool:
        info = mt5.symbol_info(symbol)
        if info is None:
            logger.error(f"[{self.name}] Unknown symbol {symbol}")
            return False
        if not info.visible:
            if not mt5.symbol_select(symbol, True):
                logger.error(f"[{self.name}] Failed to select symbol {symbol}")
                return False
        return True

    def get_positions(self, only_magic: Optional[int] = None):
        if only_magic is None:
            return mt5.positions_get()
        return mt5.positions_get(magic=only_magic)

    def get_position_by_ticket(self, ticket: int):
        poss = mt5.positions_get(ticket=ticket)
        if poss:
            return poss[0]
        return None

    def order_send(self, request: dict):
        result = mt5.order_send(request)
        if result is None:
            logger.error(f"[{self.name}] order_send returned None")
            return None
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            logger.error(f"[{self.name}] order_send failed: retcode={result.retcode}, comment={result.comment}")
        else:
            logger.info(f"[{self.name}] order_send OK: order={result.order}, deal={result.deal}")
        return result

    def account_equity(self) -> float:
        info = mt5.account_info()
        return float(info.equity) if info else 0.0

    def symbol_info(self, symbol: str):
        return mt5.symbol_info(symbol)

    def tick(self, symbol: str):
        return mt5.symbol_info_tick(symbol)


# ---------------------- Risk & Sizing ----------------------
def normalize_lot(volume: float, min_lot: float, lot_step: float, max_lot: float) -> float:
    if volume <= 0:
        return 0.0
    steps = math.floor((volume - min_lot) / lot_step + 1e-9)
    vol = min_lot + max(0, steps) * lot_step
    return min(max(vol, min_lot), max_lot)


def value_per_point_per_lot(sym_info) -> float:
    # For most symbols: value per point per 1 lot = tick_value / tick_size
    if not sym_info:
        return 0.0
    if sym_info.tick_size == 0:
        return 0.0
    return float(sym_info.tick_value) / float(sym_info.tick_size)


def compute_risk_based_lot(sl_points: float, equity: float, risk_percent: float,
                           sym_info, min_lot: float, lot_step: float, max_lot: float) -> float:
    vpppl = value_per_point_per_lot(sym_info)
    if vpppl <= 0 or sl_points <= 0:
        return 0.0
    risk_amount = equity * (risk_percent / 100.0)
    raw_lot = risk_amount / (sl_points * vpppl)
    return normalize_lot(raw_lot, min_lot, lot_step, max_lot)


# ---------------------- Copier Logic ----------------------
class TradeCopier:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.master = MT5Session(cfg.master, "MASTER")
        self.slave = MT5Session(cfg.slave, "SLAVE")
        self.state = load_state()  # str(master_ticket) -> int(slave_ticket)

    def connect_both(self) -> bool:
        if not self.master.connect():
            return False
        # Quick ping
        master_acc = mt5.account_info()
        if not master_acc:
            return False
        self.master.shutdown()

        if not self.slave.connect():
            return False
        slave_acc = mt5.account_info()
        if not slave_acc:
            return False
        self.slave.shutdown()
        return True

    def map_symbol(self, master_symbol: str) -> Optional[str]:
        return self.cfg.symbols_map.get(master_symbol, master_symbol)

    def open_slave_position(self, mpos) -> Optional[int]:
        msymbol = mpos.symbol
        ssymbol = self.map_symbol(msymbol)
        if ssymbol is None:
            logger.warning(f"Symbol {msymbol} not mapped; skipping open.")
            return None

        # Connect to SLAVE to place order
        if not self.slave.connect():
            return None
        try:
            if not self.slave.select_symbol(ssymbol):
                return None

            sinfo = self.slave.symbol_info(ssymbol)
            if sinfo is None:
                logger.error(f"[SLAVE] No symbol info for {ssymbol}")
                return None

            # Determine stop distance (points) from master SL
            if mpos.sl != 0.0:
                sl_points = abs(mpos.price_open - mpos.sl) / sinfo.point
            else:
                if self.cfg.risk.skip_if_no_sl:
                    logger.info(f"[SLAVE] Master ticket {mpos.ticket} has no SL; skipping (per config).")
                    return None
                sl_points = float(self.cfg.risk.default_sl_points)

            equity = self.slave.account_equity()
            lot = compute_risk_based_lot(
                sl_points=sl_points,
                equity=equity,
                risk_percent=self.cfg.risk.risk_percent,
                sym_info=sinfo,
                min_lot=self.cfg.risk.min_lot,
                lot_step=self.cfg.risk.lot_step,
                max_lot=self.cfg.risk.max_lot,
            )
            if lot <= 0:
                logger.warning(f"[SLAVE] Computed lot <= 0 for {ssymbol}; skipping.")
                return None

            tick = self.slave.tick(ssymbol)
            if tick is None:
                logger.error(f"[SLAVE] No tick for {ssymbol}")
                return None

            is_buy = (mpos.type == mt5.POSITION_TYPE_BUY)
            price = tick.ask if is_buy else tick.bid
            req_type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL

            # Adjust SL/TP to slave price space
            sl = 0.0
            tp = 0.0
            if mpos.sl != 0.0:
                # Keep same absolute price (assumes both brokers price similarly)
                sl = mpos.sl
            if mpos.tp != 0.0:
                tp = mpos.tp

            # Respect stops level
            stops_level = sinfo.trade_stops_level
            if stops_level and sl != 0.0:
                if is_buy and sl > price - stops_level * sinfo.point:
                    sl = round(price - (stops_level + 1) * sinfo.point, sinfo.digits)
                if (not is_buy) and sl < price + stops_level * sinfo.point:
                    sl = round(price + (stops_level + 1) * sinfo.point, sinfo.digits)

            filling = sinfo.trade_fill_mode
            type_filling = mt5.ORDER_FILLING_IOC if filling in (mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_RETURN) else mt5.ORDER_FILLING_FOK

            req = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": ssymbol,
                "volume": lot,
                "type": req_type,
                "price": price,
                "sl": sl,
                "tp": tp,
                "deviation": self.cfg.risk.max_slippage_points,
                "magic": self.cfg.copy.magic,
                "comment": f"{self.cfg.copy.comment_prefix}{mpos.ticket}",
                "type_filling": type_filling,
            }
            res = self.slave.order_send(req)
            if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                # fetch the new position ticket (deal->position may require a query)
                time.sleep(0.2)
                poss = self.slave.get_positions(only_magic=self.cfg.copy.magic)
                # Best-effort: find newest position with our comment
                slave_ticket = None
                if poss:
                    for p in sorted(poss, key=lambda x: x.time, reverse=True):
                        if p.comment.startswith(self.cfg.copy.comment_prefix) and str(mpos.ticket) in p.comment:
                            slave_ticket = p.ticket
                            break
                return slave_ticket
            return None
        finally:
            self.slave.shutdown()

    def close_slave_position(self, slave_ticket: int):
        if not self.slave.connect():
            return False
        try:
            pos = self.slave.get_position_by_ticket(slave_ticket)
            if not pos:
                logger.info(f"[SLAVE] Position {slave_ticket} already closed.")
                return True

            tick = self.slave.tick(pos.symbol)
            if not tick:
                logger.error(f"[SLAVE] No tick for {pos.symbol}")
                return False

            is_buy = (pos.type == mt5.POSITION_TYPE_BUY)
            price = tick.bid if is_buy else tick.ask
            req_type = mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY

            req = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": pos.symbol,
                "volume": pos.volume,
                "type": req_type,
                "position": pos.ticket,
                "price": price,
                "deviation": self.cfg.risk.max_slippage_points,
                "magic": self.cfg.copy.magic,
                "comment": "close_by_copier",
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
            res = self.slave.order_send(req)
            return bool(res and res.retcode == mt5.TRADE_RETCODE_DONE)
        finally:
            self.slave.shutdown()

    def sync_on_start(self):
        """On startup, copy any existing master positions that aren't yet in state."""
        if not self.master.connect():
            return
        try:
            m_positions = self.master.get_positions()
            m_by_ticket = {p.ticket: p for p in (m_positions or [])}
            for mticket, mpos in m_by_ticket.items():
                if str(mticket) not in self.state:
                    logger.info(f"[SYNC] Copying existing master position {mticket}")
                    slave_ticket = self.open_slave_position(mpos)
                    if slave_ticket:
                        self.state[str(mticket)] = int(slave_ticket)
                        save_state(self.state)
        finally:
            self.master.shutdown()

    def run(self):
        logger.info("Starting trade copier...")
        if self.cfg.copy.sync_existing_positions_on_start:
            self.sync_on_start()

        while True:
            # Pull current master positions
            if not self.master.connect():
                time.sleep(1.0)
                continue

            try:
                m_positions = self.master.get_positions()
                m_tickets_now = set([p.ticket for p in (m_positions or [])])
                m_by_ticket = {p.ticket: p for p in (m_positions or [])}

                # OPEN new ones
                if self.cfg.copy.copy_open:
                    for mticket, mpos in m_by_ticket.items():
                        if str(mticket) not in self.state:
                            logger.info(f"[OPEN] New master position {mticket} {mpos.symbol}")
                            slave_ticket = self.open_slave_position(mpos)
                            if slave_ticket:
                                self.state[str(mticket)] = int(slave_ticket)
                                save_state(self.state)

                # CLOSE those that disappeared
                if self.cfg.copy.copy_close:
                    mapped_master_tickets = list(self.state.keys())
                    for mt_str in mapped_master_tickets:
                        mt = int(mt_str)
                        if mt not in m_tickets_now:
                            slave_ticket = self.state.get(mt_str)
                            if slave_ticket:
                                logger.info(f"[CLOSE] Master {mt} closed; closing slave {slave_ticket}")
                                ok = self.close_slave_position(slave_ticket)
                                if ok:
                                    self.state.pop(mt_str, None)
                                    save_state(self.state)

            finally:
                self.master.shutdown()

            time.sleep(self.cfg.copy.poll_interval_sec)


def main():
    cfg_path = "config.json"
    if not os.path.exists(cfg_path):
        print("Config file 'config.json' not found. Create it from 'config.example.json'.")
        sys.exit(1)

    cfg = read_config(cfg_path)
    copier = TradeCopier(cfg)
    if not copier.connect_both():
        logger.error("Failed to connect to one or both accounts. Check config/terminals.")
        sys.exit(2)
    copier.run()


if __name__ == "__main__":
    main()
