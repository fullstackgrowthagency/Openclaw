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

`data/universe.py` feeds the scanner from **four** independent, live-verified
Webull screener sources, combined by `MultiSourceUniverseProvider`:

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
universe returns gets scanned, so full-scan duration scales with however
large that universe is on a given cycle rather than being bounded by a
fixed number. `TradingLoopConfig.universe_rescan_interval_seconds` is
sized as a floor between scan *starts*, not a target duration, given that
a full scan now routinely takes longer than any reasonable interval --
see that config's comments for the measured numbers (and their caveat:
they predate the wider price range, pagination, and 4th source, so they
understate current scan time).

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
analysis** (`metrics/volume_profile.py`), computed once per candidate at
discovery time (`BroadScanner._compute_static_resistance_levels`) and
stored on `candidate.static_resistance_levels`.

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
and no external dependencies (no CDN scripts) -- it polls the REST
endpoints every 5s. Keep it that way unless there's a real reason to add a
frontend toolchain; a monitoring dashboard for a single operator doesn't
need one.

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

Two non-obvious things worth knowing if you're debugging this client:

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
