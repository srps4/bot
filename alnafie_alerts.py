# alnafie_alerts.py
# Lightweight alerts for Alnafie: pre-news heads-up, trade burst, lot spike, and DD warnings.
from __future__ import annotations
import os, json, math, time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from collections import deque
from typing import List, Tuple, Optional, Dict

UTC = timezone.utc

def _get(key: str, default: str) -> str:
    v = os.getenv(key)
    return v if v not in (None, "") else default

def _get_f(key: str, default: float) -> float:
    try:
        return float(_get(key, str(default)))
    except Exception:
        return float(default)

def _now_utc() -> datetime:
    return datetime.now(tz=UTC)

@dataclass(frozen=True)
class AlertConfig:
    # News/events as CSV of "label|ISO8601Z|pre_min|post_min" items
    # e.g. NFP|2025-09-05T12:30:00Z|15|10
    events_csv: str
    trade_burst_window_sec: int
    trade_burst_count: int
    lot_spike_threshold: float
    dd_warn_frac: float  # warn when |daily_pnl| reaches dd_warn_frac * daily_limit_loss

    @staticmethod
    def from_env() -> "AlertConfig":
        return AlertConfig(
            events_csv=_get("ALERT_EVENTS", ""),
            trade_burst_window_sec=int(_get("ALERT_TRADE_BURST_WINDOW_SEC", "300")),
            trade_burst_count=int(_get("ALERT_TRADE_BURST_COUNT", "10")),
            lot_spike_threshold=_get_f("ALERT_LOT_SPIKE_THRESHOLD", 1.00),
            dd_warn_frac=_get_f("ALERT_DD_WARN_FRAC", 0.8),
        )

class AlnafieAlerts:
    def __init__(self, cfg: Optional[AlertConfig] = None):
        self.cfg = cfg or AlertConfig.from_env()
        # sliding window of executed trades timestamps
        self._trade_times: deque[datetime] = deque(maxlen=1000)

    # --------- NEWS / EVENTS ---------
    def check_pre_news(self, now: Optional[datetime] = None) -> List[str]:
        """
        Returns alert strings if we're within a pre/post window for any configured event.
        Format per item in ALERT_EVENTS: label|2025-09-05T12:30:00Z|pre_min|post_min
        """
        now = now or _now_utc()
        alerts: List[str] = []
        if not self.cfg.events_csv.strip():
            return alerts

        for chunk in self.cfg.events_csv.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                label, iso_ts, pre_min, post_min = chunk.split("|")
                when = datetime.fromisoformat(iso_ts.replace("Z", "+00:00")).astimezone(UTC)
                pre = timedelta(minutes=int(pre_min))
                post = timedelta(minutes=int(post_min))
                if when - pre <= now <= when:
                    mins = math.ceil((when - now).total_seconds() / 60.0)
                    alerts.append(f"[NEWS] {label} in T-{mins}m — consider reducing size or pausing new entries.")
                elif when < now <= when + post:
                    mins = math.floor((now - when).total_seconds() / 60.0)
                    alerts.append(f"[NEWS] {label} released {mins}m ago — spreads/volatility may be elevated.")
            except Exception:
                continue
        return alerts

    # --------- TRADE BURST ---------
    def on_trade_executed(self, executed_at_utc: Optional[datetime] = None) -> List[str]:
        """
        Call this after you place/copy a trade. Returns a burst alert if threshold is hit.
        """
        now = executed_at_utc or _now_utc()
        self._trade_times.append(now)

        # drop old trades outside window
        window = timedelta(seconds=self.cfg.trade_burst_window_sec)
        while self._trade_times and now - self._trade_times[0] > window:
            self._trade_times.popleft()

        if len(self._trade_times) >= self.cfg.trade_burst_count:
            return [f"[FLOW] {len(self._trade_times)} trades in last {self.cfg.trade_burst_window_sec}s — burst detected."]
        return []

    # --------- LOT SPIKE ---------
    def on_order_prepared(self, lots: float) -> List[str]:
        """
        Call this right after you compute lots (before sending).
        """
        if lots >= self.cfg.lot_spike_threshold:
            return [f"[SIZE] Lot spike: proposed {lots:.2f} lots (>= {self.cfg.lot_spike_threshold})."]
        return []

    # --------- DRAWDOWN EARLY WARNING ---------
    def check_dd_warning(self, daily_pnl: float, daily_limit_loss: float) -> List[str]:
        """
        Pass dd info from your risk engine: warn when |daily_pnl| >= warn_frac * |limit|.
        """
        alerts: List[str] = []
        if daily_limit_loss >= 0:  # should be negative
            return alerts
        warn_level = self.cfg.dd_warn_frac * abs(daily_limit_loss)
        if -daily_pnl >= warn_level:  # daily_pnl is negative and past warn level
            used = 100.0 * (-daily_pnl / abs(daily_limit_loss))
            alerts.append(f"[RISK] Daily loss at {used:.1f}% of limit — tighten stops / reduce size.")
        return alerts
