"""
settings_store.py — validate and persist credentials from the browser.

What this module is allowed to do
---------------------------------
* Validate an App ID and a token *format*.
* Optionally ask Deriv whether the token works, and whether the account it
  belongs to is demo. A real-money account is refused.
* Write credentials and selected trading settings into ``.env`` atomically,
  preserving every other key.

What it must never do
---------------------
* Return the token, log the token, or put it in an exception message.
  ``scrub()`` is applied to every string that leaves this module.
* Weaken the demo-only guard. The guard lives in ``DerivClient.connect`` and
  is untouched; this module only *pre-checks* so the user gets a clear error
  in the browser instead of a failed Start.

Atomic write
------------
``.env.tmp`` -> fsync -> ``os.replace`` -> chmod 0600. ``os.replace`` is
atomic on POSIX and on Windows (same volume), so a crash mid-write can never
leave a half-written ``.env``. The temp file is created with 0600 from the
start, so the secret is never briefly world-readable.
"""

from __future__ import annotations

import os
import re
import secrets
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config import MARKET_CATALOG

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"

# Keys this module owns. Everything else in .env is preserved verbatim.
MANAGED_KEYS = ("DERIV_APP_ID", "DERIV_API_TOKEN")

# Optional, non-secret settings the form may also write.
OPTIONAL_KEYS = ("STAKE", "ENABLED_INDICES", "DAILY_LOSS_LIMIT",
                 "DAILY_PROFIT_TARGET",
                 "MAX_OPEN_CONTRACTS", "MAX_TRADES_PER_DAY",
                 "MAX_CONSECUTIVE_LOSSES")

# Substrings that mark a value as an obvious placeholder rather than a token.
_PLACEHOLDER_MARKERS = (
    "placeholder", "invalid", "your_", "your-", "yourtoken", "changeme",
    "change_me", "example", "dummy", "fake", "xxxx", "todo", "redacted",
    "insert", "paste", "here",
)

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_]{15,256}$")
# App IDs are now either pure digits or modern Deriv IDs that can be
# alphanumeric. The legacy 1089 value is still rejected explicitly.
_APP_ID_RE = re.compile(r"^[A-Za-z0-9]{1,32}$")


# ---------------------------------------------------------------------------
# Scrubbing
# ---------------------------------------------------------------------------
def scrub(text: str, *secrets_: Optional[str]) -> str:
    """Remove any known secret value from a string before it is shown/logged."""
    out = str(text)
    for s in secrets_:
        if s and len(s) >= 6:
            out = out.replace(s, "***REDACTED***")
    # Belt and braces: redact anything that looks like a bearer token.
    out = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}", r"\1***REDACTED***", out)
    out = re.sub(r"(?i)(otp=)[A-Za-z0-9._\-]+", r"\1***REDACTED***", out)
    return out


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def validate_app_id(raw: str) -> Tuple[bool, str]:
    """Modern Deriv App IDs may be numeric or alphanumeric.

    The legacy 1089 ID is rejected explicitly because the current API no longer
    accepts it. Any other non-empty value that is roughly an app identifier is
    accepted so long as it contains only letters/digits.
    """
    v = (raw or "").strip()
    if not v:
        return False, "DERIV_APP_ID is required."
    if not _APP_ID_RE.match(v):
        return False, ("DERIV_APP_ID must be a valid Deriv app ID: letters and "
                       "numbers only, e.g. 123456 or 34BOsdhavC7i5uIM8vb9I.")
    if v == "1089":
        return False, ("1089 is the legacy demo App ID and is rejected by the "
                       "current Deriv API. Create your own app at "
                       "developers.deriv.com.")
    return True, ""


def validate_token(raw: str) -> Tuple[bool, str]:
    """
    Format check only — this says nothing about whether the token is valid.

    Modern Deriv PATs may start with ``pat_`` and include underscores. Spaces,
    punctuation and obvious placeholders still remain invalid.
    """
    v = (raw or "").strip()
    if not v:
        return False, "DERIV_API_TOKEN is required."
    low = v.lower()
    for marker in _PLACEHOLDER_MARKERS:
        if marker in low:
            return False, ("That looks like a placeholder, not a real token. "
                           "Create a token in the Deriv dashboard.")
    if not _TOKEN_RE.match(v):
        if len(v) < 15:
            return False, ("A Deriv token is at least 15 characters. "
                           f"Yours is {len(v)}.")
        return False, ("A Deriv token can contain letters, digits and underscores, "
                       "for example pat_123abc... . Check for spaces, quotes or "
                       "unexpected punctuation.")
    return True, ""


