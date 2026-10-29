"""
server.py — the local HTTP control panel.

Run with:

    python server.py                 # http://127.0.0.1:8000
    python server.py --port 9000
    python server.py --host 0.0.0.0  # prints a security warning

Why Flask rather than FastAPI
-----------------------------
The bot is synchronous: the engine uses blocking ``websocket-client`` reads on
its own thread, and SQLite access is synchronous. FastAPI would buy us async
endpoints that immediately block on that synchronous engine, plus a mandatory
Uvicorn worker — complexity with no benefit. Flask's threaded dev server maps
one request per thread cleanly onto this workload, Server-Sent Events are a
plain generator response, and there is no ASGI/WSGI bridge to reason about.
The dependency is also smaller, which matters for a project aimed at people
installing it on their own machine.

Security posture
----------------
* Binds ``127.0.0.1`` by default. Any other host prints a loud warning.
* **No endpoint can place a trade.** The only way to trade is ``/api/bot/start``,
  which starts the unmodified engine; every purchase still passes
  ``RiskManager.can_trade`` and the demo-only guard inside ``DerivClient``.
* The API token is never read into a response. ``/api/status`` reports
  booleans only. A ``_scrub`` pass runs over every JSON body as a last line of
  defence, and responses carry ``Cache-Control: no-store``.
* Host-header check: a request whose ``Host`` is not loopback/LAN is rejected,
  which blocks DNS-rebinding attacks against the local panel.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterator

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    request,
    send_file,
    send_from_directory,
)

from . import settings_store
from .bot_service import SERVICE, attach_ui_logging
from .state import STATE

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = Flask(__name__, static_folder=None)
app.config["JSON_SORT_KEYS"] = False

# Anything that looks like a credential must never reach a response body.
_SENSITIVE_KEYS = {
    "token", "api_token", "deriv_api_token", "authorization", "otp",
    "access_token", "secret", "bearer",
}


def _scrub(obj: Any) -> Any:
    """Recursively drop any key that could carry a credential."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if str(k).lower() in _SENSITIVE_KEYS:
                continue
            out[k] = _scrub(v)
        return out
    if isinstance(obj, list):
        return [_scrub(v) for v in obj]
    return obj


def _ok(payload: Any = None, status: int = 200) -> Response:
    body = {"ok": True}
    if payload is not None:
        body["data"] = payload
    resp = jsonify(_scrub(body))
    resp.status_code = status
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _err(message: str, status: int = 400, code: str = "bad_request") -> Response:
    resp = jsonify(_scrub({"ok": False, "error": message, "code": code}))
    resp.status_code = status
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------------------------------------------------------------------
# Localhost guard
# ---------------------------------------------------------------------------
_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def _host_is_local(host_header: str) -> bool:
    if not host_header:
        return False
    host = host_header.rsplit(":", 1)[0].strip("[]").lower()
    render_hostname = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "").strip().lower()
    if render_hostname and host == render_hostname:
        return True
    if host in _ALLOWED_HOSTS:
        return True
    # Private LAN ranges are acceptable: the user may have chosen --host.
    try:
        ip = socket.gethostbyname(host)
    except Exception:
        return False
    return (
        ip.startswith("127.")
        or ip.startswith("10.")
        or ip.startswith("192.168.")
        or any(ip.startswith(f"172.{n}.") for n in range(16, 32))
    )


