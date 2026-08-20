#!/usr/bin/env python3
"""
One-off diagnostic for a real incident (2026-08-19/20): BTCT/BTOG open
positions vanished from the dashboard after a service restart, with no
`get_positions() failed` exception in the logs -- consistent with the
configured account_id simply pointing at an account that doesn't hold
those positions, rather than a bug in WebullBrokerClient.get_positions or
_position_from_dict (both already verified correct against real data, see
client.py's module docstring).

IMPORTANT (found the hard way, first version of this script got this
wrong): this deployment is multi-tenant (2026-08-15 conversion). Each
user's real, live Webull app_key/app_secret/account_id are stored
per-user, encrypted, in the `broker_credentials` DB table (db/models.py's
BrokerCredential) and built into a per-user Settings via
auth/broker_credential_routes.py's build_settings_for_user() --
NOT the WEBULL_APP_KEY/APP_SECRET/ACCOUNT_ID env vars that get_settings()
reads. Those env vars are only a single-tenant fallback that nothing in
the running multi-tenant deployment actually uses. The first version of
this script called get_settings() directly and so queried a completely
different (and apparently unrelated/leftover) Webull sandbox app -- none
of its 5 accounts' numbers matched any of this user's 3 real account
numbers, and none held any positions at all. This version instead reads
every row out of broker_credentials directly and decrypts each one with
this deployment's real CREDENTIAL_ENCRYPTION_KEY, so it queries the exact
same credentials the running TradingLoop actually uses.

Read-only -- lists every account each stored user's credentials can see,
and for each one, whether it currently holds any open positions. No
orders, no state changes, safe to run any time.

Usage: python scripts/find_account_id.py
Needs the same env vars/DB the bot itself runs with (DATABASE_URL,
CREDENTIAL_ENCRYPTION_KEY) -- run this on the VPS, in the same environment
the systemd service uses.
"""
from webull_bot.auth.broker_credential_routes import build_settings_for_user
from webull_bot.brokers import get_broker_client
from webull_bot.config import get_settings
from webull_bot.db.models import BrokerCredential, User
from webull_bot.db.session import session_scope


def _check_user(user: User, cred: BrokerCredential, process_settings) -> None:
    print(f"=== user_id={user.id} email={user.email} trading_mode={cred.trading_mode} ===")
    try:
        user_settings = build_settings_for_user(cred, process_settings)
    except Exception as exc:
        print(f"  could not decrypt/build settings for this user's credentials: {exc!r}\n")
        return

    print(f"  Currently configured account_id: {user_settings.webull.account_id}")

    broker = get_broker_client(user_settings)
    broker.connect()
    if not hasattr(broker, "_require_trade_client"):
        print(f"  {type(broker).__name__} has no _require_trade_client -- skipping (not a WebullBrokerClient).\n")
        return

    trade_client = broker._require_trade_client()
    accounts_response = trade_client.account_v2.get_account_list()
    accounts_response.raise_for_status()
    accounts = accounts_response.json()
    print(f"  get_account_list() returned {len(accounts)} account(s): {accounts}\n")

    for account in accounts:
        account_id = account.get("account_id") or account.get("accountId")
        account_number = account.get("account_number") or account.get("accountNumber")
        marker = "  <-- currently configured" if str(account_id) == str(user_settings.webull.account_id) else ""
        print(f"  Checking account_id={account_id} (account number: {account_number}){marker}...")
        try:
            positions_response = trade_client.account_v2.get_account_position(account_id)
            positions_response.raise_for_status()
            rows = positions_response.json()
        except Exception as exc:
            print(f"    get_account_position raised: {exc!r}")
            continue
        if rows:
            print(f"    *** {len(rows)} open position(s) found: {rows}")
        else:
            print("    (no open positions)")
    print()


def main() -> None:
    process_settings = get_settings()

    with session_scope(process_settings) as session:
        rows = session.query(BrokerCredential, User).join(User, BrokerCredential.user_id == User.id).all()

    if not rows:
        print("No broker_credentials rows found in the DB -- nothing to check.")
        return

    print(f"Found {len(rows)} stored broker credential(s) across all users.\n")
    for cred, user in rows:
        _check_user(user, cred, process_settings)

    print("Whichever account_id above shows BTCT/BTOG's real open positions is the")
    print("correct one -- set it via the dashboard's Settings page (Account ID field)")
    print("and save; that already restarts this user's trading loop with the new")
    print("credentials, no manual `systemctl restart` needed.")


if __name__ == "__main__":
    main()