def validate_optional(fields: Dict[str, Any]) -> Tuple[bool, str, Dict[str, str]]:
    """Validate the optional numeric settings. Returns (ok, error, cleaned)."""
    cleaned: Dict[str, str] = {}

    if "stake" in fields and fields["stake"] not in (None, ""):
        try:
            stake = float(fields["stake"])
        except (TypeError, ValueError):
            return False, "Stake must be a number.", {}
        if not (0.01 <= stake <= 1000):
            return False, "Stake must be between 0.01 and 1000.", {}
        cleaned["STAKE"] = f"{stake:g}"

    if "daily_loss_limit" in fields and fields["daily_loss_limit"] not in (None, ""):
        try:
            lim = float(fields["daily_loss_limit"])
        except (TypeError, ValueError):
            return False, "Daily loss limit must be a number.", {}
        if not (0.01 <= lim <= 100000):
            return False, "Daily loss limit must be between 0.01 and 100000.", {}
        cleaned["DAILY_LOSS_LIMIT"] = f"{lim:g}"

    if "daily_profit_target" in fields and fields["daily_profit_target"] not in (None, ""):
        try:
            target = float(fields["daily_profit_target"])
        except (TypeError, ValueError):
            return False, "Daily take-profit target must be a number.", {}
        if not (0 <= target <= 100000):
            return False, "Daily take-profit target must be between 0 and 100000.", {}
        cleaned["DAILY_PROFIT_TARGET"] = f"{target:g}"

    if "max_open_contracts" in fields and fields["max_open_contracts"] not in (None, ""):
        try:
            n = int(fields["max_open_contracts"])
        except (TypeError, ValueError):
            return False, "Max open contracts must be a whole number.", {}
        if not (1 <= n <= 5):
            return False, "Max open contracts must be between 1 and 5.", {}
        cleaned["MAX_OPEN_CONTRACTS"] = str(n)

    if "max_trades_per_day" in fields and fields["max_trades_per_day"] not in (None, ""):
        try:
            n = int(fields["max_trades_per_day"])
        except (TypeError, ValueError):
            return False, "Max trades per day must be a whole number.", {}
        if not (0 <= n <= 1000):
            return False, "Max trades per day must be between 0 and 1000 (0 = unlimited).", {}
        cleaned["MAX_TRADES_PER_DAY"] = str(n)

    if "max_consecutive_losses" in fields and fields["max_consecutive_losses"] not in (None, ""):
        try:
            n = int(fields["max_consecutive_losses"])
        except (TypeError, ValueError):
            return False, "Max consecutive losses must be a whole number.", {}
        if not (1 <= n <= 50):
            return False, "Max consecutive losses must be between 1 and 50.", {}
        cleaned["MAX_CONSECUTIVE_LOSSES"] = str(n)

    if "enabled_indices" in fields and fields["enabled_indices"] not in (None, ""):
        raw = str(fields["enabled_indices"]).replace(" ", "")
        parts = [p for p in raw.split(",") if p]
        if not parts:
            return False, "Enabled indices cannot be empty.", {}
        allowed = {str(key) for key in MARKET_CATALOG}
        for p in parts:
            if p not in allowed:
                return False, (f"{p!r} is not a supported market key. "
                               f"Valid: {', '.join(sorted(allowed))}."), {}
        cleaned["ENABLED_INDICES"] = ",".join(dict.fromkeys(parts))

    return True, "", cleaned


# ---------------------------------------------------------------------------
# Live check against Deriv
# ---------------------------------------------------------------------------
def check_with_deriv(app_id: str, token: str,
                     timeout: int = 20) -> Dict[str, Any]:
    """
    Ask Deriv whether these credentials work, and whether the account is demo.

    Returns a dict with ``ok``, ``demo``, ``accounts`` (types only) and
    ``error``. The token is never included in any returned string.
    """
    from config import Config
    from deriv_client import AuthError, DerivClient, DerivError, RateLimited

    cfg = Config(app_id=app_id, api_token=token)
    client = DerivClient(cfg)
    try:
        accounts = client.list_accounts()
    except AuthError as exc:
        return {"ok": False, "demo": None, "accounts": [],
                "error": scrub(str(exc), token)}
    except RateLimited as exc:
        return {"ok": False, "demo": None, "accounts": [],
                "error": scrub(str(exc), token)}
    except DerivError as exc:
        return {"ok": False, "demo": None, "accounts": [],
                "error": scrub(str(exc), token)}
    except Exception as exc:  # network, DNS, TLS
        return {"ok": False, "demo": None, "accounts": [],
                "error": scrub(f"Could not reach Deriv: {exc}", token)}

    types = [str(a.get("account_type", "")).lower() for a in accounts]
    demo_accounts = [a for a in accounts
                     if str(a.get("account_type", "")).lower() == "demo"]
    return {
        "ok": True,
        "demo": bool(demo_accounts),
        "accounts": sorted(set(types)),
        "account_count": len(accounts),
        "demo_count": len(demo_accounts),
        "error": None,
    }