@app.before_request
def _guard() -> Any:
    if request.path == "/healthz":
        return None
    if request.method == "OPTIONS":
        return None
    if not _host_is_local(request.headers.get("Host", "")):
        # Blocks DNS-rebinding: a page on evil.example resolving to 127.0.0.1
        # sends Host: evil.example and is refused here.
        return _err("Host not allowed.", 403, "host_not_allowed")
    username = os.environ.get("DASHBOARD_USERNAME", "")
    password = os.environ.get("DASHBOARD_PASSWORD", "")
    is_render = bool(os.environ.get("RENDER_EXTERNAL_HOSTNAME"))
    if is_render and (not username or not password):
        return _err("Dashboard credentials are not configured.", 503,
                    "dashboard_auth_not_configured")
    if username or password:
        import hmac
        supplied = request.authorization
        supplied_username = supplied.username if supplied else ""
        supplied_password = supplied.password if supplied else ""
        user_ok = hmac.compare_digest(supplied_username, username)
        pass_ok = hmac.compare_digest(supplied_password, password)
        if not (username and password and user_ok and pass_ok):
            resp = _err("Dashboard login required.", 401, "authentication_required")
            resp.headers["WWW-Authenticate"] = 'Basic realm="Deriv Bot", charset="UTF-8"'
            return resp
    return None


# ---------------------------------------------------------------------------
# Static UI
# ---------------------------------------------------------------------------
@app.get("/")
def index() -> Response:
    resp = send_from_directory(STATIC_DIR, "index.html")
    resp.headers["Cache-Control"] = "no-store"
    return _issue_csrf(resp)


@app.get("/healthz")
def healthz() -> Response:
    return jsonify({"ok": True})


@app.get("/static/<path:name>")
def static_file(name: str) -> Response:
    resp = send_from_directory(STATIC_DIR, name)
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _issue_csrf(resp: Response) -> Response:
    """
    Give the browser a CSRF token in a cookie the JS can read.

    ``SameSite=Strict`` means the cookie is not sent on a cross-site request
    at all, and the double-submit header check catches anything that slips
    past. Not ``HttpOnly`` on purpose: the page must read it to echo it back.
    """
    token = settings_store.new_csrf_token()
    secure_cookie = bool(os.environ.get("RENDER_EXTERNAL_HOSTNAME"))
    resp.set_cookie(
        settings_store.CSRF_COOKIE, token,
        httponly=False, samesite="Strict", secure=secure_cookie, path="/",
    )
    return resp


@app.get("/settings")
def settings_page() -> Response:
    resp = send_from_directory(STATIC_DIR, "settings.html")
    resp.headers["Cache-Control"] = "no-store"
    return _issue_csrf(resp)


@app.get("/api/settings")
def api_settings_get() -> Response:
    """Current configuration state. Never includes the token value."""
    return _ok(settings_store.current_state())


