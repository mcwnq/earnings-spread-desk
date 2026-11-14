"""Settings, stored as settings.json in the data folder ~/EarningsDesk (or wherever --config points)."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

MARKET_DATA_TYPES = {"Live": 1, "Frozen": 2, "Delayed": 3, "DelayedFrozen": 4}
CRUSH_MODES = ["mult", "retrace"]   # front IV x %, or give back % of the front-over-back premium
STRIKE_RULES = ["call_above", "nearest_otm"]
GAP_BASES = ["avg", "near"]         # endurable gap from the average of both breakevens, or the nearer one


@dataclass
class ConnectionConfig:
    host: str = "127.0.0.1"
    port: int = 7497
    client_id: int = 290              # GEX Live uses 190-194; this app tries 290-294
    market_data_type: str = "Live"


@dataclass
class LoadConfig:
    max_lines: int = 40               # market data lines held at once (IBKR default allowance is 100, GEX Live takes 60)
    requests_per_second: int = 40     # message pacing towards TWS (IBKR allows 50)
    option_timeout_seconds: float = 6.0
    history_per_10_minutes: int = 50  # IBKR allows 60 historical data requests per 10 minutes


@dataclass
class EarningsConfig:
    use_nasdaq: bool = True
    finnhub_api_key: str = ""         # free key from finnhub.io; empty = Nasdaq only (dates then need a manual check)
    days_ahead: int = 7               # calendar days of announcements to load
    refresh_minutes: float = 60.0
    min_market_cap_billions: float = 2.0   # Nasdaq lists hundreds of small names a day; skip the ones below this
    watchlist: list[str] = field(default_factory=list)   # "Earnings I'm Following": when set, only these symbols
    max_symbols_per_day: int = 40


@dataclass
class StrategyConfig:
    ba_pass_percent: float = 110.0    # ask may be at most this % of bid on both legs
    ba_review_percent: float = 120.0  # between pass and this: review; above: fail
    iv_edge_points: float = 2.0       # Yes/No: front IV must be this many points above back IV
    crush_mode: str = "mult"
    crush_percent: float = 80.0       # front IV after earnings = front IV x 80% (crush_mode mult)
    back_iv_change_percent: float = 0.0
    gap_basis: str = "avg"
    strike_rule: str = "call_above"   # call_above: nearest call at/above price (course trade); nearest_otm (spreadsheet)
    lookback: int = 7                 # past announcements compared in Is/Was
    strong_hits: int = 6
    acceptable_hits: int = 5
    risk_free_rate: float = 0.04
    skip_back_iv_points: float = 8.0  # skip the next expiry for the long leg when its IV is this far above the one after
    max_back_days: int = 45
    trades_per_day: int = 1           # 1 for newer traders, up to 5 for experienced
    budget: float = 500.0             # max loss budget per trade, for sizing
    screen_days_ahead: int = 1        # option screens for entries today and the next N sessions
    screen_minutes: float = 15.0      # re-screen today's entries this often during the last 90 minutes
    record_forward_test: bool = True  # save entry snapshots and quote the same spreads the next session
    exit_checks: list[str] = field(default_factory=lambda: ["09:45", "11:00", "13:30", "15:45"])   # ET


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8060


@dataclass
class AppConfig:
    connection: ConnectionConfig = field(default_factory=ConnectionConfig)
    load: LoadConfig = field(default_factory=LoadConfig)
    earnings: EarningsConfig = field(default_factory=EarningsConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    data_dir: str = ""

    def data_path(self) -> Path:
        p = Path(self.data_dir) if self.data_dir else Path.home() / "EarningsDesk"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def to_dict(self) -> dict:
        return asdict(self)

    def public_dict(self) -> dict:
        """Settings for the browser: the Finnhub key is shown only as set or not set."""
        d = self.to_dict()
        d["earnings"]["finnhub_api_key"] = "********" if self.earnings.finnhub_api_key else ""
        return d


def _build(cls, data):
    if not isinstance(data, dict):
        return cls()
    kwargs = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        v = data[f.name]
        default = cls.__dataclass_fields__[f.name]
        sub = getattr(default.type, "__dataclass_fields__", None) if not isinstance(default.type, str) else None
        typ = _TYPES.get((cls.__name__, f.name))
        if typ is not None:
            kwargs[f.name] = _build(typ, v)
        elif sub:
            kwargs[f.name] = _build(default.type, v)
        else:
            kwargs[f.name] = v
    return cls(**kwargs)


_TYPES = {("AppConfig", "connection"): ConnectionConfig, ("AppConfig", "load"): LoadConfig,
          ("AppConfig", "earnings"): EarningsConfig, ("AppConfig", "strategy"): StrategyConfig,
          ("AppConfig", "server"): ServerConfig}


def from_dict(data: dict, keep_key_from: AppConfig | None = None) -> AppConfig:
    cfg = _build(AppConfig, data)
    if keep_key_from is not None and cfg.earnings.finnhub_api_key == "********":
        cfg.earnings.finnhub_api_key = keep_key_from.earnings.finnhub_api_key
    cfg.earnings.watchlist = [s.strip().upper() for s in cfg.earnings.watchlist if str(s).strip()]
    return cfg


def load(path: str) -> AppConfig:
    p = Path(path)
    if not p.exists():
        cfg = AppConfig()
        save(cfg, path)
        return cfg
    with open(p, encoding="utf-8") as fh:
        return from_dict(json.load(fh))


def save(cfg: AppConfig, path: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg.to_dict(), fh, indent=2)
    os.replace(tmp, p)


def validate(cfg: AppConfig) -> list[str]:
    e = []
    c, s, l = cfg.connection, cfg.strategy, cfg.load
    if not (1 <= int(c.port) <= 65535):
        e.append("TWS port must be between 1 and 65535.")
    if c.market_data_type not in MARKET_DATA_TYPES:
        e.append(f"Market data type must be one of {', '.join(MARKET_DATA_TYPES)}.")
    if not (5 <= l.max_lines <= 100):
        e.append("Live data lines must be between 5 and 100.")
    if not (1 <= l.requests_per_second <= 50):
        e.append("Requests per second must be between 1 and 50 (IBKR's limit).")
    if s.crush_mode not in CRUSH_MODES:
        e.append("Crush mode must be mult or retrace.")
    if s.strike_rule not in STRIKE_RULES:
        e.append("Default strike must be call_above or nearest_otm.")
    if s.gap_basis not in GAP_BASES:
        e.append("Endurable gap basis must be avg or near.")
    if not (10 <= s.crush_percent <= 100):
        e.append("Front IV crush must be between 10% and 100%.")
    if not (100 <= s.ba_pass_percent <= s.ba_review_percent):
        e.append("Bid-ask pass % must be at least 100 and no more than the review %.")
    if not (3 <= s.lookback <= 12):
        e.append("Past announcements compared must be between 3 and 12.")
    if not (1 <= s.trades_per_day <= 10):
        e.append("Trades per day must be between 1 and 10.")
    import re as _re
    if not all(isinstance(t, str) and _re.match(r"^(09:(3[1-9]|[45]\d)|1[0-5]:[0-5]\d)$", t) for t in s.exit_checks):
        e.append("Exit check times must be HH:MM between 09:31 and 15:59 ET.")
    if not (1 <= cfg.earnings.days_ahead <= 21):
        e.append("Days of announcements to load must be between 1 and 21.")
    return e
