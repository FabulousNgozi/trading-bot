"""
deriv_client.py — the only module that talks to Deriv.

API contract (verified against developers.deriv.me and the live endpoints,
October 2026 — see README "API divergences" for what changed):

  REST  https://api.derivws.com
        GET  /trading/v1/options/accounts            -> account list
        POST /trading/v1/options/accounts/{id}/otp   -> {"data": {"url": "wss://..."}}
        Headers: Authorization: Bearer <PAT>, Deriv-App-ID: <app_id>

  WS    wss://api.derivws.com/trading/v1/options/ws/{public,demo,real}
        The authenticated URL already carries ?otp=... — there is NO
        `authorize` message on this API (it returns UnknownMethod).

Design decisions
----------------
* **Demo-only.** ``connect()`` positively confirms ``account_type == "demo"``
  from the REST account list before it will open a trading socket. If the
  account cannot be identified as demo, it raises ``DemoAccountRequired`` and
  no purchase is ever attempted. There is no flag to bypass this.
* **Idempotent buy.** A timeout on ``buy`` is ambiguous: the contract may or
  may not exist. ``buy_contract`` therefore reconciles against the portfolio
  before it will ever retry, and never sends a second buy for the same
  proposal.
* **Bounded reconnects.** Exponential backoff with a cap, and subscriptions
  are restored after a reconnect.
* **No token in logs.** The token is only ever placed in an HTTP header; the
  OTP URL is redacted before it can reach a log line.
"""

from __future__ import annotations

import json
import random
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import requests
import websocket  # websocket-client

from config import (
    Config,
    DERIV_REST_BASE,
    DERIV_WS_PUBLIC,
)
from logger_config import get_logger

log = get_logger("deriv_client")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
class DerivError(Exception):
    """Base class for all Deriv API failures."""


class AuthError(DerivError):
    """Authentication failed, or the token is not usable."""


class DemoAccountRequired(DerivError):
    """The authorised account is not positively identified as a demo account."""