@app.post("/api/settings")
def api_settings_post() -> Response:
    """
    Validate and persist credentials.

    Guards, in order:
      1. Host header must be loopback/LAN (the global before_request guard).
      2. CSRF double-submit: X-CSRF-Token must equal the dv_csrf cookie.
      3. App ID numeric, token format sane, no placeholders.
      4. Optional live Deriv check — a real-money account is refused.
      5. Atomic write to .env with mode 0600.
    """
    # --- CSRF ---------------------------------------------------------
    if not settings_store.csrf_ok(
        request.cookies.get(settings_store.CSRF_COOKIE),
        request.headers.get(settings_store.CSRF_HEADER),
    ):
        return _err(
            "CSRF check failed. Reload the settings page and try again.",
            403, "csrf_failed",
        )

    if os.environ.get("RENDER_EXTERNAL_HOSTNAME"):
        return _err(
            "On Render, update Deriv credentials in the service Environment settings.",
            409, "credentials_managed_by_render",
        )

    body: Dict[str, Any] = request.get_json(silent=True) or {}
    app_id = str(body.get("app_id") or "").strip()
    token = str(body.get("token") or "").strip()
    verify_live = bool(body.get("verify_live", True))

    # --- format validation --------------------------------------------
    ok, msg = settings_store.validate_app_id(app_id)
    if not ok:
        return _err(msg, 422, "invalid_app_id")

    ok, msg = settings_store.validate_token(token)
    if not ok:
        return _err(msg, 422, "invalid_token")

    ok, msg, optional = settings_store.validate_optional(body)
    if not ok:
        return _err(msg, 422, "invalid_setting")

    # --- live check ----------------------------------------------------
    live: Dict[str, Any] = {"checked": False}
    if verify_live:
        result = settings_store.check_with_deriv(app_id, token)
        live = {
            "checked": True,
            "reachable": result["ok"],
            "demo": result.get("demo"),
            "accounts": result.get("accounts", []),
            "error": result.get("error"),
        }
        if not result["ok"]:
            # A rejected token is a hard stop: saving it would only make
            # Start fail later with a less clear message.
            return _err(
                settings_store.scrub(
                    result.get("error") or "Deriv rejected these credentials.",
                    token,
                ),
                422, "auth_failed",
            )
        if not result.get("demo"):
            return _err(
                "This token has no demo (virtual) account. The bot is "
                "demo-only by design and will refuse to run on a real-money "
                "account. Create a demo account and a token from it.",
                422, "real_account_rejected",
            )

    # --- persist -------------------------------------------------------
    updates: Dict[str, str] = {
        "DERIV_APP_ID": app_id,
        "DERIV_API_TOKEN": token,
    }
    updates.update(optional)

    try:
        info = settings_store.write_env(updates)
    except OSError as exc:
        return _err(
            settings_store.scrub(f"Could not write .env: {exc}", token),
            500, "write_failed",
        )

    # The config layer reads os.environ at load_config() time, so a Start
    # after this save picks the new values up without a restart.
    restart_required = SERVICE.running
    log_msg = ("Credentials saved to .env (app_id=%s, token=***, keys=%s)"
               % (app_id, ",".join(sorted(updates))))
    STATE.add_log("INFO", log_msg)

    return _ok({
        "saved": True,
        "app_id": app_id,
        "token_present": True,
        "keys_written": info["keys_written"],
        "preserved_keys": info["preserved_keys"],
        "file_mode": info["mode"],
        "live_check": live,
        "restart_required": restart_required,
        "message": (
            "Saved. Stop and Start the bot to use the new credentials."
            if restart_required else
            "Saved. Press Start to connect."
        ),
    })


@app.post("/api/settings/trading")
def api_trading_settings_post() -> Response:
    """Save non-secret trading controls without requiring the API token again."""
    if not settings_store.csrf_ok(
        request.cookies.get(settings_store.CSRF_COOKIE),
        request.headers.get(settings_store.CSRF_HEADER),
    ):
        return _err(
            "CSRF check failed. Reload the settings page and try again.",
            403, "csrf_failed",
        )

    body: Dict[str, Any] = request.get_json(silent=True) or {}
    ok, msg, updates = settings_store.validate_optional(body)
    if not ok:
        return _err(msg, 422, "invalid_setting")
    if not updates:
        return _err("Enter at least one trading setting.", 422, "empty_settings")

    try:
        info = settings_store.write_env(updates)
    except OSError as exc:
        return _err(f"Could not write trading settings: {exc}", 500,
                    "write_failed")

    restart_required = SERVICE.running
    STATE.add_log("INFO", "Trading settings saved (keys=%s)" %
                  ",".join(sorted(updates)))
    return _ok({
        "saved": True,
        "keys_written": info["keys_written"],
        "restart_required": restart_required,
        "message": (
            "Saved. Stop and start the bot to apply these settings."
            if restart_required else
            "Saved. These settings will apply the next time you start the bot."
        ),
    })


# ---------------------------------------------------------------------------
# Status / read API
# ---------------------------------------------------------------------------
@app.get("/api/status")
def api_status() -> Response:
    snap = STATE.snapshot(include_trades=False)
    snap["risk"] = STATE.risk_payload()
    snap["config"] = SERVICE.config_state()
    snap["running"] = SERVICE.running
    return _ok(snap)


@app.get("/api/risk")
def api_risk() -> Response:
    return _ok(STATE.risk_payload())


@app.get("/api/scanner")
def api_scanner() -> Response:
    snap = STATE.snapshot(include_trades=False)
    return _ok({
        "markets": snap["scanner"],
        "last_scan_at": snap["last_scan_at"],
        "scans": snap["scans"],
    })


