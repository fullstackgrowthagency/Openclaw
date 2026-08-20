#!/usr/bin/env python3
"""
One-off diagnostic for a real incident (2026-08-19/20): BTCT/BTOG open
positions vanished from the dashboard after a service restart, with no
`get_positions() failed` exception in the logs -- consistent with the
configured WEBULL_ACCOUNT_ID simply pointing at an account that doesn't
hold those positions, rather than a bug in WebullBrokerClient.get_positions
or _position_from_dict (both already verified correct against real data,
see client.py's module docstring). This has happened before: per
docs/ARCHITECTURE.md's 2026-08-12 write-up, a sandbox account reset can
silently swap in a new account_id/account_number under the same app
key/secret, leaving a stale account_id configured until someone finds and
sets the new one.

The account_id used for API calls is NOT the human-readable account number
shown in the Webull app/dashboard -- it's a separate internal ID, only
obtainable via account_v2.get_account_list() for this app's credentials
(see .env.example and dashboard/static/app_settings.html's "Account ID"
field hint). There has never been a committed script for this lookup, only
an ad hoc one-off process; this formalizes that process as a reusable,
read-only tool.

Read-only -- lists every account this app's credentials can see, and for
each one, whether it currently holds any open positions. No orders, no
state changes, safe to run any time.

Usage: python scripts/find_account_id.py
Needs the same env vars the bot itself runs with (WEBULL_APP_KEY/SECRET/
ACCOUNT_ID, TRADING_MODE) -- run this on the VPS, in the same environment
the systemd service uses.
"""
from webull_bot.brokers import get_broker_client
from webull_bot.config import get_settings


def main() -> None:
    settings = get_settings()
    broker = get_broker_client(settings)
    broker.connect()

    print(f"trading_mode={settings.trading_mode.value}")
    print(f"Currently configured account_id: {settings.webull.account_id}\n")

    if not hasattr(broker, "_require_trade_client"):
        print(f"{type(broker).__name__} has no _require_trade_client -- this script only works against WebullBrokerClient.")
        return

    trade_client = broker._require_trade_client()

    accounts_response = trade_client.account_v2.get_account_list()
    accounts_response.raise_for_status()
    accounts = accounts_response.json()
    print(f"get_account_list() returned {len(accounts)} account(s): {accounts}\n")

    for account in accounts:
        account_id = account.get("account_id") or account.get("accountId")
        account_number = account.get("account_number") or account.get("accountNumber")
        marker = "  <-- currently configured" if str(account_id) == str(settings.webull.account_id) else ""
        print(f"Checking account_id={account_id} (account number: {account_number}){marker}...")
        try:
            positions_response = trade_client.account_v2.get_account_position(account_id)
            positions_response.raise_for_status()
            rows = positions_response.json()
        except Exception as exc:
            print(f"  get_account_position raised: {exc!r}")
            continue
        if rows:
            print(f"  *** {len(rows)} open position(s) found: {rows}")
        else:
            print("  (no open positions)")
        print()

    print("Whichever account_id above shows BTCT/BTOG's real open positions is the")
    print("correct one -- set it via the dashboard's Settings page (Account ID field)")
    print("and save; that already restarts this user's trading loop with the new")
    print("credentials, no manual `systemctl restart` needed.")


if __name__ == "__main__":
    main()
