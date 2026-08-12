# Architecture

## Data flow

```
BroadScanner            (structural gates: price range, float ceiling, and a
      |                   volume floor -- any ONE of avg-daily, previous-day,
      |                   or current-day volume clearing its bar is enough;
      |                   dollar volume remains informational, not a gate)
      v  Candidate(DISCOVERED -> WATCHING)
CandidateWatcher         (recomputes MomentumMetrics + Momentum Ignition
      |                   Score on every snapshot; drives WATCHING ->
      |                   HEATING_UP -> ARMED; spread/liquidity failures set
      |                   a temporary trade_eligible=False, not REJECTED)
      v
TriggerEngine             (only looks at ARMED candidates; asks each
      |                    Strategy for an entry Signal on real-time data)
      v  Signal
RiskEngine.evaluate()      (deterministic: notional-only position sizing,
      |                     exposure/position/trade-count caps, spread/
      |                     liquidity gates, cooldowns, kill switch)
      v  RiskDecision(approved, max_shares)
OrderManager                (the ONLY thing allowed to call the broker)
      |
      v
BrokerClient                (PaperBrokerClient for fully local sim, or
                              WebullBrokerClient against the real sandbox --
                              picked by TradingMode via brokers/__init__.py)
```

Exits follow a parallel, shorter path: `PositionManager.check_exit` emits an
EXIT/SCALE_OUT `Signal`, which `OrderManager` routes straight to the broker
**without** the entry-sizing risk gates (spread/liquidity checks exist to
control new exposure, not to trap the bot in a losing position).

**Hard rule enforced in code, not just convention:** `Strategy` classes hold
no broker reference. If new code calls `broker.place_order` from anywhere
outside `execution/order_manager.py`, that's a bypass of the architecture --
don't do it, even for "just a quick script."

## Production run-loop (polling, not streaming)

`runtime/trading_loop.py`'s `TradingLoop` is what actually drives the data
flow above outside of a backtest -- `main.py` constructs one and calls
`run_forever()`. Since Webull streaming isn't implemented, it polls
`broker.get_snapshot()` per tracked candidate on a timer
(`TradingLoopConfig.poll_interval_seconds`) instead of reacting to pushed
ticks, and periodically re-runs `BroadScanner.scan()` against a
`SymbolUniverseProvider` to discover new candidates
(`universe_rescan_interval_seconds`).

**Concurrency: rescanning runs on its own background thread, decoupled from
candidate/position processing.** A full universe rescan now routinely takes
many minutes (see "Universe size is no longer bounded" below) -- long enough
that running it inline on the same loop as candidate/position processing
would starve everything else waiting behind it, including live stop-loss/
exit management on open positions, for the rescan's entire duration. That
used to be exactly how this worked and was a real gap, not a hypothetical
one, for a bot that can be holding an open position while a scan is running.
`run_forever()` fixes this by spawning `_universe_rescan_loop` on a daemon
thread (repeating the rescan back-to-back, since
`universe_rescan_interval_seconds` is a floor, not an idle wait -- a scan
almost always takes longer than the configured interval) while the main
thread runs `_process_all_candidates()` back-to-back on its own tight
`poll_interval_seconds` cadence, completely independent of how long the
current rescan is taking. Both threads touch the shared `self.candidates`
dict (the rescan thread inserts newly discovered candidates; the main
thread iterates and mutates existing ones), so all access goes through
`self._candidates_lock`, held only briefly to copy or insert into the dict
itself -- never across a network call or a full candidate-processing pass.
`run_once()` itself is unchanged: it still rescans inline, synchronously,
on the caller's own thread, for backward compatibility with callers
(mainly tests) that call it directly and expect one deterministic pass --
`run_forever()` does not call `run_once()` at all anymore, it calls
`_process_all_candidates()` and the rescan loop separately.

The one thing this loop has to handle that the backtest engine doesn't:
**`WebullBrokerClient.place_order` returns `status=SUBMITTED`, not
`FILLED`** (confirmed live -- a 2xx response means Webull accepted the
order for processing, not that it has executed). `PaperBrokerClient`, by
contrast, fills synchronously. So every entry and exit order goes through a
pending-order tracking step (`_pending_entry_orders` / `_pending_exit_orders`)
that polls `OrderManager.get_status()` on subsequent ticks until it
resolves to `FILLED` or a terminal failure, rather than assuming the order
in front of it already executed.

The other non-obvious bit: `TradingLoop` keeps its own `_positions` dict as
the source of truth for an open position's stop/target/trailing-stop/MFE/MAE
state, seeded once (with a real `avg_entry_price` from `broker.get_positions()`)
right when an entry fill is confirmed. It deliberately does NOT re-fetch
`broker.get_positions()` on every tick to read that state back, because
`PositionManager.check_exit` mutates the Position object it's given
in place, and the broker returns a fresh object on every call -- refetching
every tick would silently discard the running trailing-stop/MFE/MAE state
computed on prior ticks.

**Batched snapshot fetching** (`WebullBrokerClient.get_snapshots`,
`TradingLoop._process_all_candidates`): `_process_all_candidates` used to
call `broker.get_snapshot()` once per tracked candidate every
`poll_interval_seconds` cycle -- a real problem in practice, confirmed
against a live case (2026-08-10, see "RVOL historical baseline" section):
every `get_snapshot`-family call shares the same globally-paced
`retry.webull_market_data_limiter` (~1 req/s sustained, regardless of
concurrency), so N tracked candidates meant a real >=N-second floor on how
often any single candidate's tick actually refreshed -- tens of seconds of
staleness once the candidate list (now fed by 7 discovery sources) grows
past a handful of names, for a bot whose whole premise is reacting to
fast-moving low-float names. `get_snapshot`'s own underlying SDK method
already accepts a symbol list ("For each request, up to 100 symbols can be
subscribed"), so `get_snapshots(symbols)` batches every symbol needing a
snapshot this cycle into as few chunked calls as possible (chunked at that
100-symbol cap) instead of one call per candidate -- each chunk still
costs exactly one rate-limited request regardless of how many symbols ride
along in it, so N candidates now cost `ceil(N/100)` calls instead of N.
`_process_all_candidates` computes the exact symbol list needing a
snapshot this cycle (every candidate not `REJECTED`/`COOLDOWN`, mirroring
`_process_candidate_inner`'s own skip conditions), calls `get_snapshots`
once up front, and passes each candidate its pre-fetched snapshot via a
new `prefetched_snapshot` parameter on `_process_candidate`/
`_process_candidate_inner` -- `None` (the default) preserves the exact
original per-candidate `get_snapshot()` call, which is also what happens
automatically when the broker doesn't support batching at all (paper/
backtest mode has no rate limit to work around) or when the batch call
itself raises: `get_snapshots` is deliberately NOT part of the
`BrokerClient` interface, checked via `getattr` and caught in a broad
`try/except` around the whole batch call, same "optional Webull-specific
capability" pattern as `get_raw_bars`/`get_daily_volumes` elsewhere in this
codebase, so a batch failure degrades to the pre-batching behavior for that
one cycle rather than skipping every candidate's tick.

**The same batching applies to discovery, not just already-tracked
candidates** (`BroadScanner.scan`): `check_symbol_verbose` calls
`get_snapshot` as its very first action for *every* symbol in the universe,
before any structural gate runs -- with the universe now routinely in the
hundreds (seven discovery sources, unbounded pagination per source), that
was the dominant cost of a full scan, not `max_workers`' concurrency
(concurrency helps overlap Webull's paced calls with the separate float-
provider lookup, but every Webull call still queues on the same limiter
regardless of how many threads are running). `scan()` now batch-fetches
snapshots for the entire `symbol_universe` list up front via the same
`get_snapshots`, before spinning up the thread pool, and hands each
per-symbol worker (`_check_symbol` -> `check_symbol_verbose`, both now
accepting the same `prefetched_snapshot` parameter) its own pre-fetched
snapshot instead of making an individual call. This fetches nothing extra
-- it's the exact same set of symbols that would have been fetched one at a
time regardless -- just far fewer round-trips to do it: a 500-symbol
universe drops from ~500 rate-limited calls to 5. Same fallback
guarantees as the live-tick case: no `get_snapshots` support, or the batch
call itself raising, falls back to each symbol making its own
`get_snapshot()` call, exactly this method's pre-batching behavior. The
dashboard's on-demand single-ticker scan (`check_symbol_verbose` called
directly, never through `scan()`) is unaffected either way -- there's
only ever one symbol, so batching has nothing to save there.

`data/universe.py` feeds the scanner from **seven** independent Webull
screener sources, combined by `MultiSourceUniverseProvider`. The first
four are live-verified; the last three (pre-market, after-hours, and
amplitude) are not -- see the note after them.

- `WebullUniverseProvider` with `rank_type="RELATIVE_VOLUME_10D"` -- the
  project's "high relative volume" criterion directly.
- The same `WebullUniverseProvider` class again with `rank_type="TURNOVER_RATE"`
  (% of a stock's float traded today) -- confirmed live to surface a
  meaningfully different, generally more extreme set of names than relative
  volume, and conceptually the same thing as the `float_turnover` metric the
  MIS already computes, just used as a discovery filter instead of a score
  input.
- `WebullGainersLosersUniverseProvider` wrapping `screener.get_gainers_losers(rank_type="DAY_1", sort_by="CHANGE_RATIO", direction="DESC")`
  -- today's top % price movers. Note `get_gainers_losers`'s `rank_type` is
  a **time period** (`DAY_1`, `MIN_5`, `WEEK_52`, etc.), unrelated to
  `get_most_active`'s `rank_type` despite the shared parameter name --
  confirmed against the live SDK, not assumed from the naming.
- The same `WebullGainersLosersUniverseProvider` class again with
  `rank_type="MIN_5"` -- confirmed live (2026-08-09) this is a genuine,
  distinct 5-minute price-change ranking (not just DAY_1 recomputed over a
  shorter window: a real pull surfaced names up double-digit % in just the
  last 5 minutes that weren't yet among today's overall leaders). This is
  Webull's real equivalent of "most active last 5 minutes" for *price*.
  Also confirmed live: there is **no** equivalent 5-minute *volume*
  ranking anywhere in the API -- `get_gainers_losers`' `volume` field is
  always the whole day's cumulative volume regardless of `rank_type`, and
  `get_most_active` has no time-windowed `rank_type` at all. A synthetic
  volume-based 5-minute signal is computed per-candidate instead, once a
  symbol is already being watched (`MomentumMetrics.volume_5m`/
  `float_velocity_5m` in `metrics/rolling.py`), rather than invented from
  a screener endpoint that doesn't exist.
- **5th/6th sources, added later, NOT live-verified**: the same
  `WebullGainersLosersUniverseProvider` class again with
  `rank_type="PRE_MARKET"` and `rank_type="AFTER_MARKET"` -- catches a
  name already igniting before the regular session opens or after it
  closes, which none of the four sources above can see at all (they only
  rank regular-session activity). `PRE_MARKET`/`AFTER_MARKET` are
  documented Webull `rank_type` values for this endpoint but this specific
  pull was never confirmed live the way `DAY_1`/`MIN_5` were -- treat the
  field names/behavior as inferred from the shared response shape, not
  verified.
- **7th source, added later, NOT live-verified**: `WebullUniverseProvider`
  again with `rank_type="AMPLITUDE"` -- today's price amplitude (high-low
  range), a volatility-based ranking distinct from every volume/turnover/
  change-ratio source above. Catches a name whipsawing in a wide range
  even if its net change or relative volume alone wouldn't stand out yet.
  Same caveat: `AMPLITUDE` is a documented `rank_type` on `get_most_active`,
  but neither this specific rank_type nor the `amplitude` field name
  assumed for its pagination threshold (`_page_below_rank_threshold`) have
  been confirmed against a live response.

Each source **paginates** rather than taking a single fixed-size page
(confirmed live: both endpoints accept `page_index`/`page_size` and return
a `has_more` flag, with non-overlapping pages). Blindly paginating to
`has_more=False` can go very deep -- confirmed live still `True` 1000+ rows
into `RELATIVE_VOLUME_10D` -- so pagination stops at whichever comes first:
an empty page, `has_more=False`, a value-based rank threshold (results are
sorted descending by the ranking field, so a page dropping below the
threshold means every later page is even less relevant), or a `max_pages`
safety valve -- see `data/universe.py`'s `_paginate_screener`/
`_page_below_rank_threshold`.

