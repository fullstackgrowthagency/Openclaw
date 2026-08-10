"""
RVOL baseline: a per-symbol "what's typical volume by this point in the
session" reference, built from the SAME raw bars BroadScanner already
fetches for volume-profile resistance (WebullBrokerClient.get_raw_bars) --
no extra network call. This is what relative_volume/relative_volume_1m/
relative_volume_5m in metrics/rolling.py compare today's activity against;
without it, they fall back to a neutral default (see that module's
docstring) and the corresponding Momentum Ignition Score components always
read a flat 0, regardless of session, entirely independent of how correct
cumulative_volume itself is.

Built from Webull's own historical bars rather than accumulated from this
bot's own tick history over time, deliberately: a low-float momentum mover
is very often a symbol this bot has never watched before, and RVOL matters
most on exactly that first hot day -- a baseline that only builds up after
weeks of the bot running would have nothing to compare against precisely
when it's needed most.

**Cumulative volume is not one smooth curve across the day** -- it resets
at the pre-market/regular-session boundary and again at the regular/
after-hours boundary (see WebullBrokerClient._snapshot_from_dict's
ext_price/ext_volume handling). A baseline built as a single "minutes since
midnight" curve would compare today's regular-session-only volume against a
historical figure that also includes pre-market, understating RVOL for the
entire regular session. Instead, this tracks three independently-reset
curves -- PRE (4:00am-9:30am ET), RTH (9:30am-4:00pm ET), and ATH
(4:00pm-8:00pm ET) -- and a live lookup only ever compares against the
curve for its own phase, matching the same reset behavior the live
cumulative_volume already has.

Session boundary values (4:00am/8:00pm) are Webull's documented standard
pre-market/after-hours window, not independently confirmed live for this
account -- same "reasonable default, not verified" status as most of this
project's other unvalidated starting points (scoring/weights.yaml,
metrics/volume_profile.py's bucket count, etc.).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from typing import Optional
from zoneinfo import ZoneInfo

_EASTERN = ZoneInfo("America/New_York")
_PRE_START = time(4, 0)
_RTH_START = time(9, 30)
_ATH_START = time(16, 0)
_ATH_END = time(20, 0)


def _parse_bar_time(raw_time) -> datetime:
    if isinstance(raw_time, str):
        return datetime.strptime(raw_time, "%Y-%m-%dT%H:%M:%S.%f%z").replace(tzinfo=None)
    return raw_time


def _phase_and_bucket(timestamp_utc: datetime, bucket_minutes: int) -> Optional[tuple[str, int]]:
    """Classifies a naive-UTC timestamp into (phase, elapsed-minutes-into-
    phase rounded down to bucket_minutes), or None if it falls in none of
    PRE/RTH/ATH (e.g. overnight, midnight-4am ET) -- there's no baseline
    coverage for that window, matching _is_outside_regular_session's same
    "pre/after-market only, overnight deliberately excluded" scope in
    brokers/webull/client.py."""
    eastern_dt = timestamp_utc.replace(tzinfo=timezone.utc).astimezone(_EASTERN)
    t = eastern_dt.time()
    if _PRE_START <= t < _RTH_START:
        phase, start = "PRE", _PRE_START
    elif _RTH_START <= t < _ATH_START:
        phase, start = "RTH", _RTH_START
    elif _ATH_START <= t < _ATH_END:
        phase, start = "ATH", _ATH_START
    else:
        return None
    phase_start_dt = datetime.combine(eastern_dt.date(), start, tzinfo=_EASTERN)
    elapsed_minutes = (eastern_dt - phase_start_dt).total_seconds() / 60.0
    bucket = int(elapsed_minutes // bucket_minutes) * bucket_minutes
    return phase, bucket


@dataclass(frozen=True)
class VolumeBaseline:
    bucket_minutes: int
    # (phase, bucket) -> average CUMULATIVE volume from phase start through
    # this bucket, averaged across every historical day that had data there.
    typical_cumulative: dict[tuple[str, int], float] = field(default_factory=dict)
    # (phase, bucket) -> average volume WITHIN this one bucket alone (i.e.
    # this specific bar's own volume, averaged across days) -- used to
    # derive typical_volume_5m directly, and typical_volume_1m
    # approximately (see lookup()).
    typical_bucket_volume: dict[tuple[str, int], float] = field(default_factory=dict)

    def lookup(self, timestamp_utc: datetime) -> tuple[Optional[float], Optional[float], Optional[float]]:
        """Returns (typical_volume_same_time, typical_volume_1m,
        typical_volume_5m) for `timestamp_utc` -- the exact three
        parameters metrics/rolling.py's compute_metrics accepts. All three
        are None if this timestamp falls outside PRE/RTH/ATH, or if no
        historical day had data at this exact bucket (e.g. a bucket right
        at the open for a symbol that historically never had volume that
        early).

        typical_volume_1m is only an approximation: the underlying bars are
        5-minute (bucket_minutes) granularity, so there's no way to recover
        a true historical 1-minute figure -- this assumes volume is spread
        uniformly across the bucket (typical_bucket_volume / bucket_minutes),
        which is the best available estimate without tick-level historical
        data, not a precise historical 1-minute rate."""
        key = _phase_and_bucket(timestamp_utc, self.bucket_minutes)
        if key is None:
            return None, None, None
        typical_same_time = self.typical_cumulative.get(key)
        typical_5m = self.typical_bucket_volume.get(key)
        typical_1m = (typical_5m / self.bucket_minutes) if typical_5m is not None else None
        return typical_same_time, typical_1m, typical_5m


def compute_volume_baseline(
    bars: list[dict], *, bucket_minutes: int = 5, now: Optional[datetime] = None
) -> VolumeBaseline:
    """Builds a VolumeBaseline from raw per-bar OHLCV (Webull's native
    shape, same input as metrics/volume_profile.py's compute_volume_profile
    -- most-recent-first or chronological, order doesn't matter here).

    Excludes today (per `now`, naive UTC) entirely: a day still in progress
    would otherwise leak its own still-forming numbers into its own
    baseline, and -- for the PRE/RTH boundary specifically -- would compare
    live pre-market activity against a "today" data point that only exists
    because that same activity already happened, trivially inflating the
    baseline right when it matters most."""
    if not bars:
        return VolumeBaseline(bucket_minutes=bucket_minutes)

    now = now or datetime.utcnow()
    today_et = now.replace(tzinfo=timezone.utc).astimezone(_EASTERN).date()

    # (calendar day, phase) -> {bucket: volume in that bucket that day}.
    # Keyed by phase too so a single day contributes independently-reset
    # cumulative curves for its PRE/RTH/ATH portions, not one blended curve.
    per_day_phase_buckets: dict[tuple[date, str], dict[int, float]] = defaultdict(dict)

    for bar in bars:
        bar_time = _parse_bar_time(bar["time"])
        eastern_date = bar_time.replace(tzinfo=timezone.utc).astimezone(_EASTERN).date()
        if eastern_date >= today_et:
            continue
        key = _phase_and_bucket(bar_time, bucket_minutes)
        if key is None:
            continue
        phase, bucket = key
        day_key = (eastern_date, phase)
        per_day_phase_buckets[day_key][bucket] = per_day_phase_buckets[day_key].get(bucket, 0.0) + float(bar["volume"])

    cumulative_samples: dict[tuple[str, int], list[float]] = defaultdict(list)
    bucket_volume_samples: dict[tuple[str, int], list[float]] = defaultdict(list)

    for (_day, phase), bucket_map in per_day_phase_buckets.items():
        running = 0.0
        for bucket in sorted(bucket_map):
            running += bucket_map[bucket]
            cumulative_samples[(phase, bucket)].append(running)
            bucket_volume_samples[(phase, bucket)].append(bucket_map[bucket])

    typical_cumulative = {key: sum(values) / len(values) for key, values in cumulative_samples.items()}
    typical_bucket_volume = {key: sum(values) / len(values) for key, values in bucket_volume_samples.items()}

    return VolumeBaseline(
        bucket_minutes=bucket_minutes,
        typical_cumulative=typical_cumulative,
        typical_bucket_volume=typical_bucket_volume,
    )