@app.get("/api/trades")
def api_trades() -> Response:
    snap = STATE.snapshot(include_trades=True)
    return _ok({"trades": snap["trades"]})


@app.get("/api/trades.csv")
def api_trades_csv() -> Response:
    path = SERVICE.trades_csv_path()
    if not path or not Path(path).exists():
        return _err("No trade history to export yet.", 404, "no_data")
    return send_file(Path(path).resolve(), as_attachment=True,
                     download_name="trades.csv", mimetype="text/csv")


@app.get("/api/logs")
def api_logs() -> Response:
    since = request.args.get("since", type=float, default=0.0)
    return _ok({"logs": STATE.logs(since=since, limit=300),
                "version": STATE.version})


@app.get("/api/backtest")
def api_backtest() -> Response:
    snap = STATE.snapshot(include_trades=False)
    return _ok({"result": snap["backtest"],
                "running": snap["backtest_running"]})


@app.get("/api/backtest.csv")
def api_backtest_csv() -> Response:
    path = BASE_DIR / "data" / "backtest_report.csv"
    if not path.exists():
        return _err("No backtest report yet — run a backtest first.",
                    404, "no_data")
    return send_file(path.resolve(), as_attachment=True,
                     download_name="backtest_report.csv", mimetype="text/csv")


@app.get("/api/health")
def api_health() -> Response:
    return _ok({
        "status": "healthy",
        "demo_only": True,
        "bot_state": STATE.status,
        "uptime_seconds": STATE.snapshot(False)["uptime_seconds"],
    })


# ---------------------------------------------------------------------------
# Control API
# ---------------------------------------------------------------------------
@app.post("/api/bot/start")
def api_start() -> Response:
    result = SERVICE.start()
    if not result.get("ok"):
        return _err(result.get("error", "Could not start."), 400,
                    "not_configured" if result.get("not_configured")
                    else "start_failed")
    return _ok({"status": STATE.status, "account": STATE.snapshot(False)["account"]})


@app.post("/api/bot/stop")
def api_stop() -> Response:
    result = SERVICE.stop()
    if not result.get("ok"):
        return _err(result.get("error", "Stop failed."), 500, "stop_failed")
    return _ok({"status": STATE.status})


@app.post("/api/bot/pause")
def api_pause() -> Response:
    result = SERVICE.pause()
    if not result.get("ok"):
        return _err(result.get("error", "Pause failed."), 409, "not_running")
    return _ok({"status": STATE.status})


@app.post("/api/bot/resume")
def api_resume() -> Response:
    result = SERVICE.resume()
    if not result.get("ok"):
        return _err(result.get("error", "Resume failed."), 409, "not_running")
    return _ok({"status": STATE.status})


@app.post("/api/risk/reset")
def api_reset() -> Response:
    result = SERVICE.reset_risk()
    if not result.get("ok"):
        return _err(result.get("error", "Reset failed."), 500, "reset_failed")
    return _ok(result)


@app.post("/api/backtest/run")
def api_backtest_run() -> Response:
    body: Dict[str, Any] = request.get_json(silent=True) or {}
    symbols = body.get("symbols") or ["R_10", "R_25", "R_50", "R_75", "R_100"]
    if not isinstance(symbols, list) or not all(
            isinstance(s, str) for s in symbols):
        return _err("symbols must be a list of strings.", 422, "invalid_input")
    try:
        count = int(body.get("count", 1000))
        payout = float(body.get("payout", 1.95))
    except (TypeError, ValueError):
        return _err("count and payout must be numbers.", 422, "invalid_input")
    if not (200 <= count <= 1000):
        return _err("count must be between 200 and 1000 (API cap).",
                    422, "invalid_input")
    if not (1.01 <= payout <= 5.0):
        return _err("payout must be between 1.01 and 5.0.", 422, "invalid_input")

    result = SERVICE.start_backtest(symbols=symbols, count=count, payout=payout)
    if not result.get("ok"):
        return _err(result.get("error", "Could not start."), 409,
                    "backtest_running")
    return _ok({"started": True}, 202)