**The value-based threshold isn't optional in practice -- confirmed live
(2026-08-09) that skipping it is a real API-call explosion, not a
theoretical one.** `RELATIVE_VOLUME_10D` shipped with one from the start
(reusing `scoring/weights.yaml`'s `min_relative_volume_for_watch: 2.0`)
and landed at a reasonable 301 symbols. `TURNOVER_RATE`/`DAY_1`/`MIN_5`
initially shipped without one, relying on `max_pages` as the only
backstop -- live-tested, each paginated all the way to `max_pages` (2000
raw results) and still hadn't dropped below any meaningful activity
level, landing 950-1100+ symbols *each*. That's `max_pages` firing as the
*normal* operating point instead of the rare circuit-breaker it was
designed to be -- across 4 sources, thousands of symbols/cycle at
`BroadScanner`'s ~1.25-2.86s/symbol per Webull call is a real problem, not
a rounding error. Fixed by adding first-pass calibrated thresholds to all
three (`main.py`'s `build_trading_loop`): `turnover_rate >= 0.10` (10% of
float traded that day), `change_ratio >= 0.10` for `DAY_1` (10% full-day
move), `change_ratio >= 0.05` for `MIN_5` (a lower bar than DAY_1
deliberately -- the same % move packed into 5 minutes instead of a full
day is a much more intense signal, so requiring DAY_1's bar there would
miss real ignition moves). Re-verified live with these in place: 301/146/195/88
per source, **547 unique symbols combined** after dedup -- a real,
substantial increase over the old fixed-100-per-source cap (~150
combined previously), at a real, substantial increase in per-cycle scan
time, not an explosion. These three new thresholds are first-pass
estimates from the live decay curves observed that day, not backtested --
treat them the same as `scoring/weights.yaml`'s "unvalidated starting
point" framing, not settled values.

**The PRE_MARKET/AFTER_MARKET/AMPLITUDE thresholds (0.15/0.15/10.0) carry
the same warning, one level further removed**: they weren't set from an
observed live decay curve at all (unlike the four above), since the
sources themselves were never live-tested -- they're a first guess at
"probably high enough to avoid the same max_pages-firing-as-normal-
operating-point problem," reasoned from the pattern of the four verified
ones, not measured. Live-test these three the same way the original four
were before trusting the exact cutoffs, and don't be surprised if the
`amplitude` field-name guess for the 7th source's pagination threshold
turns out wrong (in which case it just falls back to the `max_pages`
safety valve, per that source's own comment in `main.py`).

`MultiSourceUniverseProvider` is a plain union (every source queried every
cycle, not a priority fallback chain) with per-source failure isolation --
one source raising is logged and skipped rather than aborting the scan, so
a broken/rate-limited source never destroys results already gathered from
the others. A symbol only needs to appear on one list to reach
`BroadScanner`, which vets every symbol identically regardless of which
list(s) surfaced it -- **price range, real free float via FMP, and a
volume floor are the structural pass/fail gates** (see "Volume floor"
below for the volume one). Dollar volume remains informational only (see
`scanner/broad_scanner.py`'s module docstring). `TradingLoop._rescan_universe`
scans **every** symbol this
returns -- there is deliberately no truncation, so a mover can never be
silently dropped for appearing past some cutoff. (An earlier version of
this class interleaved results round-robin across sources specifically to
stop whichever source came first from filling a truncated cap before the
others contributed anything; once truncation was removed, that
interleaving had nothing left to protect against and was simplified away
-- see `data/universe.py`'s `MultiSourceUniverseProvider` docstring.)

**Webull's sandbox has a real sustained rate limit, confirmed live
(2026-08-08) across several rounds of testing, each round correcting the
last:**
1. A burst test (10 concurrent `get_snapshot` calls) only tripped 2 429s
   and looked like a non-issue.
2. A follow-up sustained-interval test showed the real ceiling is close to
   a flat **~1 request/second regardless of concurrency** (0.5s spacing =
   0/20 errors, 0.3s spacing = 6/20 errors).
3. A real 149-symbol scan using reactive retry alone (no pacing) hit 101
   rate-limit errors and didn't finish inside a 60s timeout -- retry-with-
   backoff can't paper over sustained overload once retries themselves add
   load back into an already-saturated window.
4. A first `RateLimiter` fix paced only the *first* attempt of each call
   (call sites did `limiter.wait()` once, then `call_with_retry(fn)`). At
   both 0.6s and 1.0s spacing with 10 concurrent workers (`BroadScanner`'s
   real config), 9 of 60 calls still failed outright -- raising the
   interval made no difference, because retries triggered inside
   `call_with_retry` ran on their own backoff timer, entirely outside the
   pacer, and could still stack up across threads and re-trigger each
   other's 429s.
5. The actual fix: `call_with_retry` now calls the shared
   `webull_market_data_limiter` before *every* attempt, not just the
   first, so a retry queues on the same global pacer as any other call.
   Re-verified live end-to-end afterward: 100 real symbols from
   `MultiSourceUniverseProvider`, 10 concurrent workers, against the actual
   sandbox -- **zero hard failures** (a handful of individual 429s, all
   recovered by the now-paced retry), completing in 124.9s (~1.25s/symbol
   including retry overhead, vs. the limiter's own 1.0s interval).

`BroadScanner` issues one paced Webull call per universe symbol that
clears the structural price gate (a second once
`_compute_average_volume_info` below runs, a third for resistance
analysis), so scanning N symbols takes roughly `N * (per-symbol cost)` of
Webull-bound time no matter how many worker threads are checking symbols
concurrently -- more workers only let FMP float lookups overlap with
that, they can't make Webull itself go faster. There is no cap on N (see
`TradingLoop._rescan_universe` above): every symbol the multi-source
universe returns gets scanned, unless it's already a tracked candidate
(see the cost-optimization list below), so full-scan duration scales with
however large that universe's *new* discoveries are on a given cycle
rather than being bounded by a fixed number. `TradingLoopConfig.universe_rescan_interval_seconds` is
sized as a floor between scan *starts*, not a target duration, given that
a full scan now routinely takes longer than any reasonable interval --
see that config's comments for the measured numbers (and their caveat:
they predate the wider price range, pagination, and 4th source, so they
understate current scan time).

**Two cost optimizations (2026-08-09), both purely eliminating wasted
work with no coverage/behavior tradeoff:**

1. **`_rescan_universe` skips already-tracked symbols before calling
   `BroadScanner.scan()`.** Previously every symbol the discovery sources
   returned got the full per-symbol cost (up to 3 paced Webull calls) on
   *every* rescan pass, even ones already sitting in `self.candidates` --
   the insert loop right after `scan()` has always silently discarded
   those results anyway (`if candidate.symbol not in self.candidates`).
   Filtering the symbol list down to genuinely new discoveries before
   `scan()` runs means that cost is only ever paid once per symbol, not
   once per cycle for as long as it keeps appearing on a discovery list.
   Nothing is lost: an already-tracked candidate's score/state/exit
   management is driven entirely by `_process_all_candidates` on its own
   5-second cadence (see the "Concurrency model" section of
   `runtime/trading_loop.py`'s module docstring), not by this discovery
   pass, so it keeps getting checked exactly as often either way.
2. **`BroadScanner.check_symbol_verbose` skips the `get_daily_volumes`
   call when current-day volume alone already clears its floor.** Since
   the volume floor only rejects when ALL THREE metrics are missed (see
   below), a symbol whose `snapshot.cumulative_volume` already clears
   `min_current_day_volume` is guaranteed to survive regardless of what
   `average_daily_volume`/`previous_day_volume` would turn out to be --
   so that network call (and the retry/rate-limit cost it can carry) is
   skipped entirely in that case. `average_daily_volume`/`previous_day_volume`
   simply stay `None` on the resulting candidate then, the same
   already-handled case as a broker with no daily-volume history at all
   (paper/backtest mode).

**Volume floor** (`BroadScanner._fails_volume_floor`, fed by
`_compute_average_volume_info` plus the snapshot already in hand): a
symbol is rejected only when ALL THREE of `average_daily_volume` (below
`BroadScannerConfig.min_average_daily_volume`, 500,000 by default),
`previous_day_volume` (below `min_previous_day_volume`, 750,000 by
default), and `current_day_volume` (today's volume-so-far --
`snapshot.cumulative_volume`, below `min_current_day_volume`, 500,000 by
default) are missed -- clearing any single one of the three alone is
enough to survive. This is a re-introduction: an earlier pass through
this file (documented just above, in spirit) had made average/previous-
day volume purely informational, reasoning that a previously-quiet float
suddenly seeing abnormal volume is exactly the pattern this bot targets.
Per explicit user request (2026-08-09) the gate is back, but looser (the
original hard rejection was ≥1,000,000 shares/day with no exemption) and
with a three-way either-or exemption instead of an all-or-nothing bar --
first added as a two-way (average/previous-day) exemption, then widened
same-day to add current-day volume as a third escape hatch, again per
explicit user request. This keeps most of the original informational-only
reasoning intact: a stock that's been quiet on average and on the prior
day, but is trading heavily *right now*, still survives on
`current_day_volume` alone -- exactly the "waking up" pattern this bot is
meant to catch. A `None` `average_volume`/`previous_day_volume` (a failed
lookup, or a broker with no real daily-volume history at all -- paper/
backtest mode) can't be proven to miss its floor, so it's treated as NOT
failing; since rejection requires all three to fail, this makes rejection
impossible whenever either of those two is missing, regardless of
`current_day_volume` -- see `_fails_volume_floor`'s docstring. Dollar
volume remains purely informational either way: `Candidate.dollar_volume_today`
is populated but never gates discovery (see `scanner/broad_scanner.py`'s
module docstring). Unvalidated starting thresholds, not backtested --
same framing as `scoring/weights.yaml`.

The average-volume lookup is backed by `WebullBrokerClient.get_daily_volumes`,
which deliberately does **not** reuse `get_bars()`/`_snapshots_from_bars()`:
that method accumulates volume across every fetched bar for intraday
VWAP, which is correct for minute bars within one session but wrong for
daily bars spanning multiple days (it would sum several days' volume
together instead of reporting each day's own total). Confirmed live
(2026-08-09) against raw daily bars that each day's `volume` field is
already a clean, distinct per-day total, most-recent-first.

A live finding from that verification is directly relevant now that the
volume floor gates discovery again: **sandbox historical data quality
varies by symbol liquidity.** Mega-caps (AAPL, TSLA, NVDA) returned
consistent, plausible volume across all 10 days requested. Every low-
float/micro-cap symbol tested (the bot's actual target universe) returned
a real-looking value for only the *most recent* day, with the other 9
showing near-zero placeholder-looking figures. This means
`average_daily_volume` may not be meaningful in sandbox testing for this
bot's real target names -- `previous_day_volume` is the reliable one of
the two there. This appears to be a sandbox data-population limitation,
not a code bug, and should be re-verified once trading against real
production data. It's also exactly why the volume floor's either-or
exemption matters in practice, not just in principle: a genuinely active
low-float name whose sandbox `average_daily_volume` gets dragged toward
zero by 9 fake-looking days still survives the gate on
`previous_day_volume` or `current_day_volume` alone, so this known
data-quality gap shouldn't cause real misses even before it's fixed.

Separately (found during this same live testing, unrelated to the
average-volume work itself): the configured FMP API key was returning
`429 Limit Reach` on every endpoint tested, meaning `FloatDataProvider.get_float_data`
fails for every symbol and `BroadScanner` silently excludes everything at
that step (`_check_symbol`'s `except Exception: return None`) regardless
of any other condition -- free float remains a hard structural gate, so a
failed float lookup still means no candidate, even though volume/dollar
volume no longer work that way. Until that plan/quota issue is resolved,
**no candidates will be discovered at all**, independent of price or
volume.

`PaperBrokerClient` has no live screener of its own, so paper mode falls
back to `StaticUniverseProvider` with a placeholder watchlist -- paper mode
is for exercising the pipeline against manually-fed snapshots, not
autonomous discovery.

## State machine

```
DISCOVERED -> WATCHING -> HEATING_UP -> ARMED -> TRIGGERED -> ENTERED -> MANAGING -> EXITED -> COOLDOWN
                  \           \           \          \
                   -----------------> REJECTED (terminal, from most states)
```

A high Momentum Ignition Score can only push a candidate towards ARMED. It
never creates an order by itself -- see `state_machine.py` and
`scanner/candidate_watcher.py`.

## Resistance tracking gotcha (read before touching candidate_watcher.py)

`CandidateWatcher.update()` computes metrics/scores using the resistance
level as it stood **before** the current snapshot, and deliberately does
NOT fold the current bar's high into `candidate.resistance_level` until
`update_resistance()` is called afterward. A bar's `high_of_day` always
includes its own `last_price`, so if resistance were updated first, a
breakout check (`price > resistance`) could never fire on the bar that
actually breaks out. Call order for every snapshot must be:

```python
watcher.update(candidate, snapshot)
signal = trigger_engine.on_snapshot(candidate, snapshot)
watcher.update_resistance(candidate, snapshot)
```

`backtest/engine.py` follows this order; any new live run-loop must too.

## Resistance detection: volume profile, not hand-picked levels

`resistance_level` used to be purely the running high of day. It's now a
merge of that running high with **static levels from volume-profile
analysis** (`metrics/volume_profile.py`), computed at discovery time
(`BroadScanner._compute_static_resistance_levels`) and stored on
`candidate.static_resistance_levels` -- and, since periodic refreshing was
added, re-computed on later universe rescans for as long as the candidate
hasn't entered a position yet (see "Periodic refresh" below).

**Why volume profile instead of a list of special levels** (prior day
high, premarket high, round numbers, ...): those special levels usually
show up as high-volume clusters anyway, since psychologically notable
prices attract more trading, and a consolidation/base *is* a volume
cluster by definition. Building a histogram of volume-traded-per-price
over a lookback window and taking the biggest clusters ("high volume
nodes") is a more general mechanism that tends to surface the same levels
without hand-picking them, plus it gives a real strength signal (a node
with 3x the volume of another is a materially stronger wall) that a flat
list of price points can't provide.

**How it's built**: `WebullBrokerClient.get_raw_bars(symbol, interval,
count)` fetches raw per-bar OHLCV, deliberately bypassing
`get_bars()`/`_snapshots_from_bars()` -- that method accumulates volume
across every fetched bar for intraday VWAP, which is wrong for a profile
that needs each bar's own volume independently (it would sum multiple
bars/sessions together instead of reporting each one's own total; see
`get_daily_volumes`, which has the same requirement and now shares this
helper). `compute_volume_profile` then spreads each bar's volume evenly
across every price bucket its `[low, high]` range touches, and
`high_volume_node_levels` keeps the top N buckets that clear a
significance threshold (a fraction of the single largest bucket's volume,
to separate a real cluster from background noise).

**Pre-market/after-hours bars are included, deliberately**: `get_raw_bars`
requests `trading_sessions=["PRE", "RTH", "ATH"]` rather than leaving the
SDK's regular-session-only default, so the volume profile reflects a name's
full pre-market/after-hours activity, not just 9:30am-4:00pm ET -- a
low-float mover's real resistance level can easily form entirely in
pre-market. The three session-code strings themselves are **inferred, not
confirmed live**: `get_history_bar`'s own SDK docstring documents the
`trading_sessions` parameter without listing accepted values, and `"RTH"`/
`"PRE"`/`"ATH"` are only spelled out on a *different* endpoint in the same
SDK (`get_footprint`). Re-verify live during an actual pre-market or
after-hours session (confirm returned bar timestamps actually fall outside
the regular-session window) before fully trusting this -- see the docstring
in `brokers/webull/client.py`'s `get_raw_bars` for the full reasoning. This
change is scoped to `get_raw_bars` only: `get_bars()`/`_snapshots_from_bars()`
(VWAP, Momentum Ignition Score ticks) still uses the regular-session
default deliberately, since extending sessions there would change VWAP/MIS
behavior beyond resistance detection. It's also safe to share with
`opening_range_high` (below), which already time-filters its own bars down
to the 9:30am regular-session window, so the extra pre/after-market bars
simply fall outside that filter rather than corrupting it.

**A real data-shape finding worth knowing**: `get_raw_bars` returns the
last `count` bars that actually have data, not the last `count` calendar
time-slots. Confirmed live (2026-08-09): 100 5-minute bars for a liquid
mega-cap (AAPL) spanned about 1 day, while the same request for an
illiquid low-float mover (MB) reached back ~25 calendar days to find 100
real bars -- and a 780-bar request for that same symbol reached back
about 5.5 months. This is a legitimate consequence of thin trading (an
illiquid stock just doesn't have a bar in every 5-minute window), not bad
data -- but it means a profile built directly on "the last N bars" could
be dominated by months-old, no-longer-relevant price action for an
illiquid name. `filter_bars_by_lookback` trims the fetched bars back down
to a bounded, recent calendar window (`volume_profile_lookback_days`,
default 20) before the profile is computed, accepting that the resulting
profile may end up sparse or even empty for a name that genuinely hasn't
traded much recently -- that's an accurate reflection of "not much recent
history to draw on," not a bug to work around.

**The merge rule** (`CandidateWatcher.update_resistance`) picks the
*nearest* static level still above the running high, not the highest one
available: once intraday price trades through a static level it's no
longer resistance (it may even flip to acting as support), so re-picking
the closest remaining ceiling each tick keeps `resistance_level` meaning
"the next real obstacle," not "the biggest one on record." Falls back to
the plain running high -- this whole mechanism's entire prior behavior --
when no static level remains above it, or when `static_resistance_levels`
is empty (paper/backtest mode, or a failed/unsupported lookup; see below).

**Cost and failure handling**: `_compute_static_resistance_levels` is a
third Webull-paced call, but only for symbols that already cleared the
structural price/float gates and became an actual `Candidate` -- its cost
scales with how many candidates get discovered per scan, not with
universe size, unlike the price/float checks that run against every
universe symbol. A failure or an unsupported broker (`getattr`, same
pattern as `_compute_average_volume_info`) returns an empty list rather
than rejecting the candidate: this only changes how resistance is
*tracked*, it isn't a discovery gate, so there's nothing to fail closed on.

**Verification status**: `get_raw_bars`'s data-sparsity behavior above was
confirmed live. The full merge pipeline's own unit tests
(`tests/test_volume_profile.py`, `tests/test_candidate_watcher.py`,
`tests/test_broad_scanner.py`) all pass, but a live end-to-end run of
`_compute_static_resistance_levels` against the sandbox could not be
completed on 2026-08-09 -- by that point in the session, cumulative
testing had exhausted whatever request quota the sandbox enforces, and
every further request (even a single one, from a freshly-started process)
came back `429 TOO_MANY_REQUESTS` through all 4 retry attempts. This is
distinct from the earlier-diagnosed sustained ~1 req/s pacing limit: that
one resets by simply waiting between requests within a process, while this
one persisted across brand-new processes, suggesting the sandbox also
enforces some rolling quota tracked server-side, independent of any local
pacing. Re-verify the full pipeline live once quota recovers (or during
real market hours on the VPS) before trusting its output blindly.

**Periodic refresh** (`BroadScanner.refresh_resistance_levels`, called from
`TradingLoop._refresh_stale_resistance_levels` on every
`_rescan_universe` cycle): a volume profile built from whatever bars
existed the moment a candidate was discovered is necessarily incomplete
for one found early in the session -- nodes that form later in the day
never show up in `static_resistance_levels` unless something re-fetches
and recomputes it. This re-fetches fresh bars and rebuilds the profile
exactly like discovery does, for any candidate still in a pre-entry state
(`WATCHING`/`HEATING_UP`/`ARMED` -- `TradingLoop._RESISTANCE_REFRESH_STATES`)
whose `resistance_last_refreshed_at` is older than
`TradingLoopConfig.resistance_refresh_interval_seconds` (default 300s, an
unvalidated starting point). Once a candidate enters a position,
resistance stops mattering entirely (`PositionManager`'s stop/target/
trailing-stop rules govern exits from there, never `resistance_level`),
so refreshing stops too. Deliberately does **not** also re-fetch
`opening_range_high`: unlike the volume profile, a later bars fetch can
only make that value worse, never better, since `get_raw_bars`' fixed bar
count covers a trailing window that slides forward through the day,
pushing the market-open bars further out of range the later it's called
-- see `refresh_resistance_levels`'s docstring.

The throttle interval is independent of `universe_rescan_interval_seconds`
specifically so raising rescan frequency doesn't multiply this cost: every
refresh is a real Webull-paced call per eligible candidate, and unlike
new-symbol discovery (which pays this cost once per symbol ever), this
recurs for every still-watched candidate on a schedule of its own. This
mutates already-tracked `Candidate` objects from the background rescan
thread -- a deliberate, narrow exception to this module's stated rule that
only the main thread mutates existing candidates, justified in
`_refresh_stale_resistance_levels`'s docstring (plain attribute
reassignment is atomic under the GIL, so there's no torn read, only a
possible one-tick-stale read on a genuine race, which the multi-minute
throttle interval makes immaterial).

## RVOL historical baseline

`relative_volume`/`relative_volume_1m`/`relative_volume_5m`
(`metrics/rolling.py`) need something to compare today's activity against
-- "current volume vs. what's typical for this symbol at this point in the
session." Without a baseline, `relative_volume()`'s safe-division default
kicks in (a neutral `1.0`, meaning "exactly average"), and since the MIS
scoring curve maps that neutral value to a `0` score
(`relative_volume_score = _scale(metrics.relative_volume, 1.0, ...)` --
`1.0` is the scale's own zero point), Relative Volume and Short-Term
Relative Volume read a flat `0` in the dashboard's Score Weighting
Breakdown regardless of session or real activity. This was true before
`metrics/volume_baseline.py` existed, and is unrelated to
`cumulative_volume` correctness -- it's purely the absence of a baseline to
divide by.

**Where the baseline data comes from, and why**: two options exist for
building "what's typical for this symbol." (1) Accumulate it from this
bot's own recorded ticks over time (`momentum_scores` in Postgres) --
simple to build, but has a fatal flaw for this bot's actual target pattern:
a low-float momentum mover is very often a symbol that's never been
scanned before, and a baseline that only builds up after weeks of running
would have zero data on exactly the day it matters most (a brand-new
mover's first hot day). (2) Derive it from Webull's own historical
intraday bars -- available from day one for any symbol, and free: it
reuses the SAME ~780 bars `BroadScanner` already fetches for the
resistance volume profile (`_fetch_raw_bars`), so building this costs zero
extra Webull round-trips. Option 2 is what's implemented, specifically
because of the "brand-new mover" case option 1 can't handle.

**Cumulative volume is not one smooth curve across a trading day.** It
resets at the pre-market/regular-session boundary and again at the
regular/after-hours boundary -- this is the same discontinuity from
`WebullBrokerClient._snapshot_from_dict`'s `ext_price`/`ext_volume`
handling (see "Webull integration" below): `cumulative_volume` is sourced
from Webull's regular-session `volume` field during RTH and from
`ext_volume` outside it, and those are two different counters, not one
continuously-accumulating number. A baseline built as a single "minutes
since midnight" curve would therefore compare today's regular-session-only
volume against a historical figure that also includes that day's
pre-market, silently understating RVOL for the entire regular session.
`compute_volume_baseline` instead tracks **three independently-reset
curves** -- `PRE` (4:00am-9:30am ET), `RTH` (9:30am-4:00pm ET), and `ATH`
(4:00pm-8:00pm ET) -- built by grouping bars by calendar day and phase,
then computing a per-day running cumulative sum that resets at each
phase's own start. A live lookup (`VolumeBaseline.lookup`) classifies the
current tick into the same three phases and only ever compares against
that phase's own curve, matching the live side's reset behavior exactly.
The 4:00am/8:00pm phase boundaries are Webull's documented standard
pre-market/after-hours window, not independently confirmed live for this
account -- same "reasonable default, not verified" status as most of this
project's other unvalidated starting points.

**Bucketing and averaging**: bars are grouped into `bucket_minutes`
increments (default 5, matching `volume_profile_bar_interval`) measured
from each phase's own start. For each `(phase, bucket)` pair,
`typical_cumulative` averages the running-cumulative-through-that-bucket
across every historical day that had data there, and
`typical_bucket_volume` averages that specific bucket's own (non-
cumulative) volume the same way -- the latter feeds `typical_volume_5m`
directly, and `typical_volume_1m` approximately, as
`typical_bucket_volume / bucket_minutes` (a uniform-within-the-bucket
assumption -- there's no way to recover a true historical 1-minute figure
from 5-minute bars, so this is the best available estimate, not a precise
historical rate). **Today itself is excluded** from every average (bars
are filtered by calendar date in US/Eastern against `now`): a day still in
progress would otherwise leak its own still-forming numbers into its own
baseline, most visibly right at the PRE/RTH boundary, where "today's
pre-market volume" would otherwise inflate "today's" own baseline entry
using data that only exists because that activity already happened.

**Wiring**: `BroadScanner._compute_volume_baseline` runs once at discovery
(from the same `bars` already fetched for `static_resistance_levels`/
`opening_range_high` -- see that section above) and stores the result on
`Candidate.volume_baseline`. Unlike resistance, this is **not** part of the
periodic refresh cycle (`refresh_resistance_levels`/
`TradingLoopConfig.resistance_refresh_interval_seconds`): the baseline
only reflects days *before* today, which don't change again once the day
is over, so recomputing it once at discovery is sufficient --  there's no
"today's data filling in" effect to chase the way there is for resistance.
`CandidateWatcher.update()` looks up `candidate.volume_baseline` for the
current snapshot's timestamp on every tick and passes the three resulting
values into `compute_metrics`'s `typical_volume_same_time`/
`typical_volume_1m`/`typical_volume_5m` parameters. A candidate with no
baseline (paper/backtest mode, a failed/unsupported `get_raw_bars` lookup,
or a bucket no historical day ever reached) simply passes `None` for all
three, and `relative_volume`/`relative_volume_1m`/`relative_volume_5m`
fall back to their pre-existing neutral default -- same fail-soft contract
as `static_resistance_levels`.

## Seeding rolling history at discovery (discovery lags the move)

**The problem**: `CandidateWatcher._history[symbol]` -- the rolling tick
history every *windowed* metric (`float_velocity_5m`, `volume_accel_1m_3m`,
`price_acceleration`, `relative_volume_5m`, `dollar_volume_accel_1m_3m`;
see `metrics/rolling.py`) is diffed across -- used to start as an empty
list at discovery and only grow from live ticks going forward. For a
low-float momentum bot, that's backwards: a candidate is discovered
*because* it already made a move big enough to surface on a screener, so
the move that caused discovery structurally already happened *before*
discovery, not after. A name up +100% in pre-market on a huge volume
surge would show 0 across every "is this accelerating right now"
component for several real minutes after being found -- while its
cumulative-total components (`relative_volume`, `float_turnover`,
`breakout_proximity`) correctly read near-maxed -- silently understating a
candidate that's actually already extremely hot. Confirmed against a real
case (2026-08-10): a low-float name already up over 100% in pre-market on
heavy volume scored 40.4 (just barely HEATING_UP) because every
window-diffed component read exactly 0, even though the static components
were already maxed.

**The fix** (`metrics/rolling.seed_history_from_bars`): reconstructs
synthetic `MarketSnapshot`s for the `MAX_HISTORY_MINUTES` (20) immediately
before discovery from the same raw bars `BroadScanner` already fetches for
`static_resistance_levels`/`opening_range_high`/`volume_baseline` above --
no extra network call, this is the fourth consumer of that one fetch.
`BroadScanner._compute_seed_snapshots` computes this once at discovery and
stores it on `Candidate.seed_snapshots`; `CandidateWatcher._push_history`
splices it into `_history[symbol]` the first time (and only the first
time) that symbol's history is touched, ahead of the real live snapshot.

**Cumulative-volume anchoring is the part that has to be exactly right**:
the seeded series isn't a fresh count starting at 0 -- it's built
*backward* from the discovery snapshot's own real `cumulative_volume`
(`current`), so the seeded series and the live feed that starts arriving
right after share the exact same absolute scale. Concretely: the seed
window's total bar volume is subtracted from `current.cumulative_volume`
to get the running total as of the earliest seed bar, then each bar's own
volume is added back in walking forward chronologically -- so the *last*
seed snapshot's `cumulative_volume` lands exactly on
`current.cumulative_volume`, and the live snapshot that follows continues
that same series with no jump. Without this anchoring, splicing a
locally-reconstructed volume count (starting at 0) in front of the live
feed's actual multi-million-share total would produce one enormous,
meaningless synthetic "volume spike" at the seam between seed data and the
first real tick -- worse than not seeding at all.

Only `timestamp`/`cumulative_volume`/`last_price` are computed
meaningfully on a seed snapshot -- `compute_metrics` only reads those
three fields from non-latest history entries (see `_window`/
`_volume_since`/`price_velocity_pct` in `metrics/rolling.py`);
bid/ask/high/low/vwap/open_price are set to `last_price` as an inert
placeholder, since a historical entry's values for those are never read.

**Splice-once, not every tick** (`CandidateWatcher._push_history`): seeding
only happens when that symbol's own `_history` is still empty, so a
candidate that cools off, transitions through COOLDOWN, and later
re-enters WATCHING doesn't get re-seeded on top of real accumulated ticks
-- a fresh `Candidate` object from a later discovery gets its own fresh
`seed_snapshots` computed from bars fetched at *that* (re-)discovery time,
naturally superseding the old seed the moment `_history[symbol]` is
non-empty. `Candidate.seed_snapshots` itself is never cleared after use --
harmless, since the empty-history check means it's only ever consulted
once regardless of how long it sits on the candidate.

**Fail-soft, same contract as everything else built on this shared bars
fetch**: no bars (paper/backtest mode, a failed/unsupported `get_raw_bars`
lookup), or no bars falling in the `MAX_HISTORY_MINUTES` window immediately
before discovery, simply means `seed_snapshots` stays an empty list --
`CandidateWatcher`'s rolling window starts empty, exactly its pre-this-
feature behavior. A rolling window spanning the pre-market/regular-session
boundary can still see one self-healing artificially-flat reading right at
that seam, same known caveat as the `ext_volume` phase-reset handling in
`WebullBrokerClient._snapshot_from_dict` -- not specially handled here,
same acceptable-edge-case philosophy already established for that boundary
elsewhere in this file.

## Entry strategies

Resistance-breakout alone doesn't fit every candidate this bot finds: for a
lot of low-float movers the nearest resistance level is far enough above
the current price that waiting for it gives up most of the move, while for
others there's no meaningful resistance at all (e.g. a recent IPO with no
volume-profile history). Rather than one entry condition, `TriggerEngine`
is handed a list of independent `Strategy` instances -- each reads
`candidate`/`snapshot` state and either returns a `Signal` or doesn't; the
first one to fire for a given tick wins (see `scanner/trigger_engine.py`).
All of them still flow through the same unchanged pipeline downstream
(`Strategy -> RiskEngine -> OrderManager -> BrokerClient`), so none of this
touches position sizing, stop enforcement, or order placement.

Registered in `main.py`'s `build_trading_loop()`, in this order (most
selective/confirmed pattern first, most permissive last -- since only the
first match per tick fires, a broad catch-all placed early would prevent
more specific patterns from ever getting a chance):

1. **`RefinedBreakoutStrategy`** (`strategy/refined_breakout.py`) -- a
   stricter breakout: price must be between the resistance level and
   `resistance * 1.03` (3% above it, configurable via
   `max_breakout_extension_pct`). Plain `MomentumBreakoutStrategy` has no
   upper bound and will "confirm" a breakout of a level price ran past
   hours ago; this is reserved for a genuinely fresh, in-progress break.
2. **`OpeningRangeBreakoutStrategy`** (`strategy/opening_range_breakout.py`)
   -- fires on a break of `candidate.opening_range_high`, the high of the
   first `opening_range_minutes` (default 5) of the session. Computed once
   at discovery time (`BroadScanner._compute_opening_range_high`,
   `metrics/opening_range.py`) from the same raw bars already fetched for
   `static_resistance_levels` -- no extra network call. DST-correct market
   open via `zoneinfo`, not a hardcoded UTC offset. `None` (strategy never
   fires) whenever the fetched bars didn't cover market open -- e.g.
   discovered well after the open, or no `get_raw_bars` capability. Catches
   candidates the resistance-based strategies structurally can't: a name
   with no meaningful volume-profile cluster still has an opening range
   every session. Stop is `min(opening_range_high, flat-pct stop)` -- a
   structural ceiling, not "whichever is tighter."
3. **`VWAPReclaimStrategy`** (`strategy/vwap_reclaim.py`) -- catches a
   candidate that dipped meaningfully below VWAP
   (`distance_from_vwap_pct <= below_vwap_threshold_pct`) and is now
   reclaiming it with fresh volume. The "second leg" of a move that
   breakout and ignition entries both miss (ignition requires already being
   above VWAP; breakout requires a specific price level). Needs one bit of
   per-symbol state ("was this recently below VWAP?") to distinguish a
   fresh reclaim from having been above VWAP for hours -- kept in a private
   `dict` on the strategy instance, not on `Candidate` (see state-isolation
   note below). Stop is VWAP-anchored (`vwap * (1 - stop_buffer_pct/100)`),
   not a flat %, since VWAP-holding-as-support is exactly what this entry
   bets on.
4. **`MomentumBreakoutStrategy`** (`strategy/momentum_breakout.py`) -- the
   original plain breakout-above-resistance strategy, unbounded above (no
   3% cap). Kept registered alongside the stricter variants above it.
5. **`BreakoutPullbackStrategy`** (`strategy/breakout_pullback.py`) --
   waits for the initial resistance breakout, then requires a controlled
   pullback on declining volume before entering on reclaim. Targets entries
   further from short-term exhaustion than a bare breakout. State
   (`breakout_price`/`pullback_low`) lives on the shared `Candidate`
   object -- the original design, predating the state-isolation rule below,
   and safe because it's the only strategy that still uses those particular
   `Candidate` fields.
6. **`IgnitionPullbackStrategy`** (`strategy/ignition_pullback.py`) -- the
   same pullback-then-reclaim pattern as #5, but anchored to a *volume
   ignition* move (see #8 below) instead of a resistance breakout, so it
   works on any candidate seeing a real volume+price surge, not just ones
   near a known resistance level.
7. **`VolatilityContractionBreakoutStrategy`**
   (`strategy/volatility_contraction.py`) -- the "flag/pennant" pattern:
   price tightens into a narrow range after an initial move, then expands
   out of it with volume. Measured as the ratio of a tight window's price
   range to a broader context window's (`price_range_pct_3m /
   price_range_pct_15m`, both computed by `metrics/rolling.py` alongside
   the other per-tick metrics -- nothing new fetched). A low ratio means
   the last 3 minutes were much quieter than the last 15; `min_broader_range_pct`
   guards against mistaking a generally-dead name (quiet on both windows)
   for a real contraction. Deliberately simplified vs. genuine
   swing-high-based consolidation tracking -- worth revisiting if it
   underperforms in practice.
8. **`VolumeIgnitionStrategy`** (`strategy/volume_ignition.py`) -- the
   broadest/most permissive strategy, registered last on purpose. Fires on
   a volume acceleration surge (`volume_accel_1m_3m >=
   min_volume_acceleration`) **or** a float-turnover-specific ignition
   (`float_velocity_5m >= min_float_velocity_5m`), combined with rising
   price and price above VWAP as anti-dump confirmation. No resistance
   level needed to anchor an entry to. Its target is computed the same way
   as every other strategy's (see "Risk sizing" below) -- an earlier
   version of this strategy left `suggested_target=None` on the reasoning
   that a fixed target would cap gains, but now that hitting target is a
   partial exit rather than a full close (see "Position management"
   below), that concern no longer applies: it banks half at target like
   the other 7 and lets the rest keep riding `PositionManager`'s
   trailing-stop/VWAP-failure/time-limit exits. This is the entry for a
   symbol whose resistance is too far away to be a useful reference at
   all -- the original motivating case for this whole batch of strategies.

**State isolation**: with several pullback/phase-tracking strategies now
registered simultaneously, any *new* stateful strategy added here keeps its
state in a private `dict[str, ...]` on the strategy instance itself, keyed
by symbol (`VWAPReclaimStrategy._was_below_vwap`,
`IgnitionPullbackStrategy._phase`/`_ignition_price`/`_pullback_low`) --
**not** on the shared `Candidate` object. Two stateful strategies sharing
`Candidate` fields (the way `BreakoutPullbackStrategy` uses
`breakout_price`/`pullback_low`) would silently clobber each other's
tracking every tick, since `TriggerEngine` runs every registered strategy
against the same `Candidate` instance.

**Not implemented**: relative strength vs. SPY/QQQ was considered and
explicitly deferred -- it needs a new market-data feed this bot doesn't
have wired up yet, unlike every strategy above, which only reads
already-computed `MomentumMetrics`/`Candidate` fields.

**Verification status**: all 8 strategies have dedicated unit tests
(`tests/test_<strategy_name>.py`) covering their entry conditions, but none
have been backtested or run live yet -- their config defaults are
unvalidated starting points (same framing as `scoring/weights.yaml`), not
tuned thresholds.

## Risk sizing

Once a `Strategy` emits a `Signal`, `RiskEngine.evaluate()` (`risk/risk_engine.py`)
decides both *whether* to trade it and, if so, *how many shares*. Seven of
its `RiskConfig` fields are adjustable live from the dashboard's Settings
button (top right -- see "Dashboard" below) via `GET`/`POST
/api/risk-settings`, which mutate the running `RiskEngine.config` in place;
changes apply to the very next `Signal` evaluated, no restart needed.

**Core/extended trading hours gate** (checked immediately after the kill
switch, before any of the sizing math below): `evaluate()` refuses every
entry signal outside a trading-hours window, rejecting with
`RiskEventType.OUTSIDE_CORE_TRADING_HOURS`. Which window depends on
`RiskConfig.allow_extended_hours_trading` (dashboard-adjustable, **off by
default**): off, it's `market_hours.is_within_core_trading_hours` (9:30am-
4:00pm ET, Mon-Fri) as before; on, it widens to
`market_hours.is_within_extended_trading_hours` (4:00am-8:00pm ET, Mon-Fri
-- CORE plus Webull's standard pre-market/after-hours windows). Added
after a production report of trades filling *during* core hours whose
resulting positions then went untracked (see "Position tracking can be
lost on a broker-side fill reconciliation failure" below) -- investigating
that report surfaced that there was no explicit application-level
guarantee at all that entries only happen in core hours; this closes that
gap outright rather than leaving it to whatever Webull itself does with an
out-of-hours order (see the `support_trading_session` note in "Webull
integration"). This only ever applies to entries: `OrderManager.submit_signal`
never routes `EXIT`/`SCALE_OUT` signals through `evaluate()` at all (see
its own docstring), so a stop-loss or the end-of-core-hours auto-flatten
(see "Position management" below) is never blocked by this gate --
`evaluate()` itself has no action-type carve-out, it's simply never called
for exits.

**Important: turning `allow_extended_hours_trading` on only widens WHEN a
signal is allowed past this gate.** It does not, by itself, change what
`support_trading_session` value the resulting order goes out with -- that's
`Settings.webull_support_trading_session` (env var
`WEBULL_SUPPORT_TRADING_SESSION`), which still defaults to `"CORE"` in
code even though `"ALL"` is now confirmed live to work (see the
`support_trading_session` note in "Webull integration" for the full
history). Turning this toggle on without also setting that env var to
`"ALL"` will let a signal through the risk gate only to have the broker
still receive a `"CORE"`-scoped order underneath.

`now` matters here as much as it does for the daily-rollover/cooldown logic
already in this method, so it's threaded through properly rather than left
to default: `OrderManager.submit_signal` now accepts an optional `now` and
forwards it to `evaluate()`. `TradingLoop._submit_entry` passes its own
per-tick `now` (so the live gate checks the real tick time); `backtest/engine.py`
passes `snapshot.timestamp` (the *simulated* bar's time, not the real
wall-clock time the backtest happens to be run at -- a backtest run at 2am
replaying a 10am historical bar must gate against 10am, not 2am). Leaving
`now` as `None` (every call site's implicit behavior before this) still
falls back to `evaluate()`'s own `datetime.utcnow()`, which is what a
caller with no natural notion of simulated time wants.

**Daily loss limit** (`max_daily_loss_pct`, default 3% of equity,
dashboard-adjustable) -- checked right after the core-hours gate, before
any trade-count/cooldown/sizing checks. `RiskEngine._daily.realized_pnl`
accumulates every `record_trade_closed` call for the current day (rolled
over at midnight UTC via `_roll_day_if_needed`); once it's at or below
`-max_daily_loss_pct% * account_equity`, every new entry is rejected
(`RiskEventType.DAILY_LOSS_LIMIT_HIT`) for the rest of the day, regardless
of ticker or strategy. Like the other adjustable fields, a dashboard
change takes effect on the very next `Signal` evaluated. This only ever
blocks *new* entries -- exits never route through `evaluate()` (see the
core-hours gate note above), so an existing position can still be closed
normally even after the daily limit trips.

**1. Minimum reward:risk ratio** (`min_risk_reward_ratio`, default 2.0) --
checked first, before any sizing math. If a signal specifies a
`suggested_target`, the distance from entry to target must be at least
`min_risk_reward_ratio` times the distance from entry to stop, or the
signal is rejected outright (`MIN_RISK_REWARD_NOT_MET`). Signals with no
target at all (e.g. `VolumeIgnitionStrategy`, which manages the trade with
a trailing stop instead of a fixed target) skip this check entirely --
there's no reward distance to compare against. A small epsilon tolerance
guards against a signal that's exactly at the minimum ratio in theory
getting rejected by floating-point noise from reconstructing the target
price.

**2. Total assumed risk ceiling** (`max_total_risk_pct`, default 50% of
equity) -- summed across every open position as `(entry_price -
stop_price) * quantity`, i.e. what would actually be lost if every open
position's stop got hit simultaneously, not their combined notional size.
This is deliberately a *risk* cap, not an *exposure* cap: two positions
with identical dollar size but very different stop distances carry very
different amounts of real risk, and a notional-only cap (the old
`max_account_exposure_pct`) couldn't tell them apart. If the existing
positions alone already consume the full ceiling, the new signal is
rejected (`MAX_EXPOSURE_HIT`).

**Real bug fixed here (2026-08-12), not present in this doc's earlier
description**: `open_positions` is now explicitly supplied by the caller
(`OrderManager.submit_signal`'s `open_positions` parameter -- see that
method's docstring) rather than fetched internally via
`self.broker.get_positions()`. It used to be the latter, and every
`Position` a broker returns -- `WebullBrokerClient._position_from_dict` and
`PaperBrokerClient.get_positions()` alike -- hard-codes `stop_price=None`
(there's no such field in a broker's raw account-positions response; a
stop is a resting order or a purely local software-managed concept, never
a property of the position row itself). Since this gate's summation
filters on `p.stop_price is not None`, it silently saw zero risk from
every existing position no matter how much was actually on, and could
never reject a new entry on that basis, in every trading mode, the entire
time this gate has existed. Only a caller's own locally-tracked positions
(`TradingLoop._positions`, set from each entry's own `suggested_stop` and
kept current by breakeven/trailing math) carry a real `stop_price`.
`max_simultaneous_positions`' count-based gate was unaffected (`len()`
doesn't need `stop_price`), which is exactly why this went unnoticed.
**Renamed from a shrink-the-trade gate to a
pure accept/reject gate (2026-08-11)**: since sizing is no longer
risk-budget-driven (see #3 below), there's no "budgeted risk" left to trim
to fit remaining room -- this can only refuse a trade outright once
existing positions have already breached the ceiling, never resize one.

**3. Stop-loss distance** (`stop_loss_pct`, default 5%) -- **not a sizing
input at all** (2026-08-11 redesign). This field only ever determines
*where a strategy's stop sits*, never *how many shares to buy*. It's read
live by the five strategies whose stop is a flat %-from-entry
(`momentum_breakout`, `refined_breakout`, `opening_range_breakout`,
`volatility_contraction`, `volume_ignition`) via a `stop_loss_pct_fn`
closure -- the same live-wiring pattern `min_risk_reward_ratio` already
used (see `main.py`'s `build_trading_loop`), so a dashboard change moves
every one of those strategies' stops together on the very next signal. The
other three strategies (`vwap_reclaim`, `breakout_pullback`,
`ignition_pullback`) anchor their stop to a technical level (VWAP / a
pullback low) plus a small strategy-local buffer instead, and are
deliberately **not** wired to this field -- see each one's own config for
why forcing them to share a single flat % would corrupt their design
intent. This field used to be named `risk_per_trade_pct` and meant "% of
account equity to risk on this trade," an entry-to-stop dollar budget that
(combined with the stop distance) determined share count. That coupling
was removed entirely: see #4 below for what determines share count now.

**4. Position-size ceiling** (`max_position_size_pct`, default 100% of
**buying power**, not equity) -- **the only thing that determines share
count** (2026-08-11). `max_shares = int((buying_power *
max_position_size_pct / 100) // entry_price)`. Buying power
(`broker.get_buying_power()`) is used rather than equity specifically so
this reflects capital actually available to deploy right now, not total
account value that may already be committed to other open positions. A
signal's stop distance plays no role here whatsoever -- a tight stop and a
wide stop on the same symbol at the same price get the exact same share
count. If this comes out to zero, the trade is rejected (`Computed
position size is zero...`).

**`RiskDecision.risk_amount`**: informational only now, computed *after*
sizing rather than driving it -- the actual dollar amount this specific,
already-sized trade would lose if its stop is hit (`(entry_price -
stop_price) * max_shares`). `None` whenever there's no valid stop distance
to compute it from (`stop_loss_required=False` and no `suggested_stop`).
No consumer in this codebase reads it yet; it exists purely for
transparency into what a filled trade's real dollar risk turned out to be.

**Clamped to Webull's own hard order-quantity ceiling (2026-08-12).**
`max_shares` above is now `min(int((buying_power * max_position_size_pct /
100) // entry_price), 199_999)`. Confirmed live: a cheap/penny-priced
signal (DOGZ) combined with `max_position_size_pct=100.0` (the default)
and a large sandbox buying-power balance computed a share count well past
200,000 -- Webull rejected it outright
(`OAUTH_OPENAPI_ORDER_QUANTITY_EXCEED_LIMIT`, HTTP 417, "Order quantity
must be below 200,000"), and since every re-trigger on the symbol
recomputed the exact same oversized quantity, the candidate just kept
failing and reverting to `ARMED` forever, never opening a position. This
is a genuine broker-side constraint, not a risk-tuning question -- clamped
unconditionally in `RiskEngine.evaluate` (`_WEBULL_MAX_ORDER_QUANTITY` in
`risk/risk_engine.py`) rather than left to whatever `max_position_size_pct`
a deployment happens to have configured. See
`tests/test_risk_engine.py::test_position_size_is_clamped_to_webulls_hard_order_quantity_ceiling`.

**Order prices are rounded to a valid tick size before hitting the wire
(2026-08-12).** Same incident window as above, different Webull rejection:
once the quantity-ceiling clamp let a BIVI entry actually open, every
attempt to attach its broker-side OCO stop+target bracket then failed with
`OAUTH_OPENAPI_STOCK_ORDER_PRICE_PRECISION_EXCEED` (HTTP 417, "Price
increment should be 0.01 when price is equal to or greater than 0.9999").
Root cause: `target_price = entry_price + risk_per_share *
reward_risk_ratio` in every strategy (`strategy/*.py`) is a plain float
computation with no rounding step, so a real run produced
`3.4667600000000003` as the bracket's LIMIT leg price -- valid Python, but
not a price Webull's own tick-size rule (2-decimal increments at/above
$1) accepts. `WebullBrokerClient._order_payload` now rounds both
`limit_price` and `stop_price` through a new
`_round_to_valid_price_increment` helper (2 decimals at/above $1, 4 below
-- mirroring the standard SEC Rule 612 sub-penny convention Webull's own
message implies) right before they're serialized into the request. This
is deliberately fixed at that one choke point rather than in each
strategy's own target-price math, `RiskEngine`/`PositionManager`'s stop
math, or `OrderManager`'s extended-hours marketable-limit pricing --
every one of those computes a price somewhere upstream with no guarantee
any of them already round cleanly, and a single rounding point downstream
of all of them catches every case rather than requiring each call site to
remember to round itself. See
`tests/test_webull_broker_client.py::test_order_payload_rounds_limit_price_to_cents_at_or_above_a_dollar`,
`::test_order_payload_rounds_stop_price_to_cents_at_or_above_a_dollar`, and
`::test_order_payload_rounds_sub_dollar_prices_to_four_decimals`.

**Why the split**: the old model coupled "how far away is the stop" and
"how many shares to buy" through a single risk-budget number, which meant
changing one strategy's stop distance silently changed its position size
too. The new model decouples them completely -- `stop_loss_pct` answers
"where does the stop go," `max_position_size_pct` answers "how many
shares," and neither one affects the other's math.

**Verification status**: `tests/test_risk_engine.py` covers all of the
above (including the accept/reject-only behavior of #2 and the
buying-power-vs-equity distinction in #4), but -- like every other
threshold in this codebase -- these defaults are unvalidated starting
points, not backtested or run live yet.

**Daily trade counters vs. actual trades**: `max_trades_per_day` and
`max_trades_per_ticker_per_day` (default 2) are incremented at the very
end of `evaluate()`, the instant a signal is approved -- but approval only
means the signal passed risk criteria, not that a real position ever
opened. `OrderManager.submit_signal` still has to hand the resulting order
to the broker, which can reject it immediately, or accept it and later
resolve it to `REJECTED`/`CANCELED`/`EXPIRED` without ever filling (a
symbol rejected for being outside trading hours, a transient broker-side
rejection, etc.) -- confirmed as a real production bug: two such
broker-level rejections on the same symbol, with zero positions ever
opened, silently exhausted that symbol's entire `max_trades_per_ticker_per_day`
budget for the rest of the session, later surfacing as a confusing
`max_trades_per_ticker_hit` risk event on a symbol that had never actually
been traded.

`RiskEngine.record_entry_order_failed(symbol, now)` rolls back that
optimistic increment (`trade_count` and `trades_per_ticker[symbol]`, both
floored at 0) when this happens. `TradingLoop` calls it from all three
places an approved entry can still fail to become a real position:
`_submit_entry`'s immediate-non-fill branch (the broker rejected the order
right away), `_submit_entry`'s catch-all `except Exception` branch (see
below -- the broker call itself raised, not just returned a rejected
status), and `_poll_pending_entry`'s `REJECTED`/`CANCELED`/`EXPIRED` branch
(the order was briefly pending before failing). A genuine risk-engine-level
rejection (`OrderRejected`, caught earlier in `_submit_entry`) never calls
this -- `evaluate()` never incremented anything in that case, since the
signal was rejected before reaching the increment at the bottom of the
method. `record_trade_closed` (realized P&L, post-loss cooldown) is a
separate, unrelated counter -- this only affects the two daily
trade-count gates.

**`_submit_entry`'s catch-all exception handler** (distinct from the bug
above, but discovered from the same real production report -- a candidate
seen stuck in `TRIGGERED` for over a minute before self-resolving to
`ARMED` with the reason "no pending order found for TRIGGERED candidate"):
`TriggerEngine.on_snapshot` transitions a candidate ARMED -> TRIGGERED as a
side effect *before* `_submit_entry` is ever called in the same tick. If
`order_manager.submit_signal` raises anything other than the expected
`OrderRejected` (a real broker/network error, a malformed response, a
bug -- not a controlled risk-engine rejection), `_submit_entry` used to
have no handler for it, so the exception propagated all the way up to
`_process_all_candidates`'s generic `except Exception: logger.exception
("Unhandled error processing candidate ...")` catch-all. The candidate was
left sitting in `TRIGGERED` with **no order ever recorded** in
`_pending_entry_orders`, since the failure happened before that dict ever
got populated -- only `_poll_pending_entry`'s "shouldn't happen" fallback
(`pending is None`) would eventually notice and revert it to `ARMED`,
which could take a while if something else (e.g. `get_snapshot` also
failing for that symbol) delayed `_poll_pending_entry` from even running.
`_submit_entry` now has its own `except Exception` handler that logs the
real traceback (much more specific than the generic catch-all's message),
rolls back the risk-engine counters via `record_entry_order_failed` (since
`evaluate()` already ran and approved the signal inside `submit_signal`
*before* the broker call that failed), and reverts the candidate to
`ARMED` immediately -- instead of relying on `_poll_pending_entry`'s
fallback to eventually clean up a state it was never designed to be the
primary path for.

**The exit-submission side of the same gap -- a stop-loss that silently
never fires.** `_submit_entry`'s catch-all above only covers *entries*.
`_manage_position`'s own exit submission (`order_manager.submit_signal`
for the `EXIT`/`SCALE_OUT` signal `PositionManager.check_exit` produces)
and `_close_all_positions_now`'s equivalent (shared by the kill switch and
the end-of-core-hours auto-flatten) both had the identical narrow
`except OrderRejected` and nothing else. Confirmed as a real incident, not
just a theoretical gap: a position sat well past its `stop_price` with the
stop never firing, because whatever `broker.place_order` raised wasn't
`OrderRejected` and so propagated out of `_manage_position` entirely,
landing only in `_process_all_candidates`'s generic per-candidate
`except Exception` -- which kept the loop alive (so `check_exit` *did*
get a fair retry every subsequent tick) but gave no specific signal about
which step failed or why, making this materially harder to diagnose under
time pressure than the entry-side version of the same class of bug. Both
call sites now also catch `Exception` broadly and log specifically that
the *exit submission* failed for that symbol, with the real traceback,
before returning/continuing exactly as the `OrderRejected` branch already
did -- no position/candidate state changes here (unlike `_submit_entry`
reverting `TRIGGERED` back to `ARMED`), since `check_exit` re-evaluates
fresh from scratch every tick regardless of what happened on the previous
one. This does NOT retry with backoff or alert anyone by itself --
persistent, per-tick log noise for a genuinely stuck exit is the current
behavior, and the dashboard/logs are still where a human has to notice a
position that keeps failing to close every single tick.

**Position tracking can be lost on a broker-side fill reconciliation
failure.** `TradingLoop._confirm_entry_filled` is the *only* place a filled
entry order becomes a locally-tracked position (`self._positions`), and by
the time it runs, `_poll_pending_entry` has already popped the symbol out
of `_entry_signals`/`_pending_entry_orders` -- so if anything inside it
raises before `self._positions[symbol]` is assigned, the position that
just filled at the broker becomes **permanently invisible to the bot**: no
stop-loss/target management, not shown as an open position anywhere,
buying power silently consumed with nothing to show for it, and the
candidate reverts to `ARMED` on the very next tick via
`_poll_pending_entry`'s `pending is None` fallback ("no pending order
found for TRIGGERED candidate") since its pending-order record is already
gone.

This happened in production: a real, populated `get_account_position()`
response hit a field-name mismatch inside `_position_from_dict` --
verified only against an *empty* response during integration (see "Webull
integration" below), the field names were always a best-effort guess --
which raised a `KeyError`, and the old code here only caught
`StopIteration` ("no matching position found") around this lookup, not
that. Two fixes, at two different layers, because either alone leaves a
real gap:

1. **`_confirm_entry_filled` now catches any `Exception`** from the
   `broker.get_positions()` reconciliation lookup, not just
   `StopIteration`. That lookup is strictly a nice-to-have (a more accurate
   `avg_entry_price`/`quantity` than the signal/order already give us) --
   it must never be allowed to prevent local tracking from being recorded.
   Any failure now falls back to `signal.reference_price`/`order.quantity`
   exactly like the "no matching position" case always did, and
   `self._positions[symbol]` plus the `ENTERED`/`MANAGING` transition
   always happen regardless.
2. **`WebullBrokerClient.get_positions()` no longer lets one bad row take
   down every position at once.** Before this, a single row
   `_position_from_dict` couldn't parse raised straight out of
   `get_positions()` entirely -- which doesn't just affect fill
   reconciliation above, it's also `RiskEngine.evaluate`'s own
   `open_positions` lookup inside `OrderManager.submit_signal`, so one
   unparseable row could have blocked *every future entry* the moment it
   appeared, for every symbol, not just the one it belonged to.
   `get_positions()` now parses each row in its own `try/except`, logs the
   raw row (`logger.exception(..., "raw row: %r", row)`) and skips just
   that one on failure, keeping every other real position visible.
   `_position_from_dict`'s `symbol` field also gained the same
   multi-key-fallback treatment every other field already had
   (`raw.get("symbol") or raw.get("ticker_symbol") or
   raw.get("instrument_symbol")`) instead of a bare `raw["symbol"]` --
   the exact field name is still unverified against a real populated row
   (unchanged from before), but a wrong guess now degrades to "this one
   row is skipped and logged" instead of "every position vanishes."

The logged raw row from fix #2 is exactly the data needed to correct
`_position_from_dict`'s field-name guesses the next time this actually
fires against a real, non-empty account -- check the logs for
`"Failed to parse a position row"` after any live session with real fills.
(Update: this has since happened for real -- see the module docstring's
"account_v2.get_account_position" entry for the confirmed real field names.)

**`self._positions` can drift out of sync with the broker in EITHER
direction, and `reconcile_positions_from_broker` corrects both.** The
fixes above make a *running* process robust to a broker-lookup failure,
but `self._positions` is still just a plain in-memory dict with no
persistence or cross-checking of its own. Two distinct real incidents, one
in each direction:

1. **Broker has a position, bot doesn't (a restart wipes tracking).** Every
   process restart -- a deploy, a crash, a VPS reboot -- previously wiped
   tracking for any position that was genuinely open and correctly tracked
   a moment before, landing in the exact same "position exists at the
   broker, bot has zero record of it" state as the parsing-failure
   incident, just reached a different way.
2. **Bot has a position, broker doesn't (an external close goes
   unnoticed).** `scripts/list_and_close_positions.py` closes a position by
   calling `broker.place_order` directly, entirely outside the running
   `TradingLoop` process -- confirmed live: the dashboard kept showing a
   position as open long after the script had genuinely closed it at the
   broker, since nothing in this codebase ever told the running process
   that happened. A manual close from the Webull app itself would produce
   the identical symptom.

`reconcile_positions_from_broker()` fetches `broker.get_positions()` once
and does both directions in the same pass: **adopts** any symbol the
broker reports that isn't already in `self._positions` (inserting it and
building a fresh `Candidate` through the always-legal
`WATCHING -> HEATING_UP -> ARMED -> TRIGGERED -> ENTERED -> MANAGING` chain
tests use, so `_process_all_candidates` picks it up on the very next tick
like any other managed position), and **drops** any symbol still in
`self._positions` that the broker no longer reports at all (removing it
and transitioning its candidate `MANAGING -> EXITED -> COOLDOWN`) -- except
a symbol with a pending exit order already in flight
(`self._pending_exit_orders`), which is left alone so
`_poll_pending_exit`/`_dispatch_exit_finalization` can finish it normally
through the usual path instead of having it yanked out from under that
machinery.

**Adoption always rebuilds a fresh `Candidate` rather than advancing
whatever one already exists, and this isn't optional -- it fixes a real
bug in adoption's first version.** A single-hop transition straight to
`MANAGING` is only ever legal from `ENTERED`
(`state_machine._ALLOWED_TRANSITIONS`); the original code tried it
directly on whatever existing candidate it found, which raised
`InvalidStateTransition` for a candidate in `TRIGGERED`, `ARMED`, or
anywhere else that isn't `ENTERED`/`MANAGING` already. Confirmed live as
the reason candidates stayed stuck in `TRIGGERED` indefinitely: `TRIGGERED`
is *exactly* the state a candidate ends up in when its entry filled at the
broker but this process never confirmed it -- precisely the case adoption
exists to fix -- so the fix crashed on the exact input it was built to
handle, aborting reconciliation for every symbol still left in that pass
(a single unguarded loop, so one exception mid-iteration skips everything
after it too). The fix: if an existing candidate isn't already
`ENTERED`/`MANAGING`, discard it and build a fresh one through the full
chain instead of trying to advance the stale one -- its state already
disagrees with reality (the broker has a real fill; its state machine says
otherwise), so nothing about it is worth preserving. The old candidate's
`_persisted_transition_counts[symbol]` entry is cleared at the same time,
so `_flush_state_transitions` persists the fresh object's complete history
from zero rather than misapplying an index meant for a different object's
transition list.

**The drop path required a THIRD real incident's worth of hardening
(2026-08-12): one missing pass alone is not trustworthy evidence a
position actually closed.** Confirmed live, post sandbox-account-reset,
during a stretch of sustained 429 contention (multiple positions
simultaneously retrying bracket-attach/cancel calls -- see the
"support_trading_session" and quantity-ceiling incidents elsewhere in
this doc for the same window): a genuinely open position (BIVI) was
dropped from `self._positions` and its candidate pushed straight to
`COOLDOWN` -- ending not just broker-side but ALL software-side
management too, since `PositionManager.check_exit` is never called again
for a candidate that isn't `MANAGING` -- after exactly ONE
`reconcile_positions_from_broker` pass came back without it. Crucially,
`broker.get_positions()` itself never raised (the try/except around
`_get_positions_for_tick` already handles that failure mode, logging and
skipping the whole pass) -- it returned an ordinary 200 whose body simply
omitted a position this bot's own `orders` table confirmed was filled
minutes earlier. The 429 immediately preceding the drop in the logs was
from a *different*, unrelated call in the same tick, not the
`get_positions()` call itself -- but it's strong circumstantial evidence
the account was under exactly the kind of contention where a "successful"
200 response isn't guaranteed to be a complete one.

**Fix:** a new `self._missing_from_broker_counts: dict[str, int]` streak
counter, keyed by symbol, incremented each pass a tracked position is
absent from `broker_symbols` and reset to nothing the moment that symbol
reappears in any later pass. The drop-and-transition-to-COOLDOWN branch
now only fires once a symbol's streak reaches
`TradingLoopConfig.position_missing_confirmations_required` (2 default)
-- below that, the position is logged as "not yet treating as closed
externally" and left fully tracked and managed exactly as if the pass
hadn't happened at all, so a transient/incomplete response costs nothing
beyond a log line. This trades up to one extra
`position_reconcile_interval_seconds` of detection latency for a
GENUINE external close (still confirmed and acted on -- a real close
stays missing on every subsequent pass too) against never again silently
abandoning a real, still-open, unprotected position on a single flaky
poll. See `tests/test_trading_loop.py`'s
`test_reconcile_does_not_drop_a_position_missing_from_a_single_pass` and
`test_reconcile_miss_streak_resets_once_the_broker_reports_the_symbol_again`.

**A confirmed external close never produced a `Trade` record at all --
fixed the same day.** Once BIVI's close was correctly confirmed (across
two reconcile passes) and removed from `self._positions`/the dashboard,
it still never appeared in Trade History or Performance. Root cause: the
drop branch above only ever called `del self._positions[symbol]` plus the
candidate-state transitions -- unlike `_finalize_exit` (this process's own
internal exit-fill path), it never called `record_trade()` or
`self.on_trade_closed`, so a real, completed round-trip trade simply
vanished from history the moment it was detected as externally closed.

**Fix:** a new `TradingLoop._build_trade_for_external_close(symbol,
position, now)`, structurally parallel to `_build_trade_from_fill` but
built for the case where there's no local `Order`/`Signal` to build from
at all (this process never submitted or saw a fill for the exit). It
first tries `broker.poll_fills()` for a real exit-side fill for this
symbol at/after `position.opened_at` -- almost certainly the actual
closing trade if the broker's fill history is queryable -- taking the
most recent match's price as `exit_price`. If `poll_fills` comes up empty
or raises, it falls back to `position.stop_price or position.target_price
or position.avg_entry_price`, the exact same fallback chain
`_build_trade_from_fill` already uses for its own worst case. Tagged with
a new `ExitReason.EXTERNAL_CLOSE` (`enums.py`) so it reads distinctly
from every other exit reason in history -- a reader looking at Trade
History can tell at a glance which rows are this process's own confirmed
exits versus a best-effort reconstruction of something that happened
outside it. Wired into the drop branch right before `del
self._positions[symbol]`: builds the trade, calls
`risk_engine.record_trade_closed` (so the daily-loss-limit accounting
stays consistent regardless of which path closed a position), and invokes
`self.on_trade_closed` exactly like `_finalize_exit` does. See
`tests/test_trading_loop.py`'s
`test_reconcile_records_a_trade_for_an_externally_closed_position_using_a_real_fill`
and `test_reconcile_records_a_trade_for_an_externally_closed_position_falling_back_without_a_fill`.

**The fallback chain used to land on `avg_entry_price` far too easily,
fabricating an exact $0.00 P&L -- fixed the same day.** Confirmed live
2026-08-12 via a dashboard screenshot: WCT closed with neither a matched
`poll_fills` result nor a `stop_price`/`target_price` set (both `None` by
the time the position was confirmed externally closed), so the chain fell
straight to `position.avg_entry_price` -- recording entry=exit=1.04, an
exact $0.00/0.00% "trade" no matter what actually happened to the real
position. This was not a display/formatting limitation (negative P&L
already renders correctly elsewhere in the dashboard, e.g. a real
-$55,979.72 loss shown in red) -- the fallback price itself was simply
wrong. **Fix:** insert a fresh `broker.get_snapshot(symbol).last_price`
call ahead of the `avg_entry_price` last resort -- a live quote taken at
detection time is still real market data, and strictly better evidence
than silently assuming a break-even close. `avg_entry_price` is now only
reached if even that live snapshot call fails (e.g. a broker error). See
`tests/test_trading_loop.py`'s
`test_reconcile_external_close_falls_back_to_a_live_snapshot_not_entry_price`
and `test_reconcile_external_close_falls_back_to_entry_price_only_as_a_last_resort`.

**Still an approximation, not a guarantee of the real fill price/time --
a Webull order-history backfill would be strictly better, and was
explored the same day but is not yet working.** `order_v3.get_order_history`
(`account_id`, `page_size`, `start_date`, `end_date`) is a real, paginated
endpoint (`/openapi/trade/order/history` under the hood, `version='v2'`
despite being exposed under the SDK's `order_v3` namespace -- an SDK
quirk, not this codebase's choice) that in principle could backfill
`trades` with the ACTUAL fill price/time for every historical order,
including entries and exits that predate this fix or were never even
seen by a running process -- strictly more reliable than
`_build_trade_for_external_close`'s live-detection-time approximation
above. A live query against this sandbox account (2026-08-12, both with
an explicit `start_date`/`end_date` and with no date filter at all, which
the docstring says defaults to the last 7 days) came back an empty list
both times, despite the account's local `orders` table showing 70+ real
orders (including confirmed fills) for that exact window. Not yet
determined whether that's a request-shape issue on this codebase's side
or a genuine limitation of this endpoint in `TRADING_MODE=sandbox`
specifically (plausible -- several other sandbox behaviors this session
have differed from documented/expected live behavior, e.g. the
`support_trading_session` entitlement flips) -- needs a live, credential-
authenticated re-check (and ideally a `get_order_open` comparison, since
BIVI/BQ's resting bracket orders should show up there if the account's
order-query endpoints are populated at all) before this path is worth
building real backfill logic against. **Do not write DB-writing code
against this endpoint's assumed field shape without first confirming a
real non-empty response** -- this project has been burned by exactly that
mistake before (`_position_from_dict`'s originally-unconfirmed field-name
guesses, see the module docstring's "account_v2.get_account_position"
history).

**Update, same day: `get_order_open` (currently-resting orders) DOES
return real data for this account** -- a live check against a real OCO
bracket (its two legs, one `SUBMITTED` one `CANCELLED`) came back
correctly populated. That narrows the mystery: this account's order-query
endpoints work in general, so `get_order_history` specifically not
returning anything (rather than every read endpoint being empty on this
sandbox) is the open question -- still not resolved as of this writing.
Next probe planned: `get_order_detail` against a `client_order_id`
already confirmed `FILLED` in the local `orders` table, to check whether
Webull retains any record of an already-filled order on this account at
all before concluding `get_order_history` itself is broken/unsupported
here.

**A real, serious incident (2026-08-12) surfaced while investigating
trade history: BIVI, believed closed based on an earlier (mistaken) user
report, was still genuinely open at the broker** -- confirmed via a
direct, uncached `account_v2.get_account_position` call: 199,999 shares,
cost basis $2.74, then trading at $1.49, an unrealized loss of roughly
**$250,000 (-45.6%)**. Its entry fill earlier the same day was only
44,729 shares -- the position had grown nearly 4.5x since then. The
leading explanation, consistent with every other bug fixed this same
session: the price-rounding bug (see the "Order prices are rounded"
entry above) meant BIVI's broker-side bracket never attached, leaving it
on software-only management; the reconcile false-drop bug (see
`reconcile_positions_from_broker`'s docstring) then wrongly declared it
"closed externally" on a single flaky poll and pushed its candidate to
`COOLDOWN`; once that 15-minute cooldown timer expired, the candidate
cycled back to `WATCHING` with zero awareness a real position was still
open at the broker; and this loop then fired a genuine SECOND entry on
BIVI, averaging the cost basis down and growing the position all the way
to Webull's own 200,000-share order ceiling. Both the price-rounding bug
and the reconcile false-drop bug were already fixed earlier the same day
-- this incident is what those fixes were protecting against, discovered
after the fact rather than before.

**Fix (defense-in-depth, independent of local tracking entirely):**
`TradingLoop._submit_entry` now calls `self.broker.get_positions()`
directly -- NOT `self._get_positions_for_tick()` -- immediately before
any new entry order is submitted, and refuses the entry outright
(reverting the candidate to `ARMED`, not `TRIGGERED`) if the broker
already reports a nonzero-quantity position for that exact symbol.
Deliberately does not use the tick-level position cache
`reconcile_positions_from_broker`/`_maybe_verify_entry_via_positions`
share: this check runs BEFORE the entry it's guarding creates a new
position, so caching its (necessarily pre-entry, position-not-yet-open)
result would poison `self._tick_positions_cache` for every later
same-tick caller expecting to see the position this call is about to
create -- confirmed while building this fix: it silently broke
`_maybe_verify_entry_via_positions`' self-heal path, which stopped seeing
its own just-placed fill because it kept reusing this call's stale empty
cache instead of fetching fresh (see
`tests/test_trading_loop.py`'s six now-passing
`test_verify_via_positions_*`/`test_poll_pending_entry_self_heals_*`
tests, which briefly broke while diagnosing this). The one extra
`get_positions()` call this costs happens once per entry ATTEMPT, not
once per tick or once per candidate -- entries are comparatively rare
events, so this is an acceptable price for a check that's supposed to be
trustworthy independent of everything else in this process. A
`get_positions()` failure during this specific check doesn't block the
entry (logged, then proceeds as if the check hadn't run) -- this is
meant as defense-in-depth layered on top of `RiskEngine.evaluate`'s own
gating, not a replacement for it, and a transient broker/network hiccup
here shouldn't cost a legitimate entry. See
`tests/test_trading_loop.py`'s
`test_submit_entry_refuses_a_duplicate_when_broker_already_has_the_position`,
`test_submit_entry_proceeds_normally_when_broker_has_no_existing_position`,
and `test_submit_entry_proceeds_when_the_broker_check_itself_fails`.

**A stuck exit-order retry had no backoff at all -- fixed the same day
(CYCU/SCKT).** `_manage_position`'s `except Exception:` branch (around
software-side exit submission, see its docstring for the RDGT incident
that added it) logs and returns on a `broker.place_order` failure so
`PositionManager.check_exit` re-evaluates fresh next tick -- correct in
principle (an exit must never give up), but with no throttle of its own,
"next tick" meant every single `poll_interval_seconds` (5s default)
regardless of how many times the exact same call had already failed.
Confirmed live: a genuine stop-loss condition on two positions
simultaneously (CYCU, SCKT) kept retrying `place_order` every 5s for
many consecutive minutes, each attempt failing on
`TOO_MANY_REQUESTS` -- the retries themselves were adding to the exact
rate-limit contention blocking them, a self-reinforcing loop, while the
unrealized loss on one of the two grew past $70,000. Distinct from the
`_attach_broker_bracket` circuit breaker (`broker_bracket_attach_failures`,
which gives up permanently after N failures and falls back to
software-only management) -- an exit submission is the last line of
defense already; there is no "fall back" state to give up into, so
giving up was never an option here.

**Fix:** two new `Position` fields, `exit_submission_failures` (a
consecutive-failure counter) and `last_exit_submission_attempt_at`.
Right after `check_exit` returns a real signal, `_manage_position` now
checks whether it's still within an exponential backoff window computed
from those two fields (`exit_submission_backoff_base_seconds` \*
2^(failures-1), capped at `exit_submission_backoff_max_seconds` -- 5s,
10s, 20s, 40s, 60s(capped), 60s, ... with the defaults) and, if so,
returns immediately without attempting the broker call at all -- zero
network cost for a tick that would only have failed again anyway. On an
actual failure the counter increments and the timestamp updates (logged
now with the computed next-retry delay, not just "will retry next
tick"); on success the counter resets to zero. This never stops
retrying -- only slows down how OFTEN it retries, trading a few extra
seconds of an already-losing position staying open for not actively
worsening the rate-limit contention that's blocking its own exit. See
`tests/test_trading_loop.py`'s
`test_manage_position_backs_off_after_a_failed_exit_submission` and
`test_manage_position_exit_backoff_caps_and_resets_on_success`.

**Longer-term direction discussed the same day, not yet built:** the
user's own diagnosis was that ALL of this session's rate-limit-driven
incidents (this one, the earlier BIVI/reconcile saga, candidates
starving during bracket-attach storms) share one root cause -- this bot
tracks order/position state by polling REST endpoints
(`get_order_status`, `poll_fills`, `get_positions`) far more often than
necessary, and wants position/order tracking to move to Webull's push-
based gRPC trade-events stream (`webull.trade.trade_events_client.
TradeEventsClient`, see `scripts/verify_trade_events_streaming.py`)
instead, cutting REST call volume at the source rather than continuing
to patch each individual symptom. Live-confirmed the same day: the
sandbox host is `events-api.sandbox.webull.com` (not the SDK's bundled
default `events-api.webull.com`, which rejects this account's app key
outright with `PERMISSION_DENIED`), and a `do_subscribe()` call
succeeds against it -- but no real order/position event has been
captured yet (the listener ran clean with zero messages during a
window where, per the logs, real order activity WAS happening
elsewhere in the account, which is itself worth understanding before
concluding streaming is reliable enough to become the sole source of
truth). This backoff fix stops the immediate bleeding; the streaming
migration remains the intended actual fix and is the next real piece of
work, not yet started.

**A second, narrower fix landed the same day, ahead of the streaming
migration: `RateLimiter.exclusive()` (`retry.py`).** The user's
follow-up question, once the CYCU/SCKT retry storm calmed down: could
ALL other Webull API traffic simply be paused for the duration of any
order placement, so it never has to compete for the rate-limit budget
at all? `CallPriority.CRITICAL` already wins contention against
`BACKGROUND` traffic, but does nothing when SEVERAL genuinely
`CRITICAL` calls are simultaneously in flight (this exact incident: a
stuck exit retry, a bracket-attach retry, and `reconcile`'s
`get_positions()` all competing for the same ~1 req/s ceiling at once)
-- priority alone can't help there, since they're all the same
priority. `exclusive()` is a stronger mechanism built specifically for
this: a context manager that, while held by one thread, makes every
OTHER thread's `RateLimiter.wait()` call block outright -- regardless
of its own priority -- until the holder releases it. Implementation:
a new `_exclusive_holder` field (an OS thread id, guarded by the same
condition variable `wait()` already uses) that `wait()`'s loop checks
first, before its normal ticket/pacing logic -- any thread that isn't
the current holder just waits on the condition variable again;
`threading.get_ident()` is what makes the holder's OWN thread exempt
(so its own paced `call_with_retry` attempts inside the block proceed
completely normally), while every other thread, at any priority, is
blocked. `WebullBrokerClient.place_order` and `place_oco_bracket` --
every code path that submits a genuinely new order (entries, exits,
and broker-side stop/target brackets alike) -- now wrap their
`call_with_retry` call in `with webull_limiter.exclusive():`, so the
single highest-stakes moment this client has gets the account-wide
budget entirely to itself for that stretch. `cancel_order`/
`modify_order` were deliberately left unwrapped -- lower-stakes cleanup
actions, and holding exclusive access longer than necessary just delays
everything else without a comparable safety benefit. See
`tests/test_retry.py`'s `test_exclusive_blocks_other_threads_
regardless_of_priority`, `test_exclusive_does_not_block_the_holders_
own_thread`, `test_exclusive_releases_on_exception`, and
`test_exclusive_serializes_concurrent_holders`.

This narrows, but does not eliminate, the motivation for the streaming
migration above -- `exclusive()` protects the moment an order is
actually placed, but candidate discovery, reconcile, and every other
poll-based REST call this bot makes are still real requests competing
for the same shared account-wide ceiling the rest of the time. The
streaming migration remains the intended actual fix for that broader
problem and is still the next real piece of work, not yet started.

Called from `_process_all_candidates`, throttled by
`TradingLoopConfig.position_reconcile_interval_seconds` (default 30s) --
but firing immediately on that method's very first-ever call regardless,
since `self._last_position_reconcile` starts unset. Both `run_once` and
`run_forever` route through `_process_all_candidates`, so every real
entrypoint reconciles before any candidate is processed, with no separate
startup call needed. Direction 2 specifically needs the *periodic* re-run,
not just a one-time startup check -- an external close can happen at any
point while the process keeps running, not only right before it starts.

An adopted position (direction 1) has no originating `Signal` to pull a
real `stop_price`/`target_price` from (that only ever exists at the moment
a strategy fires, and this position may have opened in a previous
process's lifetime) -- deliberately conservative values instead of
leaving it unprotected, computed from the same flat `stop_loss_pct` /
`min_risk_reward_ratio` pair a real signal's flat-% strategies use (see
`RiskConfig.stop_loss_pct`'s docstring for which strategies those are):

```
stop_price   = current_price -+ stop_loss_pct%      (long/short)
target_price = current_price +- stop_loss_pct% * min_risk_reward_ratio
```

**Simplified back to this flat-%-of-price form (2026-08-11), after briefly
being equity/quantity-based.** An earlier version of this formula solved
for the stop that would make this position's already-fixed share count
risk exactly a configured % of account equity (`risk_budget_dollars =
account_equity * risk_per_trade_pct / 100`, then `per_share_risk =
risk_budget_dollars / quantity`) -- that only made sense while the
underlying config field (`risk_per_trade_pct`) meant "% of equity to risk
on this trade." The moment that field was renamed to `stop_loss_pct` and
repurposed to mean a genuine per-position stop distance instead (see
"Position sizing" above), the equity-based version would have quietly
started computing the wrong thing, so it was reverted to the simple flat
form shown above. No `get_account_equity()` call, and no degenerate-
distance fallback, are needed at all now -- a flat % is well-defined
regardless of share count or account equity, so there's no edge case to
fall back from. `strategy_name` is set to the sentinel
`"reconciled_at_startup"` so an adopted position is always
distinguishable from a real signal-driven one in the trade history. A
`get_positions()` or per-symbol `get_snapshot()` failure during
reconciliation is logged and skipped, not fatal to the rest -- consistent
with every other broker-loop failure mode in this file (kill-switch
flatten, batched snapshot fetch, etc.).

## Position management (exits)

**Important, easy to assume otherwise: `RiskEngine` never sets or
overrides `stop_price`/`target_price`.** It only *reads* the strategy's
`suggested_stop`/`suggested_target` to (a) size the position (see "Risk
sizing" above) and (b) gate entry via `min_risk_reward_ratio` -- rejecting
a signal outright if its own stop/target combo doesn't already meet the
ratio. If the signal is approved, the exact `suggested_stop`/
`suggested_target` values the *strategy* computed become
`Position.stop_price`/`target_price` unchanged (`trading_loop.py:_confirm_entry_filled`).

Unlike an earlier version of this design, every strategy's target
computation IS wired to `RiskConfig.min_risk_reward_ratio` -- none of them
hardcode their own reward multiple anymore. Each `Strategy.__init__` takes
a `reward_risk_ratio_fn: Callable[[], float]` (default `lambda: 2.0` when
not supplied, e.g. in tests), called fresh every time a target is computed
(`target_price = entry_price + risk_per_share * self._reward_risk_ratio_fn()`).
`main.py`'s `build_trading_loop` constructs `RiskEngine` first and passes
`lambda: risk_engine.config.min_risk_reward_ratio` to all 8 strategies --
the exact same live object the entry gate itself reads -- so raising or
lowering that value in the dashboard's Settings panel changes what every
strategy targets on its very next signal, not just what the gate demands.
A strategy's own freshly-computed target can therefore never fail its own
gate check. This also gave `VolumeIgnitionStrategy` a real target for the
first time (previously `None` -- see its docstring for why that's no
longer the right call now that a target hit is a partial exit, not a full
close).

Once a position is open, `PositionManager.check_exit` (`position/position_manager.py`)
runs once per poll tick. Two stop-ratcheting updates happen first (both
only ever tighten `stop_price`, never loosen it, and the second only
activates once the first partial exit has happened -- see below), then
four conditions are checked in order -- first match wins:

1. **Breakeven** (`PositionManagementConfig.breakeven_trigger_pct`, default
   5%) -- once `price >= avg_entry_price * 1.05`, `stop_price` jumps to at
   least `avg_entry_price` (a guaranteed no-loss floor) if it wasn't
   already there or better. Runs on every tick regardless of whether a
   partial exit has happened yet.
2. **Trailing stop** (`trailing_stop_pct`, default 3%) -- recomputed every
   tick as `current_price * (1 - 3%)`, replacing `stop_price` whenever
   that's tighter (higher) than whatever breakeven left it at. **Only
   takes effect once `Position.partial_exit_taken` is `True`** (i.e. after
   check ② below has fired at least once) -- before that, the stop is
   governed solely by the strategy's initial stop and the breakeven rule,
   not this continuous %-of-current-price math. This is a deliberate,
   explicit choice: trailing from tick one would fight the breakeven
   floor and can ratchet the stop up before the trade has actually proven
   itself by reaching its target. See `_maybe_update_trailing_stop`'s
   docstring for the full reasoning. One consequence: a strategy that
   somehow set no target at all would never reach `partial_exit_taken`,
   so its position would ride on the initial stop + breakeven alone for
   its whole lifetime -- not a concern today since all 8 strategies set one.

Both apply identically to every open position regardless of which of the 8
strategies triggered its entry -- this is universal position management,
not per-strategy.

3. **① Stop hit** -- `price <= stop_price` (whatever breakeven/trailing
   ratcheted it to). Exits the *entire* remaining position
   (`STOP_LOSS`/`TRAILING_STOP`).
4. **② Target hit -- PARTIAL exit** -- `price >= target_price` (only if the
   strategy set one, and only the *first* time -- see
   `Position.partial_exit_taken` below). Unlike every other check here,
   this does **not** close the whole position: it emits a `SCALE_OUT`
   signal that sells half (`OrderManager` floors to a whole share count),
   leaving the rest open to keep riding the breakeven/trailing-stop rules
   above rather than being fully capped at the target. This was a
   deliberate choice among three options considered (retire the hard
   target entirely; partial exit; leave as a full exit) -- partial exit
   banks some profit at a known level while still letting the remainder
   run. If the position is too small to split into two whole-share halves
   (`quantity < 2`), this falls back to a full exit (`PROFIT_TARGET`)
   instead of a meaningless zero-share partial.
5. **③ VWAP failure** -- `price` drops more than `vwap_failure_buffer_pct`
   (default 0.5%) below VWAP, regardless of where the stop sits. Full exit.
6. **④ Time limit** -- position has been open `>= time_limit_minutes`
   (default 30) with none of the above triggered. Full exit.

**`Position.partial_exit_taken`** (set the moment the partial `SCALE_OUT`
fill is confirmed -- `TradingLoop._finalize_partial_exit` /
`BacktestEngine._execute_exit`) prevents check ② from firing again on
every subsequent tick that price happens to stay above target; once set,
the remaining shares are governed purely by ①/③/④ (with ①'s bar itself
still rising via the breakeven/trailing rules) for the rest of the trade.
A full `EXIT` (①/③/④, or ② with too few shares to split) still closes the
position entirely, pops it from tracking, and moves the candidate
`MANAGING -> EXITED -> COOLDOWN`, same as before this partial-exit design
existed. A `SCALE_OUT` (②, the normal case) leaves the candidate
`MANAGING` and the position open with reduced `quantity` -- **only** the
sold portion becomes a `Trade` record (`exit_reason=PARTIAL_PROFIT_TARGET`),
and a second `Trade` is recorded later whenever the remainder eventually
does fully exit.

**Only `VolumeIgnitionStrategy` never reaches check ②** (it sets no
`target_price` at all -- see "Entry strategies" above), so its full
position rides on ①/③/④ from the very first tick; the other 7 strategies
bank half at target and let the other half do the same from that point
forward.

Both `breakeven_trigger_pct` and `trailing_stop_pct` are live-adjustable
from the dashboard's Settings panel via `GET`/`POST /api/position-settings`
(a separate config object -- `PositionManager.config` -- from `RiskEngine.config`,
hence the separate endpoint pair; see "Dashboard" below), taking effect on
the very next `check_exit()` call for every open position.

## Broker-side (resting) stop/target management (2026-08-11)

Everything above describes checks ①/② running purely in software:
`TradingLoop` polls a snapshot, `PositionManager.check_exit` compares it
against `stop_price`/`target_price`, and only THEN submits a MARKET order.
That has a real gap a synthetic (software-only) stop can't close: if this
process is slow, crashed, mid-restart, or hits an unexpected exception
right when price crosses the line, nothing enforces the stop until the
next successful tick notices -- confirmed as a real incident (RDGT,
2026-08-11): a position sat well past its stop with a five-figure
unrealized loss because the software-side exit submission silently failed
with no retry. A resting order placed *at the broker* doesn't have this
problem: Webull enforces it directly, independent of whether this process
is alive, awake, or error-free at that exact moment.

**What changed.** Confirmed live the same day (`scripts/verify_bracket_orders.py`)
that Webull's OpenAPI supports attaching a real 2-leg `OCO` (One-Cancels-
Other) combo -- a resting `STOP_LOSS` order and a resting `LIMIT`
take-profit order sharing one `client_combo_order_id` -- to a position
that's **already open**, with no `MASTER` (entry) leg required. That's
deliberately not the only combo shape Webull's own docs show an example
of (a `MASTER`-anchored combo submitted atomically with the entry) --
attaching the bracket as a *second* call right after the entry fill is
confirmed avoids touching `TradingLoop`'s entry-fill pipeline at all
(`_submit_entry` → `_poll_pending_entry` → `_confirm_entry_filled`), which
had already had several real production bugs found and fixed in it this
same week. Also confirmed live: `cancel_order` needs each leg's own
`client_order_id`, not the combo-level id (`ORDER_NOT_FOUND` otherwise);
and `modify_order`/`replace_order`'s effect on a resting order's price was
**inconclusive** (the call reported "accepted" but the immediate readback
showed the field unset) -- so this feature never uses it, using
cancel-then-place-again instead everywhere a resting order's price needs
to change.

**Capability-gated, not a new interface method.** `WebullBrokerClient.place_oco_bracket`
is *not* part of the `BrokerClient` ABC (`interfaces/broker.py`) -- resting
orders only mean something against a real broker; `PaperBrokerClient` and
the backtest engine fill every order synchronously at market with nothing
to rest against, so requiring them to implement it would be meaningless.
`OrderManager._broker_supports_resting_orders()` checks for it with
`getattr`, the same pattern already used for `get_snapshots`/`get_raw_bars`.
A position falls back automatically to the pure-software behavior
described above whenever the connected broker lacks this capability at
all (tests, `PaperBrokerClient`, backtests) -- broker-side management is
an enhancement layered on top of the existing fallback, never a
precondition for a position being tracked at all. But when the broker
DOES support resting orders and a placement call simply fails (rejected
order, rate limit, network error), that fallback is only ever TEMPORARY
(extended 2026-08-11, see "Retrying a failed attach" below) -- riding on
software-only management for a few ticks while retrying is fine; giving
up on real broker-side protection permanently after one failed call is
not.

**The lifecycle, symbol by symbol:**

1. **Attach** (`TradingLoop._attach_broker_bracket`, called from
   `_confirm_entry_filled` right after a fresh entry fill, and again after
   startup/periodic adoption in `reconcile_positions_from_broker`). Builds
   a resting `STOP` leg at the full quantity and (if a target is set and
   the position is big enough to split into two whole-share halves -- the
   same floor-to-half-share rule `OrderManager.submit_signal`'s `SCALE_OUT`
   path already uses) a resting `LIMIT` leg at half the quantity, and
   places them as one `OCO` pair via `OrderManager.place_resting_bracket`.
   When there's no target -- which happens for two structurally different
   reasons -- a lone resting order protects the full remaining quantity
   instead: a too-small-to-split position (adoption never has a target
   either) gets a plain `STOP` via `place_resting_stop`, riding on it plus
   the breakeven rule for its whole lifetime, same as before this native
   trailing-stop path existed; a position that's already taken its one
   partial exit (`partial_exit_taken` is `True`) instead gets a native
   `TRAILING_STOP` via `OrderManager.place_resting_trailing_stop` (added
   2026-08-11 at the account owner's explicit instruction that Webull
   supports this order type for US equities -- NOT yet independently
   confirmed live the way the plain `OCO` bracket was; see
   `scripts/verify_trailing_stop.py` and
   `WebullBrokerClient._ORDER_TYPE_TO_WEBULL`'s docstring for the open
   question and what to do if it turns out unsupported). Stores both legs'
   broker order ids, the synced stop price, and whether the resting stop
   is a trailing order on `Position` (`broker_stop_order_id`,
   `broker_target_order_id`, `broker_stop_price_synced`,
   `broker_stop_is_trailing`). Before placing a trailing stop specifically,
   defensively cancels any resting order still attached to this position
   first (a `TRAILING_STOP` can't be added while another resting sell
   order still reserves the same shares) -- every call site already
   guarantees this holds without the extra cancel, but this doesn't rely
   on that invariant holding forever.
2. **`PositionManager.check_exit` steps aside** for checks ①/② the instant
   `position.broker_stop_order_id` is set (`broker_managed` in that
   method) -- the broker is already watching for that exact price cross,
   so this loop firing its own market order on the same cross would race
   the broker's own fill and risk over-selling. Checks ③ (VWAP failure)
   and ④ (time limit) are unaffected either way: neither has a broker-side
   resting-order equivalent, so `PositionManager` always decides and emits
   those itself. The breakeven/trailing math (the two stop-ratcheting
   updates described above) also keeps running unconditionally -- it only
   mutates `position.stop_price` in place, which has no way to reach the
   broker on its own by design (`OrderManager`'s docstring: it's the only
   component allowed to call broker order-placement methods).
3. **Poll for a broker-side fill** (`TradingLoop._poll_broker_bracket`,
   called from `_manage_position` before `check_exit` even runs, for any
   position carrying a resting order). If the stop leg filled, finalizes a
   full `EXIT` (`STOP_LOSS`) exactly the way a software-submitted exit
   would (`_dispatch_exit_finalization`) but without this loop ever having
   submitted the fill-causing order itself. If the target leg filled,
   finalizes a `SCALE_OUT` (`PARTIAL_PROFIT_TARGET`) the same way, **then
   immediately re-attaches a fresh lone resting order for the remainder**
   (`_attach_broker_bracket` again, now with no target -- `partial_exit_taken`
   is `True`, so this is the trailing-stop branch described in step 1, not
   a plain `STOP`): Webull's `OCO` logic auto-cancels the sibling leg the
   instant one fills, so the original stop (which was sized for the FULL
   original quantity) is gone the moment the target fills, and the
   remaining shares would otherwise be naked until the next tick's sync.
4. **Sync on a stop-price change** (`TradingLoop._sync_broker_protective_orders`,
   called from `_manage_position` whenever `check_exit` runs and finds no
   exit condition) -- **unconditionally a no-op once
   `position.broker_stop_is_trailing` is `True`**, since Webull is already
   moving that resting order on its own; `PositionManager`'s software-side
   trailing math still runs and mutates `position.stop_price` every tick
   either way, but purely for tracking/display at that point, with nothing
   left to push to the broker. Before that point (a plain `STOP`, pre- or
   never-partial), compares `position.stop_price` (which `check_exit`'s
   breakeven math may have just moved) against `broker_stop_price_synced`;
   if they differ, cancels **every** resting leg for this position (not
   just the stop) and calls `_attach_broker_bracket` again for a
   completely fresh resting order (or pair, if a target is still active).
   Cancelling both legs together, rather than trying to update just the
   stop leg's price in place, is deliberate: it sidesteps both
   `modify_order`'s inconclusive live result *and* the unconfirmed
   question of whether cancelling a single leg of an already-placed `OCO`
   combo cancels or orphans its sibling.
5. **Cancel before any software-submitted exit** -- a full `EXIT` for a
   broker-managed position can still happen (VWAP failure, time limit,
   the kill switch's flatten, the end-of-core-hours auto-flatten), and
   each of those cancels any resting stop/target legs first
   (`TradingLoop._cancel_broker_protective_orders`) before submitting its
   own market order, so nothing is left resting against a position that's
   about to be closed by a different order entirely.
   `reconcile_positions_from_broker`'s drop path (a position closed
   *outside* this process -- a manual close in the Webull app, an external
   script) does the same best-effort cleanup for whatever it still has ids
   for.

**Dashboard:** `GET /api/positions` includes `broker_managed`
(`position.broker_stop_order_id is not None`) per open position, shown as
a "Mgmt" column (`Broker` / `Software`) so it's visible at a glance which
positions are riding on a resting broker order right now versus the
pure-software fallback.

**Retrying a failed attach (2026-08-11).** Step 4 above
(`_sync_broker_protective_orders`) originally only fired when a resting
order already existed and its price needed to change -- a position whose
very first `_attach_broker_bracket` call failed (a rate limit hit during
a busy startup reconcile, a transient network error) had no way back to
broker-side management: it rode on pure software-only checks for the
rest of the trade. Given the whole point of this feature is closing the
"software-side exit silently failed" gap from the RDGT incident, a
placement failure quietly becoming permanent defeated its own purpose.

Fixed by widening `_sync_broker_protective_orders`, called every tick
`_manage_position` finds no exit condition, so it now also covers "no
resting order yet at all" (`position.broker_stop_order_id is None`): if
the connected broker supports resting orders at all (a cheap `getattr`
check -- never a real call attempt for a broker that fundamentally can't
succeed, like `PaperBrokerClient`), it calls `_attach_broker_bracket`
again. A transient failure (429, a brief network blip) now self-heals
within a few ticks (`poll_interval_seconds` apart) without this loop
ever blocking synchronously to wait for it -- deliberately NOT an
in-place tight retry loop inside a single tick, which would freeze every
other position's exit management for as long as it ran. Each individual
attempt still goes through `call_with_retry`'s own fast, 429-specific
inner retry (4 attempts with exponential backoff) for the sub-second
layer; the per-tick sweep is what makes recovery from anything else --
or a 429 burst that outlasts even that -- eventually succeed rather than
being given up on after one call. Every placement call in this chain
already uses `CallPriority.CRITICAL` (`place_order`/`place_oco_bracket`
in `WebullBrokerClient`) -- the highest rate-limiter tier, so a retry
here wins contention over discovery/resistance-refresh traffic exactly
like every other exit-critical call in this codebase.

**Exception, added 2026-08-12: this whole every-tick retry is skipped
outside core hours.** `_attach_broker_bracket` no-ops immediately
(before any broker call) whenever `now` is outside core hours -- a
resting `STOP_LOSS`-leg bracket is expected to be rejected there
regardless of `support_trading_session`, and retrying it every tick was
observed live burning this same CRITICAL-tier rate-limiter budget
continuously, starving discovery. Positions still open outside core
hours fall back to `PositionManager`'s pure-software checks for as long
as that lasts. See "Webull integration"'s extended-hours follow-up
below for the full incident and `OrderManager._order_type_and_limit_price`
for the matching MARKET-vs-LIMIT change on the entry/exit side.

## Extra position-based confirmation for a TRIGGERED entry (2026-08-11)

A `TRIGGERED` candidate (entry order submitted, not yet confirmed filled)
is normally confirmed purely by `_poll_pending_entry` polling
`OrderManager.get_status` -> `broker.get_order_status` every tick until it
resolves to `FILLED` or a terminal failure. That's a single source of
truth this project has already had reason to distrust in this exact spot:
`WebullBrokerClient`'s module docstring flags `_order_from_detail`'s
field-name mapping for a *populated* response as UNVERIFIED (every live
attempt during integration was rejected for being outside market hours,
so a real filled order detail was never actually fetched), and
`get_positions()` -- a structurally different endpoint -- already had one
real incident where a field-name mismatch silently lost a fill.

`_poll_pending_entry` now falls through to a second, independent check --
`_maybe_verify_entry_via_positions` -- whenever `get_order_status` reports
anything other than a terminal status (`FILLED`/`REJECTED`/`CANCELED`/
`EXPIRED`), **or raises at all**. Once at least
`TradingLoopConfig.entry_position_verify_delay_seconds` (10s default) have
passed since the entry order was submitted (`Order.created_at`), it calls
`broker.get_positions()` directly and, if Webull already shows an open
position for this symbol, treats the entry as filled right there --
building an `Order(status=FILLED, quantity=<the broker's own quantity>, ...)`
and routing it through the same `_confirm_entry_filled` every other fill
path already uses, rather than waiting for `get_order_status` to
eventually agree (which, if that mapping is ever wrong again, might be
never). Runs at most once per pending entry
(`self._pending_entry_position_checked`, reset whenever a pending entry
resolves either way or a fresh one is submitted) rather than every tick
past the 10s mark, to avoid an extra `get_positions()` call every
`poll_interval_seconds` stacked on top of the `get_order_status` call
already happening. If the broker genuinely has no matching position yet,
this is a no-op and normal `get_order_status` polling continues
uninterrupted.

## Performance/rate-limit rehaul (2026-08-11)

A single Webull sandbox account shares one real, measured ~1 request/second
sustained rate ceiling (`brokers/webull/retry.py`'s docstring) across
**every** endpoint this bot calls -- market data, orders, positions,
account balance, all of it. Before this pass, only `market_data.*` calls
(`get_snapshot`/`get_bars`) were paced or retried at all; every
`order_v3.*`/`account_v2.*` call (`place_order`, `cancel_order`,
`get_order_status`, `get_positions`, `get_account_balance`, ...) had zero
rate-limit protection despite drawing from that exact same budget -- a
burst of market-data polling could 429 a stop-loss cancel with nothing to
retry it. Closing that gap, and making sure exit-critical traffic always
wins contention over lower-stakes traffic instead of queuing behind it in
plain arrival order, was the point of this whole pass.

**Priority tiers** (`retry.py`'s `CallPriority`, `RateLimiter`): every
Webull call this codebase makes now goes through one shared,
priority-aware `retry.webull_limiter`. When multiple threads are
simultaneously waiting for the next available ~1/s slot, the
highest-priority one is released first (ties broken by arrival order) --
implemented as a lock/condition-variable-guarded min-heap of waiting
tickets, not a second parallel limiter (that would just double the
effective rate and reintroduce 429s). Three tiers, lower number wins:

1. **`CRITICAL`** -- exit/stop-loss management: pending-exit polling,
   broker-bracket fill polling (`_poll_broker_bracket`), cancelling/
   replacing a resting protective order, entry-fill confirmation once a
   fill is known, `place_order`/`cancel_order`/`place_oco_bracket`
   themselves (always a real trading action), the per-tick batched
   `get_snapshots` call that feeds every `MANAGING` position's stop/
   target/VWAP checks, and `get_positions()` (shared fixed priority --
   see its docstring for why erring toward CRITICAL for every caller,
   entry-sizing included, was judged safe: it's cheap and infrequent
   relative to market-data polling).
2. **`NORMAL`** -- pending-entry polling, the 10s entry-verification
   check, periodic reconciliation, account equity/buying-power reads,
   `poll_fills`, `modify_order`. The default when nothing more specific
   applies.
3. **`BACKGROUND`** -- universe discovery/rescan (`BroadScanner.scan`'s own
   `get_snapshots` call) and periodic resistance-level refresh
   (`get_raw_bars`/`get_daily_volumes`). Free to wait behind everything
   above -- this is exactly the traffic that used to be able to starve out
   money-at-risk calls under load.

Strict `BrokerClient` ABC methods (`get_snapshot`, `get_bars`,
`get_positions`, `place_order`, `cancel_order`, `get_order_status`,
`poll_fills`) use a single fixed priority baked into `WebullBrokerClient`
rather than a caller-supplied one, deliberately: threading a `priority`
kwarg through every implementer of those methods (`PaperBrokerClient` and
every test double across the suite) for cases that don't actually need
call-site granularity wasn't worth the blast radius. The two methods where
the distinction *does* matter in practice --
`get_snapshots`/`get_raw_bars`/`get_daily_volumes` -- are already
optional/`getattr`-gated (only `WebullBrokerClient` implements them), so
adding a `priority` parameter there was safe and cheap; `TradingLoop`
passes `CRITICAL`, `BroadScanner` passes `BACKGROUND`, explicitly, at each
call site.

**Stop-sync hysteresis** (`TradingLoopConfig.stop_sync_min_move_pct`,
0.25% default): `_sync_broker_protective_orders` used to cancel+replace
the resting stop on *any* change to `position.stop_price`, but
`PositionManager`'s trailing-stop math (`current_price * (1 -
trailing_pct)`) recomputes to a different float almost every tick once
active -- without a minimum-move threshold, a fast-moving symbol would
cancel+replace on nearly every single tick for changes too small to
matter, burning `CRITICAL`-tier rate-limiter slots for no real protective
benefit. 0.25% is deliberately small relative to the 3% default
`trailing_stop_pct`: hysteresis against float noise, not a meaningful
loosening of how tightly the stop actually trails price.

**Batched broker-bracket status polling** (`WebullBrokerClient.list_open_orders`,
`TradingLoop._get_open_orders_for_tick`): `_poll_broker_bracket` used to
call `get_order_status` once per resting leg (up to 2) per broker-managed
position, every tick. `list_open_orders` fetches every currently-resting
order for the whole account in one call, via the confirmed
`order_v3.get_order_open` SDK method (see this doc's "Broker-side (resting)
stop/target management" section for the earlier `get_open_orders` vs.
`get_order_open` naming mixup this confirms was resolved correctly), and
`_poll_broker_bracket` checks that batch first: a leg still listed there is
confirmed still resting with **zero** individual calls needed (the common
case, every tick a resting order hasn't fired). Only a leg that's
disappeared from the batch falls back to one targeted `get_order_status`
call, to learn whether it specifically filled or was cancelled (the batch
only says "no longer open," not why). Confirmed live that `get_order_open`'s
row shape uses `total_quantity`, not the plain `quantity` key
`get_order_detail`/`place_order` use elsewhere -- `_order_from_open_order_dict`
handles that separately from `_order_from_detail`, with a defensive
fallback to `quantity` in case a response variant ever differs. Falls back
entirely to the original per-leg polling for a broker without this
capability (`PaperBrokerClient`/backtests, or a `list_open_orders` call
that failed this tick).

**Per-tick `get_positions()` dedup** (`TradingLoop._get_positions_for_tick`,
`self._tick_positions_cache`): several candidates independently needing
`get_positions()` within the same `_process_all_candidates` pass (e.g.
multiple `TRIGGERED` entries crossing their own verify-delay threshold
together, or that lining up with the independently-throttled periodic
reconcile) used to each fire their own network call for the exact same
account-wide data. Now shared: at most one real call per pass, reset at
the start of the next one. Deliberately **not** used by
`_confirm_entry_filled`'s own post-fill lookup, which always calls
`broker.get_positions()` directly -- that call specifically wants to see a
fill that may have only just been confirmed *this* tick, which a value
cached earlier in the same pass (before the fill was known) could miss.
The same per-pass-cache pattern backs `_get_open_orders_for_tick` above.

**Discovery snapshot batching** (`BroadScanner.scan`'s `get_snapshots`
call) already existed before this pass -- confirmed while investigating
this rehaul, not newly added -- collapsing what would otherwise be one
`get_snapshot` call per newly-discovered symbol into `ceil(N/100)` batched
calls for the whole new-symbol set. What this pass added was tagging it
`BACKGROUND` priority so it can never contend with the exit-critical
per-tick batch for the same rate-limiter slots.

**Still open**: `get_raw_bars`/`get_daily_volumes` (resistance levels,
volume profile, opening range, average volume) remain one call per *newly
discovered* symbol -- Webull's history-bar API is inherently per-symbol
with no multi-symbol batch equivalent (unlike snapshot), so there's no
equivalent batching lever available there; this cost is paid once per
symbol ever (not on every rescan -- see `TradingLoopConfig`'s docstring),
which is judged acceptable rather than a further optimization target for
now.

### Streaming market data -- confirmed live and wired into production (2026-08-11)

`subscribe_quotes` has been an unimplemented stub since `WebullBrokerClient`
was first built, blocked on "no sandbox mqtt_host was ever confirmed."
That blocker is now resolved, in three stages -- static SDK inspection,
then two live tests that each ruled out a wrong hypothesis before the
third one confirmed the fix:

1. **Static inspection**: `webull.data.data_streaming_client.DataStreamingClient`
   is a real, implemented MQTT client (via `paho-mqtt`). Leaving its
   `mqtt_host` constructor arg as `None` lets the SDK auto-resolve it via
   a config file bundled *inside* the SDK package
   (`webull/core/data/endpoints.json`), which has exactly **one**
   `quotes-api` entry for region `"us"` -- `data-api.webull.com`, the
   known production host, with no sandbox equivalent in that file at all
   (unlike the plain REST `api` entry, which *does* have a distinct
   `api.sandbox.webull.com`). `session_id` needs no special handshake --
   a plain caller-generated id (e.g. a UUID) passed to both the MQTT
   client and the subscribe REST call. `on_connect_success` must be set
   *before* connecting, and `client.subscribe(...)` belongs *inside* that
   callback per the class's own design (the MQTT connect happens on a
   background thread via non-blocking `connect_and_loop_start()`).
2. **First live test** (`scripts/verify_streaming.py`, using the
   auto-resolved `data-api.webull.com`): MQTT connected successfully, but
   the subscribe REST call was rejected outright with `417
   INVALID_SESSION` ("Mqtt connection not exist for session"). Looked at
   first like a timing race (REST-side session registry not yet caught up
   to the MQTT handshake) -- a second run with a 2s delay inserted before
   subscribing got the byte-for-byte identical error, **ruling that theory
   out**.
3. **Second live test**, explicitly passing `--mqtt-host
   data-api.sandbox.webull.com` (the natural next hypothesis: a
   sandbox-specific quotes host mirroring the already-confirmed
   `api.webull.com` -> `api.sandbox.webull.com` REST split, which the
   SDK's bundled config simply doesn't know about) -- **confirmed
   correct**: MQTT connected, the subscribe REST call was accepted (200),
   and real quote ticks arrived within seconds:
   ```
   topic='quote' payload=basic: symbol:AAPL,instrument_id:913256135,
   timestamp:1786474351120,trading_session:RTH,
   asks:[price:304.22,size:203],bids:[price:304.21,size:41]
   ```
   Confirmed live, during real core trading hours, against the real
   sandbox account this project runs against.

**Still open**: an MQTT CONNACK return code 1 ("Protocol not supported")
appeared during the SDK's own automatic reconnect attempt, ~4-10s after
each test run's own shutdown sequence (`loop_stop()`/`disconnect()`)
started -- present on all three live runs regardless of which mqtt_host
was used or whether the run succeeded. Most likely a reconnect-during-
shutdown artifact specific to this short-lived verification script (which
disconnects the instant it's satisfied, rather than running long-lived),
not evidence of instability in the streaming service itself -- but not
yet understood well enough to rule that out completely. Worth watching
for whether it recurs once a long-running integration is built and left
connected for real, rather than assumed away.

`WebullBrokerClient.subscribe_quotes` is implemented for real: it lazily
creates one persistent `DataStreamingClient` per broker instance (reused
across calls, not one per symbol), picks the confirmed
`data-api.sandbox.webull.com` host when `TradingMode.SANDBOX`
(`mqtt_host=None`, letting the SDK auto-resolve to production, otherwise),
and subscribes each new symbol to **both** streaming message types --
`SNAPSHOT` (price, open/high/low/volume, pre_close, plus `ext_*`/`ovn_*`
extended-hours variants) and `QUOTE` (top-of-book bid/ask, confirmed live
back in the very first verification run, before `SNAPSHOT` was
discovered). Each message type is mapped separately
(`_snapshot_from_streamed_result` / `_quote_top_of_book`) and cached per
symbol; `_merge_streamed_snapshot` combines the latest of each into one
complete `MarketSnapshot` before it's ever handed to `on_update`. A
symbol's very first message (of either type) is cached but does **not**
trigger `on_update` -- only once both a real `SNAPSHOT` and a real
`QUOTE` have been seen for it, so a caller never receives a snapshot with
a fabricated `bid=0.0`/`ask=0.0`. This matters concretely:
`metrics.calculations.bid_ask_spread` treats a non-positive bid or ask as
"spread is 0.0" (its documented behavior for missing data), which would
otherwise make `CandidateWatcher.update`'s spread-eligibility gate read a
symbol whose quote side simply hasn't arrived yet as "spread is fine" --
a fail-open this project isn't willing to accept for entry-risk logic.
Every message callback is wrapped in its own try/except so one malformed
message or a raising `on_update` can never kill the MQTT thread.

**Where it's wired into `TradingLoop`** (extended 2026-08-11 from
exit-management-only to also cover pre-entry monitoring, now that the
merge above supplies real bid/ask): streaming replaces polling for every
state in `TradingLoop._STREAMING_ELIGIBLE_STATES` --
`WATCHING`/`HEATING_UP`/`ARMED` (pre-entry momentum scoring and spread
gating) as well as `ENTERED`/`MANAGING` (exit-management price checks).
`DISCOVERED` (a candidate leaves it on its very first tick, before
there's ever anything to subscribe) and `TRIGGERED`
(`_poll_pending_entry` manages a pending order, not a live price) are
excluded on purpose. `_process_candidate_inner` prefers
`_get_streaming_snapshot` for any eligible state, falling back to the
prefetched/REST snapshot only when nothing fresh has streamed in the
last `TradingLoopConfig.streaming_staleness_seconds` (10s default).

Two different subscription triggers feed the same underlying mechanism:
a position's symbol is subscribed exactly once, right after its
broker-side bracket is attached (`_confirm_entry_filled`'s fresh-entry
path, or `reconcile_positions_from_broker`'s adoption-on-restart path);
a watch-stage candidate's symbol is (re-)subscribed once per tick from
`_process_all_candidates`, for every currently `WATCHING`/`HEATING_UP`/
`ARMED` candidate. `_ensure_streaming_subscribed` tracks already-
requested symbols for the life of the process, so the per-tick call is
cheap in steady state (a membership check, not a real subscribe) and a
later call for an already-subscribed symbol is always a no-op. If
`subscribe_quotes` ever raises `NotImplementedError` (i.e. running
against `PaperBrokerClient`, which deliberately doesn't implement
streaming) it permanently flips `_streaming_supported = False` so every
subsequent tick skips straight to REST polling instead of retrying a
call that can never succeed. Any other exception (a real connection
failure) is logged and swallowed without disabling streaming
permanently, since that failure mode is worth retrying.
`_process_all_candidates`'s batched `get_snapshots` call for the tick
also excludes any streaming-eligible-state symbol that already has a
fresh streamed snapshot, so a covered symbol never pays for both a
stream *and* a REST call on the same tick.

**Known open tradeoff**: there is deliberately no unsubscribe path for a
symbol that leaves every streaming-eligible state (a closed position, a
candidate that gets `REJECTED`) -- subscriptions only ever grow for the
life of the process. That was a reasonable simplification when only open
positions (a handful at a time) were covered; now that every
`WATCHING`/`HEATING_UP`/`ARMED` candidate BroadScanner has ever surfaced
is included too, the subscribed-symbol count could grow meaningfully
larger over a full trading day. Whether Webull's per-session subscription
count or rate has a practical ceiling this could approach is **not yet
confirmed either way** -- worth watching in production (and worth a
live-verified unsubscribe path as a follow-up if it turns out to matter),
consistent with this project's rule of not building for an unconfirmed
constraint.

**Real bug found and fixed during first production deploy (2026-08-11)**:
the first VPS deploy of the WATCHING-stage extension adopted three
existing positions in one `reconcile_positions_from_broker` pass, each
calling `subscribe_quotes` back-to-back within the same tick -- and zero
streaming activity ever appeared in the logs afterward (no successful
connect, no failure either). Root cause, confirmed directly against the
installed SDK (not guessed): `DataStreamingClient.get_connect_success()`,
which `subscribe_quotes` used to decide "connection already up, send the
REST subscribe call directly" vs. "still connecting, let the connect
callback handle it," is misleadingly named -- its backing flag is set
`True` by the `on_connect_success` property's own setter the instant a
callback is *assigned*, not once the MQTT handshake actually completes:

```python
>>> c = DataStreamingClient(...)
>>> c.get_connect_success()
None
>>> c.on_connect_success = lambda *a: None  # no network call made at all
>>> c.get_connect_success()
True
```

So every `subscribe_quotes` call after the very first one (which sets
that callback while constructing the client) saw the flag already
"true" and tried to register a subscription over REST before the MQTT
session existed server-side yet -- the same `417 INVALID_SESSION`
failure this feature's original verification had already diagnosed once
against the wrong host, now reintroduced by a completely different path
using the right host. Fixed by tracking the real connection state
ourselves: a new `WebullBrokerClient._streaming_connected` flag, set
`True` only from inside this module's own `_on_connect_success` wrapper
(which the SDK does only call once genuinely connected), and consulted
instead of `get_connect_success()` everywhere `subscribe_quotes` needs
to know whether it's safe to subscribe directly. Covered by a regression
test (`test_subscribe_quotes_does_not_trust_the_sdks_get_connect_success`)
whose fake mirrors the SDK's real (mis-)behavior rather than a
nicer-behaved approximation of it, specifically so this can't silently
regress.

### Streaming: retrying a failed connection or subscribe (2026-08-11)

Confirming the fix above worked in production raised the natural
follow-up: what happens if a connection attempt or a subscribe call
fails for a real reason (not the bug above) -- a transient network blip,
a brief Webull-side hiccup? Two distinct failure points, two distinct
fixes:

1. **The MQTT connection itself never completes** (`_on_connect_success`
   never fires, so `_streaming_connected` stays `False` forever). A new
   `_STREAMING_RECONNECT_DELAY_SECONDS` (15s) timeout, tracked via
   `WebullBrokerClient._streaming_connect_attempted_at`: if a later
   `subscribe_quotes` call finds the existing client still not connected
   and more than that long has passed since the connection attempt
   started, it tears down the stale client (`loop_stop()`/`disconnect()`,
   best-effort) and opens a fresh one instead of waiting on a callback
   that may never come. Every symbol ever requested
   (`self._streaming_subscribed_symbols`, not just whatever triggered
   this particular call) gets resubscribed once the fresh connection
   comes up -- correct because nothing was ever confirmed subscribed
   against the dead connection in the first place. Not itself tuned
   against a real stuck connection (every live connect observed so far
   has completed in well under 15s) -- just a conservative "clearly
   longer than a healthy connect should ever take" threshold.
2. **A specific subscribe REST call fails while already connected.**
   `subscribe_quotes`'s "already connected" branch used to catch and log
   this internally, which meant it returned normally either way --
   `TradingLoop._ensure_streaming_subscribed` had no way to tell the
   difference between success and failure, and would mark the symbols as
   subscribed (`self._streaming_requested_symbols`) regardless, so a
   failed symbol would just silently never stream for the rest of the
   process. Now the exception is left to propagate all the way up to
   `_ensure_streaming_subscribed`'s own try/except, which does NOT mark
   these symbols as requested on failure -- so they're picked up again
   the next time `_ensure_streaming_subscribed` runs for them. That
   "next time" used to only exist for watch-stage candidates (subscribed
   fresh every tick in `_process_all_candidates`) -- `ENTERED`/`MANAGING`
   positions only ever got one eager attempt, at
   `_confirm_entry_filled`/`reconcile_positions_from_broker`'s adoption
   time, with no way to retry a failure. `_process_all_candidates`'s
   per-tick subscribe sweep now covers every `_STREAMING_ELIGIBLE_STATES`
   symbol, not just watch-stage ones, so a failed position-adoption
   subscribe also self-heals on the very next tick -- a "short wait" in
   wall-clock terms (`poll_interval_seconds`), not a separate retry timer.

## Structural vs. temporary disqualification

Two conceptually different kinds of "this candidate isn't tradeable right
now" exist, and it matters which one a given check uses:

- **Structural / permanent** -- `CandidateState.REJECTED`. Reserved for
  conditions that can't meaningfully change for this candidate: free float
  above the ceiling (`BroadScanner`), or (structurally) an unsupported
  security type. Terminal -- `state_machine.py`'s `_ALLOWED_TRANSITIONS`
  has nothing leaving `REJECTED`, and `CandidateWatcher` never revisits a
  rejected candidate (`update()` returns immediately for one).
- **Temporary / tradeability-only** -- `Candidate.trade_eligible` +
  `Candidate.block_reasons` (a list of `enums.TradeBlockReason`).
  `CandidateWatcher.update()` recomputes both from scratch on *every*
  tick, so a resolved condition clears itself automatically -- nothing
  ever needs to explicitly "un-block" a candidate. Currently drives two
  reasons: `SPREAD_TOO_WIDE` (`spread_pct > WatcherConfig.max_spread_pct`)
  and `LOW_LIQUIDITY` (`dollar_volume < WatcherConfig.min_dollar_volume`).

**These used to both funnel into `REJECTED`.** That was wrong for this
bot's actual target pattern: a low-float name can go from an unwatchable
8% spread to a tight, tradeable one within seconds once real volume shows
up, and a permanent rejection meant the bot could never reconsider a name
it gave up on from one bad tick. The fix keeps `state`
(WATCHING/HEATING_UP/ARMED/...) driven **purely** by the Momentum
Ignition Score, completely independent of `trade_eligible` -- a candidate
can be ARMED (score-qualified) while still not `trade_eligible` (spread
temporarily too wide right now). Nothing currently reads `trade_eligible`
before generating an entry `Signal` (`TriggerEngine`/`Strategy` aren't
touched by this), but this doesn't reopen a safety gap:
`RiskEngine.evaluate()` independently re-checks spread (`max_spread_pct`)
and dollar volume (`min_dollar_volume`) against a fresh snapshot at the
actual order-submission gate, with its own separate config -- see
`risk/risk_engine.py`. `trade_eligible` is therefore visibility/diagnostics
plus a future cheap short-circuit, not the only thing standing between a
wide-spread name and an order.

The same "temporary, not permanent" philosophy still applies to dollar
volume at `BroadScanner`'s discovery-time checks: it's a `Candidate` field
for scoring context, not a pass/fail gate, since a stock waking up from
quiet is the target, not a disqualifier. The volume floor (average-daily/
previous-day/current-day volume, see "Volume floor" above) is the one
exception to that philosophy in `BroadScanner` -- it IS a structural, permanent gate
again (a symbol that misses it is never even discovered, not marked
temporarily untradeable), per explicit user request. Its either-or design
is what keeps it from re-introducing the exact problem the original
all-or-nothing version had: a name only needs ONE of the three figures
(average-daily, previous-day, or current-day-so-far volume) to clear its
bar to survive, so a previously-quiet float seeing a single real volume
day -- or trading heavily *today* even with a quiet history -- isn't
excluded just because the other two haven't caught up yet.

## Momentum Ignition Score

`scoring/weights.yaml` holds component weights and normalization
thresholds, versioned via the `version` field. `scoring/momentum_ignition_score.py`
normalizes weights to sum to 1.0 at load time and produces a 0-100 score
plus its component breakdown (`MomentumScoreComponents`) so individual
factors can be analyzed later. Nothing about the formula is assumed
correct -- it exists to be replaced once backtest/paper data says otherwise.

`metrics/rolling.py`'s `compute_metrics` tracks more raw windowed data
than the score formula consumes -- `volume_1m/5m/15m` and
`dollar_volume_1m/5m/15m` remain unused -- kept modular (pure functions in
`metrics/calculations.py`, plain fields on `MomentumMetrics`) so future
formula work doesn't require touching this module again.
`dollar_volume_1m/5m/15m` use `dollar_volume_from_avg_price`, which
averages each window's own boundary prices rather than one current price
across all windows -- this is what makes `dollar_volume_accel_1m_3m` a
genuinely distinct signal from `volume_accel_1m_3m` (a rescaled duplicate
would result from using a single price, since it would cancel out of the
ratio). `relative_volume_1m/5m` mirror the existing whole-session
`relative_volume`'s already-established pattern: they accept an optional
per-symbol baseline (`typical_volume_1m/5m`) and fall back to a neutral
1.0 when none is supplied, since no per-symbol intraday volume-distribution
baseline exists yet to compare against -- same honest gap as
`relative_volume`'s own `typical_volume_same_time`, not a new one.

**v2 (2026-08-09): three previously-unused metrics wired into the score,
plus a reweight towards current activity/"popularity."** `float_turnover`
(today's cumulative float turnover), `relative_volume_5m` (windowed RVOL),
and `dollar_volume_accel_1m_3m` were already computed above but never
consumed by `compute_components` -- they now feed
`float_turnover_score`/`short_term_relative_volume_score`/
`dollar_volume_acceleration_score` respectively (see that function). At the
same time, `weights.yaml`'s weights were rebalanced so components measuring
real-time trading activity right now -- RVOL (both whole-session and
windowed), float turnover (both cumulative and the 5m rate), and volume/
dollar-volume acceleration -- carry more relative weight than the more
static/structural components (float size, breakout proximity, VWAP trend
quality). Since `dashboard/app.py`'s `/api/candidates` sorts by score
descending, this reweighting directly controls what surfaces at the top of
the live candidates list: a name already seeing heavy real volume now
outranks one that merely looks structurally attractive (tight spread,
near a breakout level) but isn't actually trading heavily yet. See
`weights.yaml`'s own v2 comment for the exact before/after weights --
still first-pass, unvalidated numbers, same as v1.

## Data collection

`collection/event_recorder.py`'s `MomentumEventTracker` records a
`MomentumEvent` for any notable momentum occurrence -- traded or not -- and
fills in forward-looking outcome snapshots at 30s/1m/3m/5m/10m/15m as
subsequent snapshots arrive, plus MFE/MAE and HOD/VWAP-break flags. This is
the raw material for improving the MIS formula and strategies offline; it
persists via `EventRecorder`, which is a thin seam meant to be backed by
`db.models.MomentumEventRecord` in a real deployment.

## Dashboard

`dashboard/app.py`'s `create_app(trading_loop, session_factory, trading_mode)`
takes both as explicit arguments rather than importing globals, specifically
so tests (`tests/test_dashboard.py`) can pass a real `TradingLoop` wired to
`PaperBrokerClient` plus an in-memory SQLite session factory, with no live
bot or real database required.

Two different data paths, deliberately:
- **Live panels** (candidates, open positions, risk events, kill-switch
  state) read directly off `trading_loop.get_candidates()` /
  `get_open_positions()` / `risk_engine.events` -- no DB round-trip, so they
  reflect the current process's actual in-memory state, not a snapshot that
  might be stale or never got persisted. `get_candidates()`/`get_open_positions()`
  return shallow copies specifically so a dashboard request from another
  thread can't race a concurrent rescan or candidate-processing pass
  mutating the underlying dicts -- `get_candidates()` takes
  `_candidates_lock` for this (see "Concurrency: rescanning runs on its own
  background thread" above); `get_open_positions()` doesn't need a lock
  since `_positions` is only ever touched from the single candidate-
  processing thread, never from the background rescan thread.
- **Historical panels** (trade history, performance/win-rate) read from the
  database via `db/repository.py`.

`scripts/run_dashboard.py` is what makes the historical panels have
anything to show: it wires `TradingLoop`'s `on_trade_closed`/`on_order_update`
callbacks to `record_trade()`/`record_order()`, runs the loop in a
background thread, and serves the FastAPI app in the foreground. Before
this, nothing in the live loop persisted anywhere -- `on_trade_closed` just
printed. `record_order()` upserts by `client_order_id` rather than
inserting a new row per call, since one order is reported multiple times as
its status changes (`SUBMITTED` -> `FILLED`, see the Webull integration
section above) and `OrderRecord.client_order_id` has a uniqueness
constraint.

The frontend (`dashboard/static/`) is plain HTML/CSS/JS with no build step
and no bundled/npm dependencies -- it polls the REST endpoints every 5s.
Keep it that way unless there's a real reason to add a frontend toolchain;
a monitoring dashboard for a single operator doesn't need one. The one
exception, as of the **Chart panel** below, is a single CDN script loaded
on demand from TradingView -- everything else on the page still has zero
external dependencies.

**Chart panel** (above Candidates): embeds TradingView's Advanced
Real-Time Chart widget (the standard "embed-widget-advanced-chart.js"
snippet from TradingView's own widget generator) for whichever candidate
row is currently selected in the Candidates table below, reusing the same
`selectedCandidateSymbol` state `refreshScoreBreakdown`'s live-per-
candidate view already tracks -- selecting a candidate updates both at
once. Collapsed by default behind a "Show Chart" toggle button
(`initChartPanel`/`updateChartPanel` in `app.js`): this is a real,
ongoing connection to TradingView's own servers (not a static image), so
it isn't loaded unless explicitly opened, and collapsing it again fully
tears the widget's DOM down (`teardownTradingViewChart`) rather than just
CSS-hiding it, so it stops costing bandwidth/a live connection the moment
it's closed. Because TradingView's embed snippet only initializes when its
`<script>` tag is actually present in the DOM at insertion time (a
`<script>` written into `innerHTML` never executes), changing symbols
means destroying and rebuilding the whole widget container from scratch
via `renderTradingViewChart(symbol)` -- there's no simpler "just update the
symbol" API available through this embed method, only through TradingView's
heavier Charting Library integration, which this project deliberately
doesn't need. Guarded to only re-render on an actual symbol change
(`chartRenderedSymbol !== selectedCandidateSymbol`), so the independent 5s
poll cycle (`refreshCandidates`) never forces a reload of a chart nobody
touched. The symbol is passed as a bare ticker (e.g. `AAPL`, no exchange
prefix) since `Candidate` has no exchange field to draw one from --
TradingView resolves a bare symbol to its primary listing on its own,
which works for ordinary NASDAQ/NYSE/AMEX names but is unconfirmed for an
OTC-only or otherwise ambiguous ticker. Loading the widget necessarily
reveals to TradingView which ticker is being viewed (an ordinary
consequence of embedding any third-party widget, not a project-specific
privacy hole) -- worth knowing since this is a trading tool, even though
it's opt-in per open of the panel.

**Settings (top-right gear button)**: writes, not just reads (see the
Safety section's kill-switch button for the dashboard's other write path).
Two separate config objects, two separate endpoint pairs, shown together
in one modal:
- `GET`/`POST /api/risk-settings` expose six `RiskConfig` fields (see
  "Risk sizing" above), mutating `trading_loop.risk_engine.config` directly.
- `GET`/`POST /api/position-settings` expose two `PositionManagementConfig`
  fields (`breakeven_trigger_pct`, `trailing_stop_pct` -- see "Position
  management" above), mutating `trading_loop.position_manager.config`.

Both take effect on the very next evaluation (a `Signal` for risk settings,
a `check_exit()` call for position settings) -- no restart, and nothing
persisted to disk or the database, so a restart reverts to each config
dataclass's hardcoded defaults. Both are deliberately small, curated
subsets of their respective config objects (`_ADJUSTABLE_RISK_FIELDS` /
`_ADJUSTABLE_POSITION_FIELDS` in `dashboard/app.py`), not every field --
matched to what's actually useful to tune without restarting versus a
rarer, more structural decision (e.g. `max_trades_per_ticker_per_day`)
better made by editing the config in code. One of the six risk fields,
`max_simultaneous_positions`, is a whole-number position count rather than
a percentage/ratio like the other five -- its validation is special-cased
in `update_risk_settings` accordingly: 0 is accepted and means unlimited
(the RiskEngine.evaluate() cap simply doesn't apply), not rejected as
"must be greater than 0" like it would be for any of the percentage
fields, and there's no 100 ceiling on it either. One current limitation: both
`PositionManagementConfig` fields are natively `Optional[float]` (`None`
disables the rule), but the update endpoint also uses `None` to mean
"omitted from this request" -- so the dashboard can set either to a
positive value but can't use it to explicitly disable one; that still
requires a code-level config change.

## Full persistence: state transitions, MIS scores, momentum events

Beyond trades/orders, `TradingLoop` exposes three more hooks so
`scripts/run_dashboard.py` can persist everything the data-collection goals
in the project outline need, without `TradingLoop` itself importing the DB
layer:

- **`on_state_transition(symbol, from_state, to_state, timestamp)`** --
  fired for every state-machine transition, from wherever it happened
  (`CandidateWatcher`, `TriggerEngine`, or `TradingLoop` itself). Rather
  than threading a callback through every module that can call
  `state_machine.transition()`, `_flush_state_transitions()` diffs
  `Candidate.state_history` (an append-only list every `transition()` call
  already appends to) against a per-symbol "already reported" count kept in
  `TradingLoop`, and reports whatever's new. This runs inside a `finally`
  block wrapping `_process_candidate` specifically so it still fires no
  matter which early `return` branch was taken that tick.
- **`on_score_computed(symbol, score)`** -- fired with `candidate.latest_score`
  immediately after `CandidateWatcher.update()` produces one. Writes one row
  per tick per watched candidate on purpose (see the README's note on this)
  -- comparing MIS formulas offline needs a dense history, not samples only
  at trigger time.
- **`momentum_event_tracker`** -- unlike the other two, this is a
  collaborator object (`MomentumEventTracker`, `collection/event_recorder.py`),
  not a simple callback, because tracking a momentum event has ongoing state
  across many ticks (filling forward-looking 30s-15m outcome windows).
  `TradingLoop` registers a new `MomentumEvent` whenever `TriggerEngine`
  fires a signal (`_register_momentum_event`), flips `was_traded` to `True`
  in `_submit_entry` once the order actually gets submitted (not just
  attempted -- a risk-engine rejection leaves it `False`), and calls
  `momentum_event_tracker.on_snapshot()` unconditionally every tick a
  snapshot is available so outcome windows keep filling regardless of the
  candidate's state. `db/repository.py`'s `DBBackedEventRecorder` extends
  the base in-memory `EventRecorder` to also write through to the database
  on every `save()`/`update()`, keyed by the DB row id (momentum events have
  no natural unique key the way orders have `client_order_id`).

**Bug found and fixed while wiring this up:** `MomentumEventTracker._finalize()`
(which sets the final `CONTINUED`/`FAILED`/`CHOPPY` label once all outcome
windows are filled) used to run *after* the last `recorder.update()` call
for that event, so the finalized label was computed but never actually
included in what got persisted -- silent before because the original
`EventRecorder.update()` was a no-op with nothing behind it to expose the
bug. Fixed by finalizing before that last `update()` call; see
`tests/test_event_recorder.py::test_final_update_call_includes_the_finalized_outcome_label`,
which fails without the fix.

## Free-float data (FMP + yfinance fallback)

`data/float_providers/fmp.py` is a real, network-verified integration
against Financial Modeling Prep's `stable` API namespace (`/stable/shares-float`
and `/stable/profile`) -- confirmed live on 2026-08-08, including that FMP's
older `/api/v4/shares_float` now hard-fails with a "Legacy Endpoint" error.
`get_float_provider(settings)` (`data/float_providers/__init__.py`) is the
only place that should construct a float provider; it picks FMP when
`FMP_API_KEY` is set and wraps it in `CachedFloatProvider`. `MassiveFloatProvider`
remains as an alternate skeleton, unused unless FMP is unconfigured.

`FMPFloatProvider` takes an injectable `http_get` callable specifically so
`tests/test_fmp_float_provider.py` can run hermetically against canned
responses shaped like the real API, without a network call or API key.

**yfinance fallback (2026-08-09):** FMP's free tier has already hit real
429s during this project's own live testing (see the average-volume
section above), so `get_float_provider` also wraps whichever primary it
picked in `FallbackFloatProvider` (`data/float_providers/fallback.py`)
with `YFinanceFloatProvider` (`data/float_providers/yfinance_provider.py`)
as the sole fallback, controlled by `Settings.enable_yfinance_fallback`
(default on). `FallbackFloatProvider` tries `primary` first for every
symbol and only falls through to `fallbacks` (tried in order) once the
primary has already raised for that symbol -- `get_float_data_bulk` does
the equivalent per-symbol reconciliation, calling each fallback only with
whatever the previous provider(s) left missing. This matters because
`BroadScanner._check_symbol` treats any `get_float_data` exception as a
hard structural rejection (`except Exception: return None`) -- without a
fallback, a single rate-limited FMP call silently drops a symbol from
discovery entirely for that cycle.

`YFinanceFloatProvider` is deliberately never used as a standalone/primary
provider -- it goes through the unofficial, scraped `yfinance` package
(Yahoo publishes no supported API for this), and Yahoo is known to
throttle traffic from datacenter/cloud IP ranges, which is exactly what a
VPS deployment looks like to them. Wiring it in strictly as a fallback
means a Yahoo block just means "no fallback available today," not a new
failure mode layered on top of a working FMP integration. Its
`get_info()`-derived `floatShares` field is why it's worth using over
other free tiers (Finnhub, Polygon, Alpha Vantage all give shares
outstanding, not actual free float) -- see that module's docstring. Like
`FMPFloatProvider`, it takes an injectable `info_fetcher` callable so
`tests/test_yfinance_float_provider.py` runs hermetically with no real
Yahoo call and no dependency on the `yfinance` package actually being
importable in the test environment.

## Webull integration

`brokers/webull/client.py` wraps the official `webull-openapi-python-sdk`.
Read its module docstring before touching it -- it lists exactly which
field mappings were confirmed against live sandbox responses (auth,
account balance, market snapshot/bars, order request schema) versus which
are best-effort guesses pending re-verification (populated position rows,
a successful order response body, fill executions), and why: the sandbox
account had zero positions and every live order test happened on a weekend
market close, so those specific shapes couldn't be observed.

Four non-obvious things worth knowing if you're debugging this client:

1. **App key/secret are environment-locked.** A key issued for production
   authenticates fine against `api.webull.com` but gets a generic
   `401 UNAUTHORIZED: ... ensure you are connecting to the correct
   environment` from the sandbox host, and vice versa -- confirmed by
   testing the exact same credentials against both hosts. If sandbox auth
   ever breaks, test against production first (read-only calls like
   `get_account_list` are enough) before assuming the integration code
   regressed.
2. **`instrument_type` for orders is the string `"EQUITY"`**, not
   `webull.trade.common.instrument_type.InstrumentType.STOCK.name` (which
   is `"STOCK"` and gets rejected with `INVALID_PARAMETER: Instrument type
   invalid.`) -- that Python enum apparently isn't what the order-placement
   endpoint expects, despite looking like the obvious match. Confirmed by
   testing both live.
3. **`get_snapshot`'s extended-hours price/volume are opt-in, unverified,
   and time-gated.** Passing `extend_hour_required=True` (now always set --
   see `WebullBrokerClient.get_snapshot`) is required for Webull to include
   pre-market/after-hours data at all; without it, `last_price` silently
   stays pinned to the regular-session price for hours after the session
   ends, which is exactly the window the `PRE_MARKET`/`AFTER_MARKET`
   discovery sources above are trying to catch movers in. The fields this
   code reads (`ext_price`/`ext_volume` in `_snapshot_from_dict`) are
   *inferred* from the SDK's protobuf streaming schema's naming convention
   (`ext_price`/`ext_high`/`ext_low`/`ext_volume` alongside the regular
   fields, in `webull/data/quotes/subscribe/message_pb2.py`), not confirmed
   against this REST endpoint's actual JSON body -- the SDK ships no
   schema for that body to check against, and this repo's own fixture
   (`_REAL_SNAPSHOT_ROW` in `tests/test_webull_broker_client.py`) predates
   `extend_hour_required=True` and has no `ext_price`/`ext_volume` keys to
   confirm from. If a real key differs, this fails soft (falls back to the
   regular `price`/`volume`, same as before this existed) rather than
   erroring -- but that also means a wrong guess would go unnoticed without
   an explicit live check during a real pre-market or after-hours session.
   `ext_volume` matters beyond just the displayed price: `cumulative_volume`
   feeds every volume-derived Momentum Ignition Score component (relative
   volume, volume/dollar-volume acceleration, float velocity/turnover -- see
   `metrics/rolling.py`), and the regular-session `volume` field is
   legitimately 0 before 9:30am ET even during active pre-market trading,
   so without `ext_volume` those components read a flat 0 all pre-market
   long, for every candidate, regardless of real activity.

   **Both fields are gated by `_is_outside_regular_session`** (the quote's
   own timestamp, not wall-clock "now") rather than being preferred purely
   because they're present and non-zero: it's unconfirmed whether Webull
   actually zeroes `ext_price`/`ext_volume` out the instant the regular
   session opens, or whether they keep echoing that morning's last
   pre-market value all day. If it's the latter, trusting "field present"
   alone would silently corrupt *regular-session* data with a stale
   pre-market number -- gating on the quote's own timestamp removes that
   risk regardless of which way Webull's real behavior turns out to be.
   One known, self-healing edge case: a rolling metrics window that
   straddles the 9:30am boundary sees `cumulative_volume` apparently drop
   (pre-market's `ext_volume` total -> the regular session's near-zero
   `volume` count), which `metrics/rolling.py`'s `_volume_since` clamps to
   0 rather than negative -- one artificially-flat reading right at the
   open that clears itself once the straddling snapshot ages out of the
   window. There's also a separate `overnight_required`/`ovn_price`/
   `ovn_volume` set for Webull's newer overnight session, deliberately not
   requested here since only pre/after-market was asked for. Use
   `scripts/verify_extended_hours_bars.py` as a template for writing an
   analogous live check of `get_snapshot`'s `ext_price`/`ext_volume`
   fields during a real pre-market/after-hours window -- that script
   checks `get_raw_bars`' `trading_sessions` parameter specifically, not
   this one, but the same "does the live response actually confirm the
   inferred field/value" question applies here.
4. **`support_trading_session` is `"CORE"` by default -- `"ALL"` was tried, and
   directly confirmed live to be rejected outright by this account/endpoint.**
   `_order_payload()` (used for both entries and exits) briefly changed
   this from `"CORE"` to `"ALL"` mid-session, on the strength of Webull's
   own public docs (`developer.webull.com/apis/docs/trade-api/stock/`),
   which document three values -- `"CORE"` (regular session only), `"ALL"`
   (regular + pre/after-hours), `"NIGHT"` (a separate overnight session,
   out of scope here same as `overnight_required`/`ovn_price`/`ovn_volume`
   above) -- with no stated restriction against `"ALL"` for market orders.
   That change was live-tested closing a real position and got an
   immediate, unambiguous rejection: `OAUTH_OPENAPI_PARAM_ERR` / "Parameter
   error, invalid support_trading_session, value: ALL" (HTTP 417). **A live
   API response overrides documentation, full stop** -- reverted back to
   `"CORE"` the moment this was observed, not left in place pending further
   research. Two separate diagnoses touched this field this session and
   both turned out to be based on incomplete information: the first assumed
   an out-of-hours trigger explained a buying-power-reserved-with-no-fill
   report (the user directly corrected this -- the real trigger was during
   core hours, redirected to the position-tracking fix below and the new
   core-hours entry gate); the second assumed the documented `"ALL"` value
   would actually work here. Do not re-attempt `"ALL"` again without a
   successful live order proving this specific account/endpoint accepts it
   -- the docs have now disagreed with the live API once and are not
   sufficient evidence on their own. Practical consequence: with
   `RiskConfig.allow_extended_hours_trading` off (the default -- see "Risk
   sizing" above), `"CORE"` costs entries nothing, since one's never
   attempted outside that window anyway -- **observed live 2026-08-11 that
   it may cost the end-of-day auto-flatten's own exit order**, though: a
   position still open right at the 4:00pm ET close never actually
   flattened, retried every tick with no visible progress. The leading
   diagnosis -- a `"CORE"`-scoped order needs a still-live CORE session,
   already ended by the time the old trigger fired -- was never
   independently confirmed via a captured rejection message (unlike the
   `"ALL"` rejection two paragraphs above, which was); it's the best
   explanation that fits the symptom and the session's own documented
   scope, not a proven root cause. Fixed by moving *when* the flatten
   fires, not the session flag itself -- see "Position management"'s
   "End-of-day auto-flatten" section for `is_within_closing_buffer`. If
   positions still don't flatten within the new buffer window, this
   diagnosis needs revisiting.

   **Extended-hours trading follow-up (2026-08-12).** The user asked to
   enable pre-market/after-hours trading by "changing CORE to ALL" -- the
   finding above directly contradicts that being sufficient on its own.
   Two things changed as a result, both aimed at making this testable
   without another code deploy: (a) `support_trading_session` is no longer
   hardcoded -- `_order_payload()` now reads
   `Settings.webull_support_trading_session` (env var
   `WEBULL_SUPPORT_TRADING_SESSION`, still defaulting to `"CORE"`), so a
   candidate value can be tried live with a config change + restart; (b)
   `RiskConfig.allow_extended_hours_trading` (dashboard-adjustable, off by
   default) now lets entry signals through outside core hours at all --
   see "Risk sizing" above -- since previously even a correctly-accepted
   extended-hours order would never have been attempted in the first
   place. Fresh evidence gathered while investigating, none of it
   conclusive on its own: re-checking developer.webull.com on 2026-08-12
   still shows `"ALL"` as the documented extended-hours value (no newer
   guidance found); but the SDK's own bundled sample scripts
   (`samples/trade/trade_client_v2.py`, `trade_client_v3.py`) never once
   pass `"ALL"` either, across every order type they demonstrate -- only
   `"CORE"` and an unexplained `"N"` (combo/OCO legs) appear. Leading,
   **not yet verified** hypothesis: Webull commonly gates extended-hours
   order entry behind a separate account-level entitlement/agreement (a
   regulatory risk disclosure most brokers require before allowing
   pre-market/after-hours orders at all), which this account may not have
   enabled -- that would explain public docs (globally true) disagreeing
   with this specific account's live behavior (entitlement-gated). Check
   the Webull app's account/trading-permissions settings for an
   extended-hours opt-in before re-testing `"ALL"`.
   `scripts/verify_extended_hours_order.py` is the live test written to
   answer this cleanly: run it during a real pre-market or after-hours
   window, and it tries `"ALL"`/`"NIGHT"`/`"CORE"` (as a control) each as
   a resting, unfilled limit order (never real market exposure) and
   reports exactly which the API accepts. Update
   `WEBULL_SUPPORT_TRADING_SESSION` to whichever value that confirms
   works, and only then turn on `allow_extended_hours_trading` from the
   dashboard for real use.

   **RESOLVED (2026-08-12, ~4:21am ET, genuine pre-market): `"ALL"` is
   confirmed live to work.** Re-ran `verify_extended_hours_order.py`
   against BAOS with a clean (non-rate-limited) request this time --
   `"ALL"` was ACCEPTED (`{'client_order_id': ..., 'order_id': ...}`) and
   cleanly cancelled, directly reversing the 2026-08-10 rejection above.
   `"NIGHT"` was correctly rejected at that same moment with a specific,
   sensible message ("Overnight Trading is only available during the
   Overnight Session, which operates from 8:00pm to 4:00am ET") -- not a
   param error, confirming `"NIGHT"` is a real, correctly-scoped value
   too (and was itself ACCEPTED the night before, ~8:54pm ET, genuinely
   inside its own window). Best-guess explanation for the reversal: an
   account-level extended-hours entitlement was enabled between
   2026-08-10 and 2026-08-12 (the entitlement-gating hypothesis above),
   not a code or documentation error on either side -- this was never
   independently confirmed by checking the account settings directly,
   just inferred from the error message changing character (2026-08-10:
   "invalid parameter"; 2026-08-11 night: "FIXGW not ready for night";
   2026-08-12: accepted outright). Also notable and NOT necessarily
   good news: `"CORE"` was ALSO accepted during this same pre-market
   run (after a few rate-limit retries) -- this softens, without fully
   disproving, the "CORE-scoped order needs a still-live CORE session"
   diagnosis behind the end-of-day auto-flatten timing fix a few
   paragraphs up, since this test only exercised order *acceptance*, not
   *matching/fill* behavior at the 4:00pm close specifically. That fix
   stays in place as a safety margin regardless.

   **Practical upshot:** set `WEBULL_SUPPORT_TRADING_SESSION=ALL` in the
   deployment's `.env` to enable pre-market/after-hours order submission,
   restart the dashboard service, then turn on
   `RiskConfig.allow_extended_hours_trading` from the dashboard Settings
   modal. The code default stays `"CORE"` regardless (no auto-flip), so
   this is an explicit per-deployment opt-in. **Caveat: only verified in
   `TRADING_MODE=sandbox`** -- re-verify with a live-mode test before
   assuming a live account carries the same entitlement.

   **Follow-up, same morning: `"ALL"` does NOT mean every order type is
   accepted outside core hours.** At 9:10am ET (still pre-market), the
   live bot's real `_attach_broker_bracket` call -- a resting OCO
   stop+target bracket, `combo_type="OCO"` with a `STOP_LOSS` leg and a
   `LIMIT` leg -- was rejected with `support_trading_session="ALL"` using
   the EXACT SAME error as the original 2026-08-10 finding
   (`OAUTH_OPENAPI_PARAM_ERR`, "invalid support_trading_session, value:
   ALL"), even though the simple single-leg LIMIT order
   `verify_extended_hours_order.py` tested 49 minutes earlier came back
   clean. The user's diagnosis, which fits both observations: Webull only
   accepts LIMIT orders outside core hours -- a common brokerage
   restriction -- so a `STOP_LOSS`-type leg (or possibly the `OCO`
   combo shape itself) is rejected regardless of `support_trading_session`,
   while a plain `LIMIT` order is fine. Not independently confirmed via
   Webull's own docs/support (the working theory, not a proven root
   cause) but consistent with everything observed so far and a much
   simpler explanation than another entitlement flip 49 minutes apart.
   Compounding this: `_sync_broker_protective_orders`' every-tick retry
   (see "Broker-side (resting) stop/target management" above) kept
   re-attempting and re-failing this exact call every ~5s, burning
   CRITICAL-priority rate-limiter budget continuously and starving
   BACKGROUND-priority discovery/candidate-scanning calls behind it --
   observed live as candidates failing to populate during this same
   window.

   **Design change as a result (2026-08-12): pre-market/after-hours now
   uses LIMIT orders exclusively, with no broker-side resting stop/target
   at all -- core hours are completely unchanged.**
   `OrderManager._order_type_and_limit_price` (used by `submit_signal` for
   both entries and exits) returns a plain `MARKET` order during core
   hours, or a marketable `LIMIT` outside them -- priced
   `OrderManager.EXTENDED_HOURS_LIMIT_BUFFER_PCT` (0.5% default) through
   the current bid/ask (above the ask for a buy-side order, below the bid
   for a sell-side order), aggressive enough to fill like a market order
   under normal conditions while staying within whatever order type the
   broker actually accepts. Separately, `TradingLoop._attach_broker_bracket`
   now no-ops immediately (before any broker call) whenever `now` falls
   outside core hours, rather than attempting and retrying a STOP-based
   resting bracket that's expected to keep failing -- this both stops the
   rate-limit-starving retry loop above and means a pre-market/after-hours
   position is protected purely by `PositionManager`'s existing
   software-side stop/target/VWAP-failure/time-limit checks (the same
   fallback path used for any broker without resting-order support at
   all -- see `check_exit`'s docstring), for as long as it stays open
   outside core hours. The moment core hours resume, the very next
   `_sync_broker_protective_orders` retry attaches a normal broker-side
   bracket as usual.

   **Second reversal (2026-08-12, ~9:49am ET, post sandbox-account-reset):
   `"ALL"` is REJECTED during core hours specifically, resolving the
   apparent contradiction above.** Right after the sandbox account was
   reset (new `account_id`/`account_number`, same app key/secret) and
   `WEBULL_SUPPORT_TRADING_SESSION` was still `ALL` from the earlier
   opt-in, every entry order during core hours failed with the exact same
   `OAUTH_OPENAPI_PARAM_ERR` (HTTP 417, "invalid support_trading_session,
   value: ALL") as the original 2026-08-10 finding -- while a pre-market
   order the same morning, on the same (new) account, was independently
   confirmed to still accept `"ALL"` cleanly. That rules out "account
   entitlement flipped again" as the explanation (a single account can't
   have and not have the entitlement in the same few minutes) and points
   instead at the simplest reading of all three data points together:
   `"ALL"` was never an unconditional account-level toggle -- Webull
   accepts it only for orders actually submitted outside core hours, and
   rejects it as an invalid parameter for one submitted during core hours,
   regardless of what the configured default is. The 2026-08-12 pre-market
   "CORE" order also being accepted (noted above) is consistent with
   this too: `"CORE"` just isn't session-restricted the other way.

   **Fix:** `WebullBrokerClient._order_payload` no longer passes
   `Settings.webull_support_trading_session` straight through as a static
   value. It now calls `is_within_core_trading_hours(order.created_at)`
   (`market_hours.py`) per order and forces `"CORE"` whenever that's true,
   falling back to the configured value (typically `"ALL"`) only when it's
   false. This means a deployment can leave
   `WEBULL_SUPPORT_TRADING_SESSION=ALL` set permanently in `.env` and get
   correct behavior across both core and extended hours automatically,
   rather than needing a manual `.env` edit + service restart at each
   session boundary (which is what this incident's short-term workaround
   was, before the dynamic fix landed). See
   `tests/test_webull_broker_client.py`'s
   `test_order_payload_forces_core_session_during_core_hours_regardless_of_setting`
   and `test_order_payload_uses_configured_session_outside_core_hours` for
   the pinned-clock coverage of both branches.

Streaming (`subscribe_quotes`) is confirmed live and working (2026-08-11) --
see the "Streaming market data" section above and `scripts/verify_streaming.py`.
The sandbox host for `DataStreamingClient`'s `mqtt_host`
(`data-api.sandbox.webull.com`) had to be confirmed live rather than
guessed, since the SDK's own auto-resolution only knows the production
value (`data-api.webull.com`).

## Database

See `db/models.py`. Deliberately does not store a raw tick log --
`market_observations` holds sampled/derived features, not the full tape.
Run `scripts/init_db.py` against `DATABASE_URL` to create tables; introduce
Alembic once the schema needs to evolve without losing data you care about.

**`db/session.py`'s `create_all()` also runs `sync_schema()`** (added
2026-08-11) right after `Base.metadata.create_all()` -- the latter only
ever creates a table that doesn't exist AT ALL, it silently does nothing
to a table that's already there even after its model gains new columns.
Real incident this fixes: the VPS's long-lived `trades` table predated
several `TradeRecord` columns, so `record_trade()` had been failing
(silently, via a broad `except Exception: logger.exception(...)` at the
call site) since those columns were added, leaving the dashboard's
Performance/Trade History cards empty despite real trades closing.
`sync_schema()` diffs every already-existing table's live columns against
what its model expects and `ALTER TABLE ... ADD COLUMN`s in whatever's
missing -- additive-only (never drops/renames/retypes a column, never
adds a constraint, never backfills data into the new column for existing
rows), a cheap patch for this one failure mode, not a substitute for
Alembic once real migrations (dropped columns, backfills, renames) are
needed.

## Backtesting

`backtest/engine.py` reuses the live `Strategy` / `RiskEngine` /
`OrderManager` / `CandidateWatcher` / `TriggerEngine` / `PositionManager`
code, swapping only the broker for `PaperBrokerClient`. Snapshots across all
symbols are merged into one chronological timeline and processed one at a
time so no symbol can see a future bar (its own or another symbol's).

Known gaps, tracked rather than silently ignored: no trading-halt modeling,
no order latency, no partial fills, no L2/order-flow-aware fills.

## Safety

See `config.py` docstring and the README's "Safety model" section. The
three-condition live-trading gate is checked independently in the broker
factory, `WebullBrokerClient.__init__`, and `OrderManager.submit_signal` --
redundant on purpose.

**Kill switch, from the dashboard**: the header's "Safety" badge (top
right) is a button, not just a status indicator -- clicking it opens a
confirmation modal (a second, deliberate step before anything happens) and
then `POST /api/kill-switch` with `{"active": true|false}`.

- **Engaging** (`active: true`) calls `TradingLoop.engage_kill_switch_and_flatten`,
  which does two things with two different timings:
  1. `RiskEngine.engage_kill_switch()` -- a plain boolean flip, takes
     effect the instant it's called (`RiskEngine.evaluate()` checks
     `kill_switch_active` first, before anything else, so no new entry can
     slip through even mid-request).
  2. From that point on, `_process_all_candidates` checks
     `risk_engine.kill_switch_active` on the trading loop's own main
     thread every single tick -- **not** a one-shot request consumed once,
     and **not** synchronously in the dashboard's request handler.
     `_close_all_positions_now` force-closes every open position at
     market (`ExitReason.RISK_KILL_SWITCH`), reusing the exact same
     submit -> fill-or-pending -> finalize path `_manage_position` uses
     for any other exit (`_dispatch_exit_finalization`,
     `_pending_exit_orders`) -- a position that doesn't fill synchronously
     is left pending and picked up by the following tick's ordinary
     `_manage_position`/`_poll_pending_exit` call for that symbol, no
     kill-switch-specific polling code needed.
- **Disengaging** (`active: false`) calls `RiskEngine.release_kill_switch()`,
  which also stops the retry above (the per-tick check reads
  `kill_switch_active` fresh every time) -- any position still open at
  that point is left exactly as it is, matching the dashboard's own
  disengage confirmation text.

**Retries every tick until it actually succeeds (fixed 2026-08-11) --
this was a real, confirmed incident, not a theoretical gap.** Originally
a one-shot request flag (`_close_all_positions_requested`), consumed and
cleared the instant a single tick saw it, *before* `_close_all_positions_now`
even ran. A single failed close attempt on any symbol -- a rate limit, a
`get_snapshot` hiccup, a rejected order, anything -- permanently
abandoned the flatten for that symbol: it just fell back into ordinary
`_manage_position` handling (only exits on a real stop/target/VWAP/
time-limit condition), with nothing left to ever retry a *force*-close
specifically. Reported live: clicking "Engage & Close All Positions"
during core hours, on several separate occasions, appeared to do
nothing at all -- exactly the symptom this gap predicts if the very
first attempt hits any transient failure. Fixed by driving the retry off
`risk_engine.kill_switch_active` directly, re-checked every tick, mirroring
the end-of-day auto-flatten's own already-correct pattern below (which
never had this bug, since it was never a one-shot flag to begin with).

**A second, related risk found and fixed at the same time**:
`_close_all_positions_now` had no guard against a symbol whose close was
already submitted and still pending (`self._pending_exit_orders`) --
harmless as a one-shot action (a real broker fill takes at most a few
seconds), but genuinely dangerous once retried every tick: a still-
pending symbol would get a *second* market exit order submitted against
it before the first one even resolved, risking a real over-sell against
a live broker. `_close_all_positions_now` now skips any symbol already
in `_pending_exit_orders` at the top of its loop -- it's already being
tracked by the normal per-tick pending-exit poll, nothing more to do
until that resolves.

**Why the flatten is deferred to the main thread instead of running inline
in the request handler**: `_close_all_positions_now` mutates `Candidate`/
`Position` objects and the trading loop's internal dicts fairly heavily
(pop positions, transition states, append trades) -- much more than the
simple attribute reassignments the resistance-refresh and Settings-panel
mutations get away with doing directly from another thread (see "Resistance
detection"'s "Periodic refresh" and "Dashboard"'s Settings section). Doing
that from the dashboard's request thread while the main thread might
simultaneously be running `PositionManager.check_exit` on the very same
position would be a real, not just theoretical, race. Deferring it to a
flag the main thread itself consumes keeps 100% of position-closing logic
on the single thread that already owns it, at the cost of up to one
`poll_interval_seconds` of latency before flattening actually starts --
new-entry blocking, the more time-sensitive half, still happens instantly
regardless.

A `get_snapshot` failure for one symbol during a flatten is logged and
skipped, not fatal to the rest -- one bad quote during an emergency stop
shouldn't leave every other position uncautiously open.

**Per-position "Close" button, added 2026-08-12** -- a "Close" column in
the Open Positions table's rightmost column, `POST /api/positions/{symbol}/close`
-> `TradingLoop.request_manual_close(symbol)`. Deliberately a separate
mechanism from the kill switch above, not a reuse of it: the kill switch
is all-or-nothing (closes every open position) and sticky (stays engaged,
blocking all new entries, until manually disengaged) -- neither is right
for "close just this one position and keep trading normally otherwise."
`request_manual_close` adds `symbol` to `self._manual_close_requests` (a
plain set) and calls `RiskEngine.pause_new_entries` (see below), then
returns immediately -- same "record the request, defer the actual work to
the main thread" pattern as `engage_kill_switch_and_flatten`, for the same
thread-safety reason. `_process_all_candidates` retries it every tick via
`_close_all_positions_now(..., symbols=self._manual_close_requests)` --
that method's `symbols` parameter (also added this change) narrows its
loop to a specific subset instead of every open position, so the kill
switch and end-of-day auto-flatten (which both omit it, defaulting to
`None` = every position) are unaffected. `_manual_close_requests` is
pruned against `self._positions` both before and after each attempt, so a
symbol that finalizes (successfully closes) drops out on its own -- no
separate completion callback needed, and nothing is left dangling if the
close succeeds synchronously within the same tick it was attempted.

**Why this exists: a real incident, same day.** A software-managed
stop-loss exit for one symbol kept losing the account-wide Webull
rate-limit race, tick after tick, for over 20 seconds per attempt --
`max_simultaneous_positions` was set to 0 (unlimited) with
`max_position_size_pct` at 100%, and with ~137 candidates active the bot
was very likely attempting many simultaneous entries at once, each a
CRITICAL-priority `place_order` call directly competing with the stuck
exit for the same rate-limiter slots. `CallPriority`'s tiers only help
CRITICAL win against BACKGROUND (discovery) traffic -- they do nothing
against a flood of *other* CRITICAL traffic. `RiskEngine.pause_new_entries(seconds, now=None)`
sets `self._entries_paused_until`, checked in `evaluate()` right after the
kill-switch check (rejecting with the new `RiskEventType.ENTRIES_TEMPORARILY_PAUSED`)
-- a short, self-expiring block (`TradingLoopConfig.manual_close_entry_pause_seconds`,
20.0s default) that clears itself with no dashboard action needed, unlike
`kill_switch_active`. A second call before the first pause expires simply
extends the end time rather than stacking. This doesn't guarantee the
requested close wins its very next rate-limiter slot, but it removes the
single biggest source of competing CRITICAL-tier load while it's active.

**End-of-day auto-flatten** (distinct from the kill switch above, added
at the same time as the core trading hours entry gate in "Risk sizing"):
`_process_all_candidates` checks, every tick, whether
`market_hours.is_within_closing_buffer(now, config.end_of_day_flatten_buffer_minutes)`
is true and `self._positions` is non-empty; if so it calls the exact same
`_close_all_positions_now` the kill switch uses, just with
`exit_reason=ExitReason.END_OF_CORE_HOURS` instead of the default
`RISK_KILL_SWITCH` (`_close_all_positions_now` now takes that as a
parameter for this reason).

**Fires BEFORE the close, not at/after it (extended 2026-08-11).**
Originally gated on `market_hours.is_after_core_trading_hours(now)` --
true only at/after the literal 4:00pm ET close. Observed live the same
day this was a real bug, not a theoretical one: a position still open
right at the close never actually flattened, retried every tick with no
visible progress. Leading diagnosis, not independently confirmed via a
captured rejection message (the flatten's own error handling logs and
swallows per-symbol failures rather than surfacing the exact reason) --
every order this project submits (including the flatten's own exit
order) is scoped to the `"CORE"` trading session (see
`WebullBrokerClient._order_payload`'s `support_trading_session` note),
and that session has, by definition, already ended by the time
`is_after_core_trading_hours` first turns true, so a `"CORE"`-scoped
order submitted then would need a still-live session it no longer has.
`is_within_closing_buffer` fires
`TradingLoopConfig.end_of_day_flatten_buffer_minutes` (2 minutes default)
*before* the close instead, while the CORE session is still live enough
for the exit order to actually execute. Still fires every tick from that
point on (not a one-shot window), so a position opened in the last
moments before the buffer started -- `RiskEngine.evaluate` still allows
entries right up to 4:00pm -- is still caught, on its very next tick.

Two things make this deliberately *not* just "call
`engage_kill_switch_and_flatten` on a timer":

1. It never sets `risk_engine.kill_switch_active`. The kill switch is a
   sticky, manual halt a human clears from the dashboard; forcing every
   user to re-enable trading by hand every single morning would turn a
   routine end-of-day flatten into a recurring manual chore, and isn't
   what "close positions at the end of the day" was asked for. New entries
   are already independently blocked outside core hours by `RiskEngine.evaluate`'s
   own gate (see "Risk sizing"), so nothing else needs to hold that door
   shut overnight.
2. It's not a one-shot flag consumed once, unlike the kill-switch's
   `_close_all_positions_requested`. It's a plain clock check re-evaluated
   every tick, which is deliberately cheap to leave running forever: once
   the day's positions are actually flattened, `self._positions` is empty
   and the `if self._positions and ...` guard short-circuits before ever
   touching the broker again, all the way until the next position opens
   the following session.

Ordering matters and is intentional: this check runs *after* the
kill-switch-requested block in `_process_all_candidates`, so if both are
somehow true on the same tick (kill switch engaged AND after hours), the
kill-switch flatten runs first, empties `self._positions`, and the
auto-flatten check that follows finds nothing left to do -- a closed
position is never attributed to the wrong `ExitReason` by a second flatten
attempt re-processing it.
