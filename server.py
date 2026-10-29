"""
server.py — start the local web control panel.

    python server.py                 # http://127.0.0.1:8000
    python server.py --port 9000

This is a thin launcher. The implementation lives in ``webapp/``:

    webapp/server.py       Flask app, REST endpoints, SSE stream
    webapp/bot_service.py  runs the unmodified TradingBot in a thread
    webapp/state.py        thread-safe state shared with HTTP handlers

Nothing here touches the trading engine.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    from webapp.server import main as _main
    return _main()


if __name__ == "__main__":
    raise SystemExit(main())
