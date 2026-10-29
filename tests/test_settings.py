"""
test_settings.py — the credentials form: validation, atomic write, CSRF.

Every test runs against a temporary .env. Nothing here touches the real
project .env, and no test ever writes a real credential anywhere.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

import pytest

from webapp import settings_store


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def env_file(tmp_path, monkeypatch):
    """Point settings_store at a throwaway .env and clear the env vars."""
    path = tmp_path / ".env"
    monkeypatch.setattr(settings_store, "ENV_PATH", path)
    monkeypatch.setattr(settings_store, "PROJECT_ROOT", tmp_path)
    for key in ("DERIV_APP_ID", "DERIV_API_TOKEN", "STAKE",
                "DAILY_LOSS_LIMIT", "MAX_TRADES_PER_DAY",
                "MAX_CONSECUTIVE_LOSSES", "ENABLED_INDICES"):
        monkeypatch.delenv(key, raising=False)
    return path


@pytest.fixture()
def client(env_file, monkeypatch):
    """Flask test client with the live Deriv check stubbed out."""
    from webapp import server as srv

    monkeypatch.setattr(
        settings_store, "check_with_deriv",
        lambda app_id, token, timeout=20: {
            "ok": True, "demo": True, "accounts": ["demo"],
            "account_count": 1, "demo_count": 1, "error": None,
        },
    )
    srv.app.config["TESTING"] = True
    with srv.app.test_client() as c:
        yield c


def _csrf(client) -> str:
    """Load /settings so the CSRF cookie is set, then read it back."""
    client.get("/settings")
    cookie = client.get_cookie(settings_store.CSRF_COOKIE)
    return cookie.value if cookie else ""


def _post(client, payload, csrf=None, headers=None):
    h = {"Content-Type": "application/json"}
    if csrf is not None:
        h[settings_store.CSRF_HEADER] = csrf
    if headers:
        h.update(headers)
    return client.post("/api/settings", json=payload, headers=h)


GOOD_TOKEN = "AbCdEf1234567890XyZ"      # 19 alnum chars, no placeholder words
GOOD_APP_ID = "123456"
NEW_STYLE_APP_ID = "34BOsdhavC7i5uIM8vb9I"
NEW_STYLE_PAT = "pat_dc765bfb9af098cbb2d6e83933f426909b87ec8d8b9ec6f329904ea3db83f84f"


# ---------------------------------------------------------------------------
# 1. Valid save
# ---------------------------------------------------------------------------
class TestValidSave:
    def test_valid_save_writes_env(self, client, env_file):
        csrf = _csrf(client)
        resp = _post(client, {"app_id": GOOD_APP_ID, "token": GOOD_TOKEN},
                     csrf=csrf)
        assert resp.status_code == 200, resp.get_data(as_text=True)
        body = resp.get_json()
        assert body["ok"] is True
        assert body["data"]["saved"] is True
        assert body["data"]["app_id"] == GOOD_APP_ID

        text = env_file.read_text()
        assert f"DERIV_APP_ID={GOOD_APP_ID}" in text
        assert f"DERIV_API_TOKEN={GOOD_TOKEN}" in text

    def test_file_mode_is_0600(self, client, env_file):
        _post(client, {"app_id": GOOD_APP_ID, "token": GOOD_TOKEN},
              csrf=_csrf(client))
        mode = stat.S_IMODE(env_file.stat().st_mode)
        assert mode == 0o600, f"expected 0600, got {oct(mode)}"

    def test_token_never_returned(self, client):
        resp = _post(client, {"app_id": GOOD_APP_ID, "token": GOOD_TOKEN},
                     csrf=_csrf(client))
        raw = resp.get_data(as_text=True)
        assert GOOD_TOKEN not in raw
        assert "token" not in resp.get_json()["data"] or \
            resp.get_json()["data"].get("token") is None

    def test_optional_settings_saved(self, client, env_file):
        resp = _post(client, {
            "app_id": GOOD_APP_ID, "token": GOOD_TOKEN,
            "stake": "2.5", "daily_loss_limit": "10",
            "max_trades_per_day": "15", "enabled_indices": "10,50,100",
        }, csrf=_csrf(client))
        assert resp.status_code == 200
        text = env_file.read_text()
        assert "STAKE=2.5" in text
        assert "DAILY_LOSS_LIMIT=10" in text
        assert "MAX_TRADES_PER_DAY=15" in text
        assert "ENABLED_INDICES=10,50,100" in text

    def test_get_settings_never_leaks_token(self, client, env_file):
        _post(client, {"app_id": GOOD_APP_ID, "token": GOOD_TOKEN},
              csrf=_csrf(client))
        resp = client.get("/api/settings")
        raw = resp.get_data(as_text=True)
        assert resp.status_code == 200
        assert GOOD_TOKEN not in raw
        data = resp.get_json()["data"]
        assert data["token_present"] is True
        assert data["token_hint"] == "••••••••"   # fixed placeholder only
        assert data["app_id"] == GOOD_APP_ID


# ---------------------------------------------------------------------------
# 2. Invalid App ID
# ---------------------------------------------------------------------------
class TestInvalidAppId:
    def test_accepts_new_style_app_id(self, client):
        resp = _post(client, {"app_id": NEW_STYLE_APP_ID, "token": GOOD_TOKEN},
                     csrf=_csrf(client))
        assert resp.status_code == 200, resp.get_data(as_text=True)

    @pytest.mark.parametrize("bad", ["", "1.5", "-5", "  "])
    def test_rejects_invalid_format(self, client, bad):
        resp = _post(client, {"app_id": bad, "token": GOOD_TOKEN},
                     csrf=_csrf(client))
        assert resp.status_code == 422
        assert resp.get_json()["code"] == "invalid_app_id"

    def test_rejects_legacy_1089(self, client):
        resp = _post(client, {"app_id": "1089", "token": GOOD_TOKEN},
                     csrf=_csrf(client))
        assert resp.status_code == 422
        assert "1089" in resp.get_json()["error"]

    def test_nothing_written_on_failure(self, client, env_file):
        _post(client, {"app_id": "abc", "token": GOOD_TOKEN},
              csrf=_csrf(client))
        assert not env_file.exists()


# ---------------------------------------------------------------------------
# 3. Placeholder / malformed token
# ---------------------------------------------------------------------------
class TestInvalidToken:
    @pytest.mark.parametrize("bad", [
        "invalid_placeholder_token",
        "your_token_here",
        "changeme123456789",
        "short",
        "has spaces in it 123",
        "AbCdEf1234567890XyZ!",     # punctuation
        "",
    ])
    def test_rejects(self, client, bad):
        resp = _post(client, {"app_id": GOOD_APP_ID, "token": bad},
                     csrf=_csrf(client))
        assert resp.status_code == 422
        assert resp.get_json()["code"] == "invalid_token"

    def test_placeholder_message_is_helpful(self, client):
        resp = _post(client, {"app_id": GOOD_APP_ID,
                              "token": "invalid_placeholder_token"},
                     csrf=_csrf(client))
        assert "placeholder" in resp.get_json()["error"].lower()

    def test_validate_token_unit(self):
        assert settings_store.validate_token(GOOD_TOKEN)[0] is True
        assert settings_store.validate_token(NEW_STYLE_PAT)[0] is True
        assert settings_store.validate_token("invalid_placeholder_token")[0] is False
        assert settings_store.validate_token("abc")[0] is False


# ---------------------------------------------------------------------------
# 4. Real-account token rejection
# ---------------------------------------------------------------------------
class TestRealAccountRejected:
    def test_real_only_account_refused(self, client, env_file, monkeypatch):
        monkeypatch.setattr(
            settings_store, "check_with_deriv",
            lambda app_id, token, timeout=20: {
                "ok": True, "demo": False, "accounts": ["real"],
                "account_count": 1, "demo_count": 0, "error": None,
            },
        )
        resp = _post(client, {"app_id": GOOD_APP_ID, "token": GOOD_TOKEN},
                     csrf=_csrf(client))
        assert resp.status_code == 422
        body = resp.get_json()
        assert body["code"] == "real_account_rejected"
        assert "demo-only" in body["error"].lower()
        assert not env_file.exists(), "must not save a real-account token"

    def test_auth_failure_refused(self, client, env_file, monkeypatch):
        monkeypatch.setattr(
            settings_store, "check_with_deriv",
            lambda app_id, token, timeout=20: {
                "ok": False, "demo": None, "accounts": [],
                "error": "Deriv rejected the credentials (401).",
            },
        )
        resp = _post(client, {"app_id": GOOD_APP_ID, "token": GOOD_TOKEN},
                     csrf=_csrf(client))
        assert resp.status_code == 422
        assert resp.get_json()["code"] == "auth_failed"
        assert not env_file.exists()

    def test_skip_live_check_still_saves(self, client, env_file):
        resp = _post(client, {"app_id": GOOD_APP_ID, "token": GOOD_TOKEN,
                              "verify_live": False}, csrf=_csrf(client))
        assert resp.status_code == 200
        assert env_file.exists()


# ---------------------------------------------------------------------------
# 5. Atomic write preserves other keys
# ---------------------------------------------------------------------------
class TestAtomicWrite:
    def test_preserves_unrelated_keys_and_comments(self, client, env_file):
        env_file.write_text(
            "# my hand-written comment\n"
            "DERIV_APP_ID=999\n"
            "DERIV_API_TOKEN=oldtokenvalue12345\n"
            "STAKE=3.0\n"
            "CURRENCY=EUR\n"
            "\n"
            "# trailing note\n"
            "LOG_LEVEL=DEBUG\n"
        )
        resp = _post(client, {"app_id": GOOD_APP_ID, "token": GOOD_TOKEN},
                     csrf=_csrf(client))
        assert resp.status_code == 200

        text = env_file.read_text()
        assert "# my hand-written comment" in text
        assert "# trailing note" in text
        assert "CURRENCY=EUR" in text
        assert "LOG_LEVEL=DEBUG" in text
        assert "STAKE=3.0" in text
        assert f"DERIV_APP_ID={GOOD_APP_ID}" in text
        assert f"DERIV_API_TOKEN={GOOD_TOKEN}" in text
        assert "DERIV_APP_ID=999" not in text
        assert "oldtokenvalue12345" not in text

    def test_no_duplicate_keys(self, client, env_file):
        env_file.write_text("DERIV_APP_ID=1\nDERIV_API_TOKEN=aaaaaaaaaaaaaaaa\n")
        _post(client, {"app_id": GOOD_APP_ID, "token": GOOD_TOKEN},
              csrf=_csrf(client))
        text = env_file.read_text()
        assert text.count("DERIV_APP_ID=") == 1
        assert text.count("DERIV_API_TOKEN=") == 1

    def test_no_tmp_file_left_behind(self, client, env_file):
        _post(client, {"app_id": GOOD_APP_ID, "token": GOOD_TOKEN},
              csrf=_csrf(client))
        leftovers = list(env_file.parent.glob(".env.*.tmp"))
        assert leftovers == [], f"temp files left: {leftovers}"

    def test_creates_env_when_absent(self, client, env_file):
        assert not env_file.exists()
        _post(client, {"app_id": GOOD_APP_ID, "token": GOOD_TOKEN},
              csrf=_csrf(client))
        assert env_file.exists()
        assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


# ---------------------------------------------------------------------------
# 6. Token never in a response body or a log line
# ---------------------------------------------------------------------------
class TestNoLeak:
    def test_token_absent_from_every_response(self, client, env_file, caplog):
        caplog.set_level(logging.DEBUG)
        csrf = _csrf(client)
        responses = [
            _post(client, {"app_id": GOOD_APP_ID, "token": GOOD_TOKEN},
                  csrf=csrf),
            client.get("/api/settings"),
            client.get("/api/status"),
            client.get("/api/health"),
            client.get("/api/risk"),
            client.get("/settings"),
            client.get("/"),
        ]
        for resp in responses:
            raw = resp.get_data(as_text=True)
            assert GOOD_TOKEN not in raw, (
                f"token leaked in {resp.request.path}"
            )

    def test_token_absent_from_logs(self, client, env_file, caplog):
        caplog.set_level(logging.DEBUG)
        _post(client, {"app_id": GOOD_APP_ID, "token": GOOD_TOKEN},
              csrf=_csrf(client))
        for record in caplog.records:
            assert GOOD_TOKEN not in record.getMessage()

    def test_scrub_removes_token_from_error_text(self):
        msg = f"REST 401 for Bearer {GOOD_TOKEN} at wss://x?otp={GOOD_TOKEN}"
        out = settings_store.scrub(msg, GOOD_TOKEN)
        assert GOOD_TOKEN not in out
        assert "REDACTED" in out

    def test_scrub_redacts_bearer_without_known_secret(self):
        out = settings_store.scrub("Authorization: Bearer abcdef1234567890")
        assert "abcdef1234567890" not in out


# ---------------------------------------------------------------------------
# 7. CSRF
# ---------------------------------------------------------------------------
class TestCsrf:
    def test_missing_header_rejected(self, client, env_file):
        _csrf(client)   # cookie present, header absent
        resp = _post(client, {"app_id": GOOD_APP_ID, "token": GOOD_TOKEN})
        assert resp.status_code == 403
        assert resp.get_json()["code"] == "csrf_failed"
        assert not env_file.exists()

    def test_mismatched_header_rejected(self, client, env_file):
        _csrf(client)
        resp = _post(client, {"app_id": GOOD_APP_ID, "token": GOOD_TOKEN},
                     csrf="wrong-token-value")
        assert resp.status_code == 403
        assert not env_file.exists()

    def test_no_cookie_rejected(self, client, env_file):
        resp = _post(client, {"app_id": GOOD_APP_ID, "token": GOOD_TOKEN},
                     csrf="anything")
        assert resp.status_code == 403

    def test_csrf_ok_constant_time_helper(self):
        assert settings_store.csrf_ok("abc", "abc") is True
        assert settings_store.csrf_ok("abc", "abd") is False
        assert settings_store.csrf_ok(None, "abc") is False
        assert settings_store.csrf_ok("abc", None) is False

    def test_settings_page_sets_csrf_cookie(self, client):
        resp = client.get("/settings")
        assert resp.status_code == 200
        cookie = client.get_cookie(settings_store.CSRF_COOKIE)
        assert cookie is not None
        assert len(cookie.value) >= 20


# ---------------------------------------------------------------------------
# 8. Host guard still applies to the settings endpoint
# ---------------------------------------------------------------------------
class TestHostGuard:
    def test_foreign_host_rejected(self, client, env_file):
        csrf = _csrf(client)
        resp = _post(client, {"app_id": GOOD_APP_ID, "token": GOOD_TOKEN},
                     csrf=csrf, headers={"Host": "evil.example.com"})
        assert resp.status_code == 403
        assert resp.get_json()["code"] == "host_not_allowed"
        assert not env_file.exists()

    def test_settings_page_foreign_host_rejected(self, client):
        resp = client.get("/settings", headers={"Host": "evil.example.com"})
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# 9. Pages render
# ---------------------------------------------------------------------------
class TestPages:
    def test_settings_page_has_banner_and_nav(self, client):
        html = client.get("/settings").get_data(as_text=True)
        assert "DEMO MODE ONLY" in html
        assert 'href="/settings"' in html
        assert 'href="/"' in html
        assert 'type="password"' in html
        assert "DERIV_APP_ID" in html
        assert "DERIV_API_TOKEN" in html

    def test_dashboard_has_settings_link(self, client):
        html = client.get("/").get_data(as_text=True)
        assert 'href="/settings"' in html
        assert "DEMO MODE ONLY" in html

    def test_settings_page_has_no_token_value(self, client, env_file):
        env_file.write_text("DERIV_APP_ID=123456\n"
                            "DERIV_API_TOKEN=AbCdEf1234567890XyZ\n")
        html = client.get("/settings").get_data(as_text=True)
        assert "AbCdEf1234567890XyZ" not in html