# ---------------------------------------------------------------------------
# .env reading / writing
# ---------------------------------------------------------------------------
def read_env_lines() -> List[str]:
    if not ENV_PATH.exists():
        return []
    try:
        return ENV_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []


def current_state() -> Dict[str, Any]:
    """
    Report what is configured — booleans and non-secret values only.

    The token is never read into the returned dict, not even masked.
    """
    from config import load_dotenv  # noqa: F401  (ensures .env is loaded)

    if ENV_PATH.exists() and load_dotenv is not None:
        try:
            load_dotenv(str(ENV_PATH), override=False)
        except Exception:
            pass

    app_id = (os.getenv("DERIV_APP_ID") or "").strip()
    token = (os.getenv("DERIV_API_TOKEN") or "").strip()

    def _num(key: str, default: str) -> str:
        return (os.getenv(key) or default).strip()

    return {
        "configured": bool(app_id and token),
        "app_id_present": bool(app_id),
        "token_present": bool(token),
        "app_id": app_id or None,          # App ID is not a secret
        "token_hint": "••••••••" if token else None,  # fixed placeholder only
        "env_exists": ENV_PATH.exists(),
        "env_path": str(ENV_PATH),
        "stake": _num("STAKE", "1.0"),
        "daily_loss_limit": _num("DAILY_LOSS_LIMIT", "5.0"),
        "daily_profit_target": _num("DAILY_PROFIT_TARGET", "0"),
        "max_open_contracts": _num("MAX_OPEN_CONTRACTS", "3"),
        "max_trades_per_day": _num("MAX_TRADES_PER_DAY", "0"),
        "max_consecutive_losses": _num("MAX_CONSECUTIVE_LOSSES", "3"),
        "enabled_indices": _num("ENABLED_INDICES", "0,10,25,50,75,100,150,250"),
    }


def _merge_lines(existing: List[str], updates: Dict[str, str]) -> List[str]:
    """
    Replace managed keys in place, keep every other line byte-identical.

    Comments, blank lines, ordering and unrelated keys all survive.
    """
    remaining = dict(updates)
    out: List[str] = []
    for line in existing:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    # Append anything that was not already present.
    for key, value in remaining.items():
        out.append(f"{key}={value}")
    return out


def write_env(updates: Dict[str, str]) -> Dict[str, Any]:
    """
    Atomically merge ``updates`` into .env with mode 0600.

    Raises OSError on failure; the caller turns that into a 500.
    """
    existing = read_env_lines()
    merged = _merge_lines(existing, updates)
    body = "\n".join(merged).rstrip("\n") + "\n"

    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".env.", suffix=".tmp", dir=str(ENV_PATH.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, ENV_PATH)
        try:
            os.chmod(ENV_PATH, 0o600)
        except OSError:
            pass
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    # Make the new values visible to this process immediately, so a Start
    # right after saving does not need a restart.
    for key, value in updates.items():
        os.environ[key] = value

    return {
        "path": str(ENV_PATH),
        "keys_written": sorted(updates),
        "mode": oct(ENV_PATH.stat().st_mode & 0o777),
        "preserved_keys": sorted(
            line.split("=", 1)[0].strip()
            for line in existing
            if "=" in line and not line.strip().startswith("#")
            and line.split("=", 1)[0].strip() not in updates
        ),
    }


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------
CSRF_COOKIE = "dv_csrf"
CSRF_HEADER = "X-CSRF-Token"


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_ok(cookie_value: Optional[str], header_value: Optional[str]) -> bool:
    """Double-submit check: the header must equal the cookie, constant-time."""
    if not cookie_value or not header_value:
        return False
    return secrets.compare_digest(str(cookie_value), str(header_value))
