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
RiskEngine.evaluate()      (deterministic: risk-per-trade sizing, exposure/
      |                     position/trade-count caps, spread/liquidity
      |                     gates, cooldowns, kill switch)
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
decides both *whether* to trade it and, if so, *how many shares*. Four of
its `RiskConfig` fields are adjustable live from the dashboard's Settings
button (top right -- see "Dashboard" below) via `GET`/`POST
/api/risk-settings`, which mutate the running `RiskEngine.config` in place;
changes apply to the very next `Signal` evaluated, no restart needed.

**Core trading hours gate** (checked immediately after the kill switch, before
any of the sizing math below, and not configurable): `evaluate()` refuses
every entry signal outside 9:30am-4:00pm ET, Monday-Friday
(`market_hours.is_within_core_trading_hours`), rejecting with
`RiskEventType.OUTSIDE_CORE_TRADING_HOURS`. Added after a production report
of trades filling *during* core hours whose resulting positions then went
untracked (see "Position tracking can be lost on a broker-side fill
reconciliation failure" below) -- investigating that report surfaced that
there was no explicit application-level guarantee at all that entries only
happen in core hours; this closes that gap outright rather than leaving it
to whatever Webull itself does with an out-of-hours order (see the
`support_trading_session` note in "Webull integration"). This only ever
applies to entries: `OrderManager.submit_signal` never routes
`EXIT`/`SCALE_OUT` signals through `evaluate()` at all (see its own
docstring), so a stop-loss or the end-of-core-hours auto-flatten (see
"Position management" below) is never blocked by this gate -- `evaluate()`
itself has no action-type carve-out, it's simply never called for exits.

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
rejected (`MAX_EXPOSURE_HIT`). Otherwise, whatever risk-room remains
becomes a ceiling on this trade's own budgeted risk (see #3) -- shrinking
it rather than rejecting outright, so one trade can't single-handedly blow
through the fleet-wide ceiling even when its own per-trade budget would
otherwise allow more.

**3. Risk-based position sizing** (`risk_per_trade_pct`, default 5% of
equity) -- the primary sizing driver. Budgeted risk = `equity *
risk_per_trade_pct / 100`, capped by whatever room #2 left; shares = that
dollar amount divided by the entry-to-stop distance. A tighter stop lets
this budget buy more shares (same dollar risk, smaller per-share risk); a
wider stop buys fewer. `RiskDecision.risk_amount` reports the actual
(post-cap) dollar amount budgeted, for transparency into what really drove
the sizing.

**4. Position-size ceiling** (`max_position_size_pct`, default 100% of
**buying power**, not equity) -- an independent cap on any single
position's notional size (`shares * entry_price`), regardless of how
favorable the stop distance made #3's math. Buying power (`broker.get_buying_power()`)
is used rather than equity specifically so this reflects capital actually
available to deploy right now, not total account value that may already
be committed to other open positions.

**Final sizing**: `max_shares = min(shares from #3, shares from #4)`. If
that comes out to zero (e.g. no risk-room left after #2, or a
degenerate stop distance), the trade is rejected
(`Computed position size is zero...`) rather than silently shrunk to
something nonsensical.

**Verification status**: `tests/test_risk_engine.py` covers all four
mechanics above (including the shrink-not-reject behavior of #2 and the
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
process's lifetime) -- deliberately conservative synthetic values instead
of leaving it unprotected: `stop_price` is set to
`risk_engine.config.risk_per_trade_pct` below the *current* price for a
long (above it for a short), a plain %-off-current-price line rather than
a real entry-to-stop risk calculation, and `target_price` is left `None`
entirely -- it rides on the breakeven/trailing-stop rules only, same as
any position whose target has already been hit (see "Position management"
below). `strategy_name` is set to the sentinel `"reconciled_at_startup"`
so an adopted position is always distinguishable from a real signal-driven
one in the trade history. A `get_positions()` or per-symbol
`get_snapshot()` failure during reconciliation is logged and skipped, not
fatal to the rest -- consistent with every other broker-loop failure mode
in this file (kill-switch flatten, batched snapshot fetch, etc.).

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
Every position falls back automatically and silently to the pure-software
behavior described above whenever the connected broker lacks this
capability (tests, `PaperBrokerClient`, backtests) **or** a broker call in
this chain fails for any reason (rejected order, network error) -- broker-
side management is an enhancement layered on top of the existing fallback,
never a precondition for a position being tracked at all.

**The lifecycle, symbol by symbol:**

1. **Attach** (`TradingLoop._attach_broker_bracket`, called from
   `_confirm_entry_filled` right after a fresh entry fill, and again after
   startup/periodic adoption in `reconcile_positions_from_broker`). Builds
   a resting `STOP` leg at the full quantity and (if a target is set and
   the position is big enough to split into two whole-share halves -- the
   same floor-to-half-share rule `OrderManager.submit_signal`'s `SCALE_OUT`
   path already uses) a resting `LIMIT` leg at half the quantity, and
   places them as one `OCO` pair via `OrderManager.place_resting_bracket`
   (or a lone `STOP` via `place_resting_stop` when there's no target --
   adoption never has one). Stores both legs' broker order ids and the
   synced stop price on `Position` (`broker_stop_order_id`,
   `broker_target_order_id`, `broker_stop_price_synced`).
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
   immediately re-attaches a fresh lone resting stop for the remainder**
   (`_attach_broker_bracket` again, now with no target -- `partial_exit_taken`
   is `True`): Webull's `OCO` logic auto-cancels the sibling leg the
   instant one fills, so the original stop (which was sized for the FULL
   original quantity) is gone the moment the target fills, and the
   remaining shares would otherwise be naked until the next breakeven/
   trailing sync happened to run.
4. **Sync on a stop-price change** (`TradingLoop._sync_broker_protective_orders`,
   called from `_manage_position` whenever `check_exit` runs and finds no
   exit condition). Compares `position.stop_price` (which `check_exit`'s
   breakeven/trailing math may have just moved) against
   `broker_stop_price_synced`; if they differ, cancels **every** resting
   leg for this position (not just the stop) and calls
   `_attach_broker_bracket` again for a completely fresh resting order (or
   pair, if a target is still active). Cancelling both legs together,
   rather than trying to update just the stop leg's price in place, is
   deliberate: it sidesteps both `modify_order`'s inconclusive live result
   *and* the unconfirmed question of whether cancelling a single leg of an
   already-placed `OCO` combo cancels or orphans its sibling.
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
- `GET`/`POST /api/risk-settings` expose four `RiskConfig` fields (see
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
rarer, more structural decision (e.g. `max_simultaneous_positions`) better
made by editing the config in code. One current limitation: both
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
4. **`support_trading_session` is `"CORE"` -- `"ALL"` was tried, and
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
   sufficient evidence on their own. Practical consequence:
   `RiskEngine.evaluate`'s core-hours entry gate (see "Risk sizing" above)
   means `"CORE"` costs entries nothing, since one's never attempted outside
   that window anyway -- but the end-of-core-hours auto-flatten's exit
   order fires right at the 4:00pm ET close boundary, and it's not yet
   confirmed whether a `"CORE"`-flagged order placed at that exact instant
   executes cleanly or risks the original "accepted but won't execute"
   symptom this field was first changed to fix. Watch for that specifically
   the next time a position is still open at the close.

Streaming (`subscribe_quotes`) intentionally raises `NotImplementedError`:
`DataStreamingClient` needs an `mqtt_host`, and only the production value
(`data-api.webull.com`) is documented anywhere found so far. Don't guess a
sandbox equivalent -- confirm it live (or from support/docs) first.

## Database

See `db/models.py`. Deliberately does not store a raw tick log --
`market_observations` holds sampled/derived features, not the full tape.
Run `scripts/init_db.py` against `DATABASE_URL` to create tables; introduce
Alembic once the schema needs to evolve without losing data you care about.

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
  2. Sets a request flag (`TradingLoop._close_all_positions_requested`)
     that `_process_all_candidates` consumes on the trading loop's own
     main thread, at the start of its very next tick -- **not**
     synchronously in the dashboard's request handler. `_close_all_positions_now`
     then force-closes every open position at market
     (`ExitReason.RISK_KILL_SWITCH`), reusing the exact same
     submit -> fill-or-pending -> finalize path `_manage_position` uses
     for any other exit (`_dispatch_exit_finalization`,
     `_pending_exit_orders`) -- a position that doesn't fill synchronously
     is left pending and picked up by the following tick's ordinary
     `_manage_position`/`_poll_pending_exit` call for that symbol, no
     kill-switch-specific polling code needed.
- **Disengaging** (`active: false`) just calls `RiskEngine.release_kill_switch()`
  -- nothing to flatten on the way out, positions (if any exist) are left
  exactly as they are.

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

**End-of-core-hours auto-flatten** (distinct from the kill switch above,
added at the same time as the core trading hours entry gate in "Risk
sizing"): `_process_all_candidates` checks, every tick, whether
`market_hours.is_after_core_trading_hours(now)` is true and
`self._positions` is non-empty; if so it calls the exact same
`_close_all_positions_now` the kill switch uses, just with
`exit_reason=ExitReason.END_OF_CORE_HOURS` instead of the default
`RISK_KILL_SWITCH` (`_close_all_positions_now` now takes that as a
parameter for this reason). Two things make this deliberately *not* just
"call `engage_kill_switch_and_flatten` on a timer":

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
