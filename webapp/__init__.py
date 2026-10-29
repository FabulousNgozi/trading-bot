"""webapp — the local web control panel layered on top of the trading engine.

The engine (bot.py, strategy.py, risk_manager.py, trade_executor.py,
trade_monitor.py, storage.py) is NOT modified by anything in this package.
The web layer only:

  * owns the lifecycle of a ``TradingBot`` (start / stop / pause / resume),
  * reads published state (signals, risk, contracts) into a thread-safe
    snapshot the browser can poll,
  * exposes HTTP endpoints and Server-Sent Events.

Because the panel is an observer plus a supervisor, every safety property of
the CLI bot still holds here: the same RiskManager gates every purchase, the
same demo-only guard runs at connect time, and there is no code path in this
package that can send a ``buy``.
"""

__all__ = ["state", "bot_service", "server"]