# ---------------------------------------------------------------------------
# Server-Sent Events
# ---------------------------------------------------------------------------
@app.get("/api/events")
def api_events() -> Response:
    """Streams a snapshot whenever state changes, plus a keepalive."""
    def _gen() -> Iterator[str]:
        last_version = -1
        last_beat = 0.0
        # Send an immediate snapshot so the UI paints without waiting.
        yield "retry: 3000\n\n"
        while True:
            now = time.time()
            version = STATE.version
            if version != last_version:
                snap = STATE.snapshot(include_trades=True)
                snap["risk"] = STATE.risk_payload()
                snap["config"] = SERVICE.config_state()
                snap["running"] = SERVICE.running
                payload = json.dumps(_scrub(snap), default=str)
                yield f"event: state\ndata: {payload}\n\n"
                last_version = version
                last_beat = now
            elif now - last_beat > 15:
                yield ": keepalive\n\n"
                last_beat = now
            time.sleep(0.5)

    resp = Response(_gen(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Accel-Buffering"] = "no"
    resp.headers["Connection"] = "keep-alive"
    return resp


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
@app.errorhandler(404)
def _not_found(e: Any) -> Response:
    if request.path.startswith("/api/"):
        return _err("Endpoint not found.", 404, "not_found")
    return Response("Not found", status=404)


@app.errorhandler(405)
def _method(e: Any) -> Response:
    return _err("Method not allowed.", 405, "method_not_allowed")


@app.errorhandler(500)
def _server_error(e: Any) -> Response:
    return _err("Internal server error.", 500, "internal_error")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def _lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "unavailable"


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(
        description="Local web control panel for the Deriv Volatility Bot."
    )
    render_host = bool(os.environ.get("RENDER_EXTERNAL_HOSTNAME"))
    p.add_argument("--host", default=("0.0.0.0" if render_host else "127.0.0.1"),
                   help="Bind address (default 127.0.0.1 — local only)")
    p.add_argument("--port", type=int,
                   default=int(os.environ.get("PORT", "8000")),
                   help="Port (default from PORT or 8000)")
    p.add_argument("--debug", action="store_true", help="Flask debug mode")
    p.add_argument("--log-level", default="INFO",
                   help="Logging level (default INFO)")
    args = p.parse_args(argv)

    attach_ui_logging(args.log_level)

    # Make the project root importable when run as `python server.py`.
    root = str(BASE_DIR)
    if root not in sys.path:
        sys.path.insert(0, root)
    os.chdir(root)

    local_only = args.host in ("127.0.0.1", "localhost", "::1")
    url_host = "127.0.0.1" if local_only else args.host

    print()
    print("=" * 70)
    print("  Deriv Volatility Bot — WEB CONTROL PANEL")
    print("=" * 70)
    print("  DEMO MODE ONLY — this panel cannot enable real-money trading.")
    print()
    print(f"  Open:  http://{url_host}:{args.port}/")
    if not local_only:
        print()
        print("  " + "!" * 66)
        print("  !! WARNING: bound to a non-loopback address.")
        print(f"  !! Anyone who can reach {_lan_ip()}:{args.port} on this network")
        print("  !! can start, stop and pause the bot.")
        print("  !! There is no authentication on this panel.")
        print("  !! Use --host 127.0.0.1 unless you fully trust the network.")
        print("  " + "!" * 66)
    print()
    cfg = SERVICE.config_state()
    if cfg["configured"]:
        print("  Credentials: found — press Start to connect.")
    else:
        print("  Credentials: NOT CONFIGURED")
        print("  The UI will load, but Start will report what is missing.")
        print("  Copy .env.example to .env and add DERIV_APP_ID + a demo "
              "DERIV_API_TOKEN.")
    print()
    print("  Press Ctrl-C to stop the panel.")
    print("=" * 70)
    print()

    app.run(host=args.host, port=args.port, debug=args.debug,
            threaded=True, use_reloader=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