class ApiError(DerivError):
    """The API returned an error object."""

    def __init__(self, code: str, message: str, details: Any = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details


class RateLimited(DerivError):
    """The API is rate-limiting us."""


class ConnectionLost(DerivError):
    """The WebSocket dropped and could not be restored."""


class ProposalRejected(DerivError):
    """A proposal could not be obtained or failed validation."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def redact_url(url: str) -> str:
    """Strip the OTP from a WebSocket URL so it is safe to log."""
    if "otp=" not in url:
        return url
    base, _, _ = url.partition("otp=")
    return base + "otp=***REDACTED***"


class DerivClient:
    """
    Synchronous Deriv client.

    A single background thread owns the WebSocket read loop; requests are
    matched to responses by ``req_id``. This keeps the public API simple
    (blocking calls with timeouts) while still allowing subscriptions.
    """

    def __init__(self, cfg: Config, session: Optional[requests.Session] = None) -> None:
        self.cfg = cfg
        self._http = session or requests.Session()
        self._ws_conn: Optional[websocket.WebSocket] = None
        self._req_id = 0
        self._lock = threading.RLock()
        self._responses: Dict[int, Dict[str, Any]] = {}
        self._events: Dict[int, threading.Event] = {}
        self._subscribers: Dict[str, Callable[[Dict[str, Any]], None]] = {}
        self._reader: Optional[threading.Thread] = None
        self._running = False
        self._connected = False
        self._ws_url: Optional[str] = None
        self._reconnect_attempts = 0
        self._max_reconnects = 5
        self._subscriptions: List[Dict[str, Any]] = []

        # Populated by connect()
        self.account_id: Optional[str] = None
        self.account_type: Optional[str] = None
        self.currency: Optional[str] = None
        self.balance: Optional[float] = None

    # ==================================================================
    # REST layer
    # ==================================================================
    def _rest_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.cfg.api_token}",
            "Deriv-App-ID": self.cfg.app_id,
            "Content-Type": "application/json",
        }

    def _rest(self, method: str, path: str, timeout: int = 20) -> Dict[str, Any]:
        url = f"{DERIV_REST_BASE}{path}"
        try:
            resp = self._http.request(
                method, url, headers=self._rest_headers(), timeout=timeout
            )
        except requests.RequestException as exc:
            raise DerivError(f"REST request failed: {exc}") from exc

        if resp.status_code == 401:
            raise AuthError(
                "Deriv rejected the credentials (401). Check DERIV_APP_ID and "
                "DERIV_API_TOKEN, and that the token has the 'trade' scope."
            )
        if resp.status_code == 403:
            raise AuthError(
                "Deriv denied access (403). The token may lack the required "
                "scope, or the app is not approved."
            )
        if resp.status_code == 429:
            raise RateLimited("Deriv REST API rate limit hit (429).")
        if resp.status_code >= 400:
            detail = ""
            try:
                body = resp.json()
                errs = body.get("errors") or []
                if errs:
                    detail = "; ".join(
                        f"{e.get('code')}: {e.get('message')}" for e in errs
                    )
            except Exception:
                detail = resp.text[:200]
            raise DerivError(f"REST {resp.status_code}: {detail}")

        try:
            return resp.json()
        except ValueError as exc:
            raise DerivError("REST response was not valid JSON") from exc

    def list_accounts(self) -> List[Dict[str, Any]]:
        """GET /trading/v1/options/accounts -> list of account objects."""
        body = self._rest("GET", "/trading/v1/options/accounts")
        data = body.get("data")
        if not isinstance(data, list):
            raise DerivError("Unexpected account list response shape")
        return data

    def request_otp_url(self, account_id: str) -> str:
        """POST .../accounts/{id}/otp -> authenticated WebSocket URL."""
        body = self._rest(
            "POST", f"/trading/v1/options/accounts/{account_id}/otp"
        )
        url = (body.get("data") or {}).get("url")
        if not url:
            raise AuthError("OTP endpoint returned no WebSocket URL")
        return url

    # ==================================================================
    # Connection
    # ==================================================================
    def connect(self) -> Dict[str, Any]:
        """
        Authenticate and open the trading socket.

        Refuses to proceed unless the account is positively identified as a
        demo (virtual) account.
        """
        accounts = self.list_accounts()
        if not accounts:
            raise AuthError(
                "No Options accounts found for this token. Create a demo "
                "account in the Deriv dashboard first."
            )

        # Prefer an active demo account; never silently fall back to real.
        demo = [a for a in accounts
                if str(a.get("account_type", "")).lower() == "demo"]
        if not demo:
            types = sorted({str(a.get("account_type")) for a in accounts})
            raise DemoAccountRequired(
                "DEMO-ONLY: no demo account found for this token "
                f"(account types present: {types}). The bot will not trade a "
                "real-money account. Create a demo account and use a token "
                "for it."
            )

        active_demo = [a for a in demo if a.get("status") == "active"] or demo
        acct = active_demo[0]
        self.account_id = str(acct.get("account_id"))
        self.account_type = str(acct.get("account_type", "")).lower()
        self.currency = acct.get("currency")
        self.balance = acct.get("balance")

        # Belt and braces: re-assert the invariant after assignment.
        if self.account_type != "demo":
            raise DemoAccountRequired(
                f"DEMO-ONLY: resolved account {self.account_id} is "
                f"{self.account_type!r}, refusing to connect."
            )

        url = self.request_otp_url(self.account_id)
        # The OTP URL determines demo vs real. Verify it points at /demo.
        if "/ws/demo" not in url:
            raise DemoAccountRequired(
                "DEMO-ONLY: the OTP URL does not point at the demo endpoint "
                f"({redact_url(url)}). Refusing to connect."
            )

        self._ws_url = url
        self._open_socket(url)

        log.info(
            "Connected — account %s (%s), currency %s, balance %s",
            self.account_id, self.account_type, self.currency, self.balance,
        )
        return {
            "account_id": self.account_id,
            "account_type": self.account_type,
            "currency": self.currency,
            "balance": self.balance,
        }

    def _open_socket(self, url: str) -> None:
        with self._lock:
            self._responses.clear()
            self._events.clear()
            self._connected = False

        try:
            conn = websocket.create_connection(
                url, timeout=20, enable_multithread=True
            )
        except Exception as exc:
            raise ConnectionLost(f"WebSocket connect failed: {exc}") from exc

        self._ws_conn = conn
        self._connected = True
        self._running = True
        self._reader = threading.Thread(
            target=self._read_loop, name="deriv-ws-reader", daemon=True
        )
        self._reader.start()

    def _read_loop(self) -> None:
        conn = self._ws_conn
        while self._running and conn is not None:
            try:
                raw = conn.recv()
            except Exception as exc:
                if not self._running:
                    break
                log.warning("WebSocket read error: %s", exc)
                self._connected = False
                self._handle_disconnect()
                break
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            self._dispatch(msg)

    def _dispatch(self, msg: Dict[str, Any]) -> None:
        req_id = msg.get("req_id")
        if req_id is not None:
            with self._lock:
                self._responses[req_id] = msg
                ev = self._events.get(req_id)
            if ev is not None:
                ev.set()

        # Subscription pushes carry msg_type and a subscription id.
        sub = msg.get("subscription") or {}
        sub_id = sub.get("id")
        if sub_id and sub_id in self._subscribers:
            try:
                self._subscribers[sub_id](msg)
            except Exception as exc:  # never let a callback kill the reader
                log.warning("Subscriber callback error: %s", exc)

    def _handle_disconnect(self) -> None:
        """Bounded exponential backoff, then restore subscriptions."""
        if not self._running:
            return
        while self._reconnect_attempts < self._max_reconnects:
            self._reconnect_attempts += 1
            delay = min(2 ** self._reconnect_attempts, 30) + random.uniform(0, 1)
            log.warning(
                "Reconnecting in %.1fs (attempt %d/%d)",
                delay, self._reconnect_attempts, self._max_reconnects,
            )
            time.sleep(delay)
            try:
                # A fresh OTP is required — the old one is single-use.
                if self.account_id:
                    url = self.request_otp_url(self.account_id)
                    self._ws_url = url
                self._open_socket(self._ws_url or DERIV_WS_PUBLIC)
                self._reconnect_attempts = 0
                log.info("Reconnected")
                self._restore_subscriptions()
                return
            except Exception as exc:
                log.warning("Reconnect attempt failed: %s", exc)
        log.error("Giving up after %d reconnect attempts", self._max_reconnects)
        self._running = False

    def _restore_subscriptions(self) -> None:
        for sub in list(self._subscriptions):
            try:
                self._send(sub, expect_response=False)
                log.info("Restored subscription: %s", sub.get("msg_type", "?"))
            except Exception as exc:
                log.warning("Could not restore subscription: %s", exc)

    # ==================================================================
    # Request/response
    # ==================================================================
    def _next_req_id(self) -> int:
        with self._lock:
            self._req_id += 1
            return self._req_id

    def _send(self, payload: Dict[str, Any], expect_response: bool = True,
              timeout: float = 20.0) -> Optional[Dict[str, Any]]:
        if not self._connected or self._ws_conn is None:
            raise ConnectionLost("WebSocket is not connected")

        req_id = self._next_req_id()
        payload = dict(payload)
        payload["req_id"] = req_id

        ev = threading.Event()
        if expect_response:
            with self._lock:
                self._events[req_id] = ev

        try:
            self._ws_conn.send(json.dumps(payload))
        except Exception as exc:
            raise ConnectionLost(f"Send failed: {exc}") from exc

        if not expect_response:
            return None

        if not ev.wait(timeout):
            with self._lock:
                self._events.pop(req_id, None)
            raise DerivError(
                f"Timed out after {timeout}s waiting for "
                f"{payload.get('msg_type') or list(payload)[0]}"
            )

        with self._lock:
            msg = self._responses.pop(req_id, None)
            self._events.pop(req_id, None)

        if msg is None:
            raise DerivError("Response vanished")

        if "error" in msg:
            err = msg["error"]
            code = err.get("code", "Unknown")
            if code == "RateLimit":
                raise RateLimited(err.get("message", "rate limited"))
            raise ApiError(code, err.get("message", ""), err.get("details"))

        return msg

    # ==================================================================
    # Market data
    # ==================================================================
    def active_symbols(self) -> List[Dict[str, Any]]:
        msg = self._send({"active_symbols": "full"})
        return (msg or {}).get("active_symbols", [])

    def contracts_for(self, symbol: str) -> List[Dict[str, Any]]:
        """
        Available contracts for a symbol.

        NOTE: the new API rejects a `currency` field here
        (InputValidationFailed), unlike the legacy v3 API. Do not add it back.
        """
        msg = self._send({"contracts_for": symbol})
        return ((msg or {}).get("contracts_for") or {}).get("available", [])

    def candles(self, symbol: str, granularity: int = 60,
                count: int = 200) -> List[Dict[str, Any]]:
        """
        Historical candles, oldest first.

        The API caps a single response at 1000 candles and rejects
        granularity=30. Supported granularities include 60, 300, 3600.
        """
        count = min(int(count), 1000)
        msg = self._send({
            "ticks_history": symbol,
            "style": "candles",
            "granularity": granularity,
            "count": count,
            "end": "latest",
        })
        return (msg or {}).get("candles", [])

    def subscribe_candles(self, symbol: str, granularity: int,
                          callback: Callable[[Dict[str, Any]], None]) -> str:
        """Subscribe to a candle stream. Returns the subscription id."""
        req_id = self._next_req_id()
        payload = {
            "ticks_history": symbol,
            "style": "candles",
            "granularity": granularity,
            "count": 1,
            "end": "latest",
            "subscribe": 1,
            "req_id": req_id,
        }
        ev = threading.Event()
        with self._lock:
            self._events[req_id] = ev
        self._ws_conn.send(json.dumps(payload))  # type: ignore[union-attr]
        if not ev.wait(20):
            raise DerivError("Timed out subscribing to candles")
        with self._lock:
            msg = self._responses.pop(req_id, None)
            self._events.pop(req_id, None)
        if msg and "error" in msg:
            raise ApiError(msg["error"].get("code", "?"),
                           msg["error"].get("message", ""))
        sub_id = ((msg or {}).get("subscription") or {}).get("id")
        if not sub_id:
            raise DerivError("No subscription id returned")
        self._subscribers[sub_id] = callback
        self._subscriptions.append(payload)
        return sub_id

    # ==================================================================
    # Account
    # ==================================================================
    def balance_now(self) -> Optional[float]:
        msg = self._send({"balance": 1})
        bal = (msg or {}).get("balance") or {}
        if "balance" in bal:
            self.balance = float(bal["balance"])
        return self.balance

    def portfolio(self) -> List[Dict[str, Any]]:
        """Open contracts currently held by the account."""
        msg = self._send({"portfolio": 1})
        return ((msg or {}).get("portfolio") or {}).get("contracts", [])

    # ==================================================================
    # Trading
    # ==================================================================
    def proposal(self, symbol: str, contract_type: str, stake: float,
                 duration: int, duration_unit: str,
                 currency: Optional[str] = None,
                 multiplier: Optional[int] = None) -> Dict[str, Any]:
        """Request a fixed-expiry or multiplier proposal."""
        payload = {
            "proposal": 1,
            "amount": stake,
            "basis": "stake",
            "contract_type": contract_type,
            "currency": currency or self.cfg.currency,
            "duration": duration,
            "duration_unit": duration_unit,
            "underlying_symbol": symbol,
        }
        if contract_type in ("MULTUP", "MULTDOWN"):
            payload.pop("duration")
            payload["multiplier"] = int(multiplier or 2)
        try:
            msg = self._send(payload)
        except ApiError as exc:
            raise ProposalRejected(f"{exc.code}: {exc.message}") from exc
        prop = (msg or {}).get("proposal")
        if not prop:
            raise ProposalRejected("Proposal response contained no proposal")
        return prop

    def buy_contract(self, proposal_id: str, price: float,
                     timeout: float = 30.0) -> Dict[str, Any]:
        """
        Buy using a proposal id.

        A timeout here is genuinely ambiguous — the purchase may have
        succeeded. We therefore reconcile against the portfolio before
        raising, and we never retry the buy automatically.
        """
        try:
            msg = self._send({"buy": proposal_id, "price": price},
                             timeout=timeout)
        except DerivError as exc:
            if "Timed out" in str(exc):
                log.warning(
                    "Buy timed out — reconciling against portfolio before "
                    "deciding whether the purchase succeeded"
                )
                found = self._find_recent_contract(price)
                if found:
                    log.info("Purchase DID succeed: contract %s",
                             found.get("contract_id"))
                    return found
                raise DerivError(
                    "Buy timed out and no matching contract was found in the "
                    "portfolio. Treating as NOT purchased. Manual check "
                    "recommended before the next trade."
                ) from exc
            raise

        receipt = (msg or {}).get("buy")
        if not receipt:
            raise DerivError("Buy response contained no receipt")
        return receipt

    def _find_recent_contract(self, price: float) -> Optional[Dict[str, Any]]:
        """Look for a just-opened contract matching the expected stake."""
        try:
            for c in self.portfolio():
                if abs(float(c.get("buy_price", 0)) - price) < 1e-6:
                    return c
        except Exception as exc:
            log.warning("Portfolio reconciliation failed: %s", exc)
        return None

    def open_contract(self, contract_id: int) -> Dict[str, Any]:
        """Current state of one contract (proposal_open_contract)."""
        msg = self._send({"proposal_open_contract": 1,
                          "contract_id": contract_id})
        poc = (msg or {}).get("proposal_open_contract")
        if not poc:
            raise DerivError(f"No data for contract {contract_id}")
        return poc

    def sell_contract(self, contract_id: int, price: float = 0.0) -> Dict[str, Any]:
        """Sell an open contract using the broker's current available price."""
        msg = self._send({"sell": int(contract_id), "price": float(price)})
        receipt = (msg or {}).get("sell")
        if not receipt:
            raise DerivError(f"No sell receipt for contract {contract_id}")
        return receipt

    def subscribe_contract(self, contract_id: int,
                           callback: Callable[[Dict[str, Any]], None]) -> str:
        """Stream a contract's state until it settles."""
        req_id = self._next_req_id()
        payload = {"proposal_open_contract": 1, "contract_id": contract_id,
                   "subscribe": 1, "req_id": req_id}
        ev = threading.Event()
        with self._lock:
            self._events[req_id] = ev
        self._ws_conn.send(json.dumps(payload))  # type: ignore[union-attr]
        if not ev.wait(20):
            raise DerivError("Timed out subscribing to contract")
        with self._lock:
            msg = self._responses.pop(req_id, None)
            self._events.pop(req_id, None)
        if msg and "error" in msg:
            raise ApiError(msg["error"].get("code", "?"),
                           msg["error"].get("message", ""))
        sub_id = ((msg or {}).get("subscription") or {}).get("id")
        if not sub_id:
            raise DerivError("No subscription id for contract")
        self._subscribers[sub_id] = callback
        self._subscriptions.append(payload)
        return sub_id

    # ==================================================================
    # Lifecycle
    # ==================================================================
    def close(self) -> None:
        self._running = False
        self._connected = False
        try:
            if self._ws_conn is not None:
                self._ws_conn.close()
        except Exception:
            pass
        self._ws_conn = None

    @property
    def connected(self) -> bool:
        return self._connected
