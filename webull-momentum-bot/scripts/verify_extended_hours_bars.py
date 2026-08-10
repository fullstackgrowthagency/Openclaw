#!/usr/bin/env python3
"""
One-off live check for an assumption flagged as "inferred, not confirmed"
in WebullBrokerClient.get_raw_bars: that passing
trading_sessions=["PRE", "RTH", "ATH"] to the SDK's get_history_bar
actually returns pre-market/after-hours bars, not just the regular
9:30am-4:00pm ET session (the values themselves were borrowed from a
different endpoint's docstring, get_footprint, since get_history_bar's own
docstring never lists accepted values).

Only meaningful to run during an actual pre-market (before 9:30am ET) or
after-hours (after 4:00pm ET) window on a trading day -- outside those
windows there's nothing extended-hours for the extra bars to prove. Needs
real sandbox credentials configured (WEBULL_APP_KEY/SECRET/ACCOUNT_ID) --
this hits the live Webull sandbox API, same as running the bot itself.

Usage: python scripts/verify_extended_hours_bars.py [SYMBOL]
  (defaults to AAPL if no symbol given -- pick a low-float name you expect
  to actually have pre/after-market volume today for a more meaningful check)
"""
import sys
from datetime import datetime, time
from zoneinfo import ZoneInfo

from webull_bot.brokers.webull.client import WebullBrokerClient
from webull_bot.config import get_settings

_EASTERN = ZoneInfo("America/New_York")
_MARKET_OPEN = time(9, 30)
_MARKET_CLOSE = time(16, 0)


def _parse_bar_time(raw: str) -> datetime:
    return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S.%f%z")


def main() -> None:
    symbol = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    client = WebullBrokerClient(get_settings())

    print(f"Fetching raw 5m bars for {symbol} (trading_sessions=PRE/RTH/ATH)...")
    bars = client.get_raw_bars(symbol, "5m", 200)
    if not bars:
        print("No bars returned -- can't verify anything.")
        return

    print(f"Got {len(bars)} bars.\n")

    extended_bars = []
    for bar in bars:
        eastern_time = _parse_bar_time(bar["time"]).astimezone(_EASTERN)
        if not (_MARKET_OPEN <= eastern_time.time() < _MARKET_CLOSE):
            extended_bars.append((eastern_time, bar))

    times = sorted(_parse_bar_time(b["time"]).astimezone(_EASTERN) for b in bars)
    print(f"Earliest bar (ET): {times[0]}")
    print(f"Latest bar (ET):   {times[-1]}\n")

    if extended_bars:
        print(f"CONFIRMED: {len(extended_bars)} bars fall outside 9:30am-4:00pm ET -- "
              f"trading_sessions=PRE/RTH/ATH is actually returning extended-hours data.")
        print("Sample extended-hours bars:")
        for eastern_time, bar in sorted(extended_bars)[:5]:
            print(f"  {eastern_time}  high={bar['high']}  volume={bar['volume']}")
    else:
        print("NOT CONFIRMED: every returned bar falls inside 9:30am-4:00pm ET. Either:")
        print("  - it's currently regular trading hours and no extended-hours bars exist yet to fetch, or")
        print("  - the trading_sessions=[\"PRE\",\"RTH\",\"ATH\"] value guess is wrong and Webull silently")
        print("    ignored it, falling back to the RTH-only default.")
        print("Re-run this during an actual pre-market/after-hours window to tell the two apart.")


if __name__ == "__main__":
    main()
