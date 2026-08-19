# CLAUDE.md

Notes for Claude sessions working on this repo. Each session starts fresh
with no memory of prior conversations, so operational details that live
only in chat history get lost — this file is where they're kept instead.

## VPS deployment

- Repo path on the VPS: `/opt/Openclaw/webull-momentum-bot`
- systemd service: `webull-dashboard.service` — `ExecStart` runs
  `.venv/bin/python scripts/run_dashboard.py --host 0.0.0.0 --port 8000`
  (this single process serves both the dashboard API and the trading
  loop(s) — see `scripts/run_dashboard.py`).
- This session has **no direct shell access to the VPS** (confirmed in
  `docs/ARCHITECTURE.md`'s "Zero-trades incident" writeup, where log
  diagnosis had to go through the user pasting `journalctl` output). A
  code push to GitHub does **not** update what's running on the VPS —
  deploying always means handing the user the commands to run themselves:

  ```bash
  cd /opt/Openclaw/webull-momentum-bot
  git pull origin claude/webull-momentum-trading-bot-qzkn6n
  systemctl restart webull-dashboard
  ```

- To check the service is up and confirm a specific deploy landed:
  ```bash
  systemctl status webull-dashboard
  journalctl -u webull-dashboard -n 100 --no-pager
  curl http://127.0.0.1:8000/api/mis-weights   # e.g. confirm weights_version
  ```
