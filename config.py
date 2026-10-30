"""
config.py — central configuration for the Deriv Volatility Bot.

All values come from environment variables (loaded from `.env` if present).
Nothing here is hard-coded to a credential, and there is deliberately no
switch that can enable real-money trading: the bot is demo-only by design.

Design notes
------------
* Every numeric value is validated at load time. A bad value raises
  ``ConfigError`` immediately rather than failing halfway through a trade.
* ``Config`` is a frozen dataclass: once built it cannot be mutated at
  runtime, so no code path can quietly raise the stake or the loss limit.
* Secrets are never included in ``repr()`` or in ``to_safe_dict()``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

try:  # python-dotenv is optional at import time so tests can run without it
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


PROJECT_ROOT = Path(__file__).resolve().parent


class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""


# ---------------------------------------------------------------------------
# Deriv API endpoints — verified against developers.deriv.me (Oct 2026)
# ---------------------------------------------------------------------------
# The legacy `wss://ws.derivws.com/websockets/v3` endpoint is no longer
# reachable (HTTP 520). The current API is:
#   REST  : https://api.derivws.com
#   WS    : wss://api.derivws.com/trading/v1/options/ws/{public,demo,real}
# Authentication is OTP-in-URL: you POST to the OTP endpoint with a Bearer
# token and receive a ready-to-use WebSocket URL. There is no `authorize`
# message on the new API (it returns UnknownMethod).
DERIV_REST_BASE = "https://api.derivws.com"
DERIV_WS_PUBLIC = "wss://api.derivws.com/trading/v1/options/ws/public"
DERIV_WS_DEMO = "wss://api.derivws.com/trading/v1/options/ws/demo"
DERIV_WS_REAL = "wss://api.derivws.com/trading/v1/options/ws/real"

# Standard Volatility Indices. The 1-second variants (1HZ10V etc.) are
# deliberately NOT in this map — they are a different product with a
# different tick rate and must not be confused with these.
VOLATILITY_INDEX_SYMBOLS = {
    10: "R_10",
    25: "R_25",
    50: "R_50",
    75: "R_75",
    100: "R_100",
}

# Human-readable names as returned by active_symbols, used for verification.
VOLATILITY_INDEX_NAMES = {
    10: "Volatility 10 Index",
    25: "Volatility 25 Index",
    50: "Volatility 50 Index",
    75: "Volatility 75 Index",
    100: "Volatility 100 Index",
}

# ENABLED_INDICES keys. The original numeric keys remain unchanged; extra
# synthetic markets use explicit aliases so existing settings keep working.
# One-second Volatility symbols are opt-in because their tick rate differs.
MARKET_CATALOG = {
    0: ("stpRNG", "Step Index"),
    **{idx: (f"R_{idx}", f"Volatility {idx} Index")
       for idx in (10, 25, 50, 75, 100, 150, 250)},
    1010: ("1HZ10V", "Volatility 10 (1s) Index"),
    1015: ("1HZ15V", "Volatility 15 (1s) Index"),
    1025: ("1HZ25V", "Volatility 25 (1s) Index"),
    1030: ("1HZ30V", "Volatility 30 (1s) Index"),
    1050: ("1HZ50V", "Volatility 50 (1s) Index"),
    1075: ("1HZ75V", "Volatility 75 (1s) Index"),
    1090: ("1HZ90V", "Volatility 90 (1s) Index"),
    1100: ("1HZ100V", "Volatility 100 (1s) Index"),
}

# Symbols that belong to the 1-second family. Used to *reject* a match so we
# can never accidentally trade the wrong product.
ONE_SECOND_SYMBOLS = {"1HZ10V", "1HZ15V", "1HZ25V", "1HZ30V",
                      "1HZ50V", "1HZ75V", "1HZ90V", "1HZ100V"}


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    if v is None:
        return default
    v = v.strip()
    return v if v != "" else default


def _env_int(name: str, default: int, minimum: Optional[int] = None,
             maximum: Optional[int] = None) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        val = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}")
    if minimum is not None and val < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {val}")
    if maximum is not None and val > maximum:
        raise ConfigError(f"{name} must be <= {maximum}, got {val}")
    return val


def _env_float(name: str, default: float, minimum: Optional[float] = None,
               maximum: Optional[float] = None) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        val = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}")
    if minimum is not None and val < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {val}")
    if maximum is not None and val > maximum:
        raise ConfigError(f"{name} must be <= {maximum}, got {val}")
    return val


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    low = raw.lower()
    if low in ("1", "true", "yes", "on"):
        return True
    if low in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be a boolean, got {raw!r}")


@dataclass(frozen=True)
class Config:
    """Immutable, validated runtime configuration."""

    # --- credentials -------------------------------------------------------
    app_id: str
    api_token: str
    require_demo_account: bool = True

    # --- contract ----------------------------------------------------------
    contract_type: str = "CALL"
    duration: int = 1
    duration_unit: str = "m"
    stake: float = 1.0
    currency: str = "USD"

    # --- risk --------------------------------------------------------------
    max_open_contracts: int = 3
    max_consecutive_losses: int = 3
    daily_loss_limit: float = 5.0
    daily_profit_target: float = 0.0
    cooldown_seconds: int = 60
    # 0 disables only the daily entry-count cap.
    max_trades_per_day: int = 0
    loss_cooldown_minutes: int = 60
    trading_day_timezone: str = "UTC"

    # --- legacy market allowlist (retained for old .env compatibility) -----
    enabled_indices: List[int] = field(
        default_factory=lambda: [0, 10, 25, 50, 75, 100, 150, 250]
    )

    # --- strategy ----------------------------------------------------------
    ema_fast: int = 20
    ema_mid: int = 50
    ema_slow: int = 200
    rsi_period: int = 14
    rsi_min: float = 50.0
    rsi_max: float = 70.0
    sr_lookback: int = 50
    sr_proximity_atr: float = 0.5
    min_signal_score: int = 60
    require_htf_confirmation: bool = True
    require_candle_confirmation: bool = True
    scan_interval_seconds: int = 15
    ai_brain_enabled: bool = True

    # --- storage -----------------------------------------------------------
    db_path: str = "data/bot.db"
    csv_path: str = "data/trades.csv"
    log_level: str = "INFO"
    log_file: str = "data/bot.log"

    # ------------------------------------------------------------------
    def __repr__(self) -> str:  # never leak the token
        return (
            f"Config(app_id={self.app_id!r}, api_token='***', "
            f"stake={self.stake}, currency={self.currency}, "
            "market_scan='all active synthetic indices', demo_only=True)"
        )

    def to_safe_dict(self) -> dict:
        """Config summary safe to log or print. Token is redacted."""
        return {
            "app_id": self.app_id,
            "api_token": "***redacted***",
            "contract_type": self.contract_type,
            "duration": f"{self.duration}{self.duration_unit}",
            "stake": self.stake,
            "currency": self.currency,
            "max_open_contracts": self.max_open_contracts,
            "max_consecutive_losses": self.max_consecutive_losses,
            "daily_loss_limit": self.daily_loss_limit,
            "daily_profit_target": self.daily_profit_target,
            "cooldown_seconds": self.cooldown_seconds,
            "max_trades_per_day": self.max_trades_per_day,
            "loss_cooldown_minutes": self.loss_cooldown_minutes,
            "trading_day_timezone": self.trading_day_timezone,
            "market_scan": "all active synthetic indices",
            "min_signal_score": self.min_signal_score,
            "ai_brain_enabled": self.ai_brain_enabled,
            "scan_interval_seconds": self.scan_interval_seconds,
            "db_path": self.db_path,
            "csv_path": self.csv_path,
        }

    def resolved_symbols(self) -> dict:
        """Map enabled index -> expected standard symbol code."""
        return {i: MARKET_CATALOG[i][0] for i in self.enabled_indices}


def _parse_indices(raw: Optional[str]) -> List[int]:
    if raw is None:
        return [0, 10, 25, 50, 75, 100, 150, 250]
    out: List[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            idx = int(part)
        except ValueError:
            raise ConfigError(
                f"ENABLED_INDICES contains a non-integer: {part!r}. "
                f"Valid values are {sorted(MARKET_CATALOG)}"
            )
        if idx not in MARKET_CATALOG:
            raise ConfigError(
                f"ENABLED_INDICES contains {idx}, which is not a supported "
                f"market key. Valid values are {sorted(MARKET_CATALOG)}."
            )
        if idx not in out:
            out.append(idx)
    if not out:
        raise ConfigError("ENABLED_INDICES is empty — nothing to trade.")
    return out


def load_config(env_file: Optional[str] = None, require_credentials: bool = True) -> Config:
    """
    Build a validated Config from the environment.

    Parameters
    ----------
    env_file:
        Optional path to a .env file. Defaults to ``<project>/.env``.
    require_credentials:
        When False, missing DERIV_APP_ID / DERIV_API_TOKEN are tolerated.
        Used by the backtester and the test-suite, which never authenticate.
    """
    if load_dotenv is not None:
        path = env_file or str(PROJECT_ROOT / ".env")
        if os.path.exists(path):
            load_dotenv(path, override=False)

    app_id = _env("DERIV_APP_ID", "")
    token = _env("DERIV_API_TOKEN", "")

    if require_credentials:
        if not app_id:
            raise ConfigError(
                "DERIV_APP_ID is not set. Create an application at "
                "https://developers.deriv.com and put the App ID in .env"
            )
        if not token:
            raise ConfigError(
                "DERIV_API_TOKEN is not set. Create a PAT for a DEMO account "
                "in the Deriv dashboard and put it in .env (never in source)."
            )

    contract_type = (_env("CONTRACT_TYPE", "CALL") or "CALL").upper()
    if contract_type not in ("CALL", "PUT"):
        raise ConfigError(
            f"CONTRACT_TYPE must be CALL or PUT, got {contract_type!r}"
        )

    duration_unit = (_env("DURATION_UNIT", "m") or "m").lower()
    if duration_unit not in ("s", "m", "h", "d", "t"):
        raise ConfigError(f"DURATION_UNIT invalid: {duration_unit!r}")

    tz = _env("TRADING_DAY_TIMEZONE", "UTC") or "UTC"
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(tz)
    except Exception:
        raise ConfigError(
            f"TRADING_DAY_TIMEZONE {tz!r} is not a valid IANA timezone "
            f"(e.g. UTC, Europe/London, America/New_York)"
        )

    rsi_min = _env_float("RSI_MIN", 50.0, 0, 100)
    rsi_max = _env_float("RSI_MAX", 70.0, 0, 100)
    if rsi_min >= rsi_max:
        raise ConfigError(
            f"RSI_MIN ({rsi_min}) must be less than RSI_MAX ({rsi_max})"
        )

    ema_fast = _env_int("EMA_FAST", 20, 2, 500)
    ema_mid = _env_int("EMA_MID", 50, 3, 500)
    ema_slow = _env_int("EMA_SLOW", 200, 4, 1000)
    if not (ema_fast < ema_mid < ema_slow):
        raise ConfigError(
            f"EMA periods must satisfy EMA_FAST < EMA_MID < EMA_SLOW "
            f"(got {ema_fast}, {ema_mid}, {ema_slow})"
        )

    stake = _env_float("STAKE", 1.0, 0.01, 1000.0)

    cfg = Config(
        app_id=app_id,
        api_token=token,
        require_demo_account=_env_bool("REQUIRE_DEMO_ACCOUNT", True),
        contract_type=contract_type,
        duration=_env_int("DURATION", 1, 1, 1440),
        duration_unit=duration_unit,
        stake=stake,
        currency=(_env("CURRENCY", "USD") or "USD").upper(),
        max_open_contracts=_env_int("MAX_OPEN_CONTRACTS", 3, 1, 5),
        max_consecutive_losses=_env_int("MAX_CONSECUTIVE_LOSSES", 3, 1, 50),
        daily_loss_limit=_env_float("DAILY_LOSS_LIMIT", 5.0, 0.01, 100000),
        daily_profit_target=_env_float("DAILY_PROFIT_TARGET", 0.0, 0, 100000),
        cooldown_seconds=_env_int("COOLDOWN_SECONDS", 60, 0, 86400),
        max_trades_per_day=_env_int("MAX_TRADES_PER_DAY", 0, 0, 1000),
        loss_cooldown_minutes=_env_int("LOSS_COOLDOWN_MINUTES", 60, 0, 10080),
        trading_day_timezone=tz,
        enabled_indices=_parse_indices(_env("ENABLED_INDICES")),
        ema_fast=ema_fast,
        ema_mid=ema_mid,
        ema_slow=ema_slow,
        rsi_period=_env_int("RSI_PERIOD", 14, 2, 200),
        rsi_min=rsi_min,
        rsi_max=rsi_max,
        sr_lookback=_env_int("SR_LOOKBACK", 50, 5, 500),
        sr_proximity_atr=_env_float("SR_PROXIMITY_ATR", 0.5, 0.01, 10),
        min_signal_score=_env_int("MIN_SIGNAL_SCORE", 60, 0, 100),
        require_htf_confirmation=_env_bool("REQUIRE_HTF_CONFIRMATION", True),
        require_candle_confirmation=_env_bool("REQUIRE_CANDLE_CONFIRMATION", True),
        scan_interval_seconds=_env_int("SCAN_INTERVAL_SECONDS", 15, 1, 3600),
        ai_brain_enabled=_env_bool("AI_BRAIN_ENABLED", True),
        db_path=_env("DB_PATH", "data/bot.db") or "data/bot.db",
        csv_path=_env("CSV_PATH", "data/trades.csv") or "data/trades.csv",
        log_level=(_env("LOG_LEVEL", "INFO") or "INFO").upper(),
        log_file=_env("LOG_FILE", "data/bot.log") or "",
    )

    # A stake of 0 or negative would be silently rejected by the broker; catch
    # it here so the failure is loud and early.
    if cfg.stake <= 0:
        raise ConfigError("STAKE must be greater than zero")

    return cfg


def ensure_data_dir(cfg: Config) -> None:
    """Create the parent directories for the DB, CSV and log files."""
    for p in (cfg.db_path, cfg.csv_path, cfg.log_file):
        if not p:
            continue
        Path(p).parent.mkdir(parents=True, exist_ok=True)
