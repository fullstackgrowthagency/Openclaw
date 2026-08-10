"""
Tier 2: Candidate Watcher.

Recomputes momentum metrics and the Momentum Ignition Score on every new
snapshot for symbols already in WATCHING/HEATING_UP/ARMED, and drives the
corresponding state transitions. Reaching ARMED means "hand this to the
Trigger Engine for real-time entry monitoring" -- it is not itself a trade
decision.

Structural vs. temporary disqualification (read before touching `update()`):
spread and dollar volume used to permanently REJECT a candidate the moment
either crossed its threshold. That's wrong for this bot's actual target
pattern -- a low-float name can go from an unwatchable 8% spread to a
tight, tradeable one within seconds once real volume shows up, and a
permanent rejection would mean the bot can never come back to a name it
gave up on one bad tick earlier. These are now tracked as a *temporary*,
every-tick-recomputed `trade_eligible`/`block_reasons` pair on the
Candidate instead (see enums.TradeBlockReason) -- state (WATCHING/
HEATING_UP/ARMED/...) keeps being driven purely by the Momentum Ignition
Score, completely independent of tradeability, so a candidate can be
ARMED (score-qualified) while still not trade_eligible (e.g. spread is
temporarily too wide right now). CandidateState.REJECTED remains reserved
for genuinely permanent/structural disqualification (free float too
large, unsupported security, etc. -- see BroadScanner and
state_machine.py's _ALWAYS_CAN_REJECT), which this module doesn't set at
all anymore.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta

from ..enums import CandidateState, TradeBlockReason
from ..metrics.rolling import MAX_HISTORY_MINUTES, compute_metrics
from ..models import Candidate, MarketSnapshot
from ..scoring.momentum_ignition_score import MISConfig, compute_score
from ..state_machine import transition


@dataclass
class WatcherConfig:
    heating_up_score_threshold: float = 40.0
    armed_score_threshold: float = 70.0
    cooling_off_ratio: float = 0.5   # drop back a stage if score falls below threshold * this ratio
    # Both of these gate `trade_eligible`, not `state` -- see module
    # docstring. A candidate crossing either threshold stays in whatever
    # state its score already put it in; it just can't be entered until
    # the condition clears (checked fresh every tick, so it clears itself
    # automatically -- nothing has to explicitly "un-block" a candidate).
    max_spread_pct: float = 2.0
    min_dollar_volume: float = 500_000


class CandidateWatcher:
    def __init__(self, mis_config: MISConfig | None = None, config: WatcherConfig | None = None):
        self.mis_config = mis_config or MISConfig.load()
        self.config = config or WatcherConfig()
        self._history: dict[str, list[MarketSnapshot]] = defaultdict(list)

    def _push_history(
        self, symbol: str, snapshot: MarketSnapshot, seed_snapshots: list[MarketSnapshot]
    ) -> list[MarketSnapshot]:
        history = self._history[symbol]
        # Splice in the discovery-time seed exactly once -- only when this
        # symbol's own history is still empty, so a candidate that cools
        # off and re-enters WATCHING later doesn't get re-seeded on top of
        # real accumulated ticks. See metrics/rolling.seed_history_from_bars
        # for why this exists: without it, every window-diffed metric
        # (float velocity, volume/dollar-volume acceleration, price
        # acceleration, short-term relative volume) reads its cold-start
        # neutral default for several real minutes after discovery, blind
        # to a move that may have already happened well before this
        # candidate was found -- exactly backwards for a bot whose
        # candidates are discovered BECAUSE they already moved.
        if not history and seed_snapshots:
            history.extend(seed_snapshots)
        history.append(snapshot)
        cutoff = snapshot.timestamp - timedelta(minutes=MAX_HISTORY_MINUTES)
        while history and history[0].timestamp < cutoff:
            history.pop(0)
        return history

    def update(self, candidate: Candidate, snapshot: MarketSnapshot) -> Candidate:
        if candidate.state == CandidateState.REJECTED:
            return candidate

        candidate.last_price = snapshot.last_price
        history = self._push_history(candidate.symbol, snapshot, candidate.seed_snapshots)

        free_float = candidate.float_data.free_float_shares if candidate.float_data else None
        # RVOL baseline lookup (metrics/volume_baseline.py) -- None for
        # paper/backtest mode or a candidate whose baseline failed to
        # compute at discovery, in which case compute_metrics' relative_volume
        # fields fall back to their existing neutral default, same as before
        # this existed.
        typical_same_time = typical_1m = typical_5m = None
        if candidate.volume_baseline is not None:
            typical_same_time, typical_1m, typical_5m = candidate.volume_baseline.lookup(snapshot.timestamp)
        # IMPORTANT: metrics/strategies must see the resistance level as it
        # stood *before* this snapshot -- a bar's high_of_day always includes
        # its own last_price, so folding it in before the breakout check
        # would make resistance >= current price on every single bar and no
        # breakout could ever trigger. `update_resistance` (called by the
        # caller *after* the trigger engine has looked at this bar) is what
        # rolls the current bar's high into resistance for the *next* bar.
        metrics = compute_metrics(
            free_float,
            history,
            resistance_level=candidate.resistance_level,
            typical_volume_same_time=typical_same_time,
            typical_volume_1m=typical_1m,
            typical_volume_5m=typical_5m,
        )
        candidate.latest_metrics = metrics
        candidate.latest_score = compute_score(metrics, candidate.float_data, self.mis_config)

        # Recomputed from scratch every tick (not merged with the previous
        # value) so a resolved condition clears itself the moment it's no
        # longer true -- see module docstring. This does NOT return early:
        # the score/state-transition logic below still runs regardless of
        # trade eligibility, since eligibility and state are orthogonal.
        block_reasons: list[TradeBlockReason] = []
        if metrics.spread_pct > self.config.max_spread_pct:
            block_reasons.append(TradeBlockReason.SPREAD_TOO_WIDE)
        if metrics.dollar_volume < self.config.min_dollar_volume:
            block_reasons.append(TradeBlockReason.LOW_LIQUIDITY)
        candidate.block_reasons = block_reasons
        candidate.trade_eligible = not block_reasons

        score = candidate.latest_score.score

        if candidate.state == CandidateState.WATCHING and score >= self.config.heating_up_score_threshold:
            transition(candidate, CandidateState.HEATING_UP, now=snapshot.timestamp, reason=f"MIS {score:.1f} crossed heating-up threshold")
        elif candidate.state == CandidateState.HEATING_UP:
            if score >= self.config.armed_score_threshold:
                transition(candidate, CandidateState.ARMED, now=snapshot.timestamp, reason=f"MIS {score:.1f} crossed armed threshold")
            elif score < self.config.heating_up_score_threshold * self.config.cooling_off_ratio:
                transition(candidate, CandidateState.WATCHING, now=snapshot.timestamp, reason=f"MIS {score:.1f} cooled off")
        elif candidate.state == CandidateState.ARMED and score < self.config.heating_up_score_threshold:
            transition(candidate, CandidateState.HEATING_UP, now=snapshot.timestamp, reason=f"MIS {score:.1f} cooled off from armed")

        return candidate

    def update_resistance(self, candidate: Candidate, snapshot: MarketSnapshot) -> None:
        """Roll this bar's high into the running high for future bars, then
        merge in any static resistance levels from volume-profile analysis
        (candidate.static_resistance_levels, set once at discovery -- see
        BroadScanner._compute_static_resistance_levels and
        metrics/volume_profile.py). Callers must invoke this AFTER the
        trigger engine has evaluated the current snapshot against
        `candidate.resistance_level`, not before -- see the note in
        `update()`.

        The merge picks the *nearest* static level still above the running
        high, not the highest one available: once intraday price trades
        through a static level, it's no longer resistance (it may even act
        as support going forward), so re-picking the closest remaining
        ceiling each time keeps resistance_level meaning "the next real
        obstacle," not "the biggest one on record." Falls back to the
        plain running high (this method's entire behavior before static
        levels existed) when no static level remains above it, or when
        static_resistance_levels is empty -- e.g. paper/backtest mode, or a
        broker/lookup that couldn't supply them (see
        BroadScanner._compute_static_resistance_levels: that failure mode
        is intentionally silent, not an error)."""
        running_high = max(candidate.resistance_level or 0.0, snapshot.high_of_day)
        levels_above = [level for level in candidate.static_resistance_levels if level > running_high]
        candidate.resistance_level = min(levels_above) if levels_above else (running_high or None)
