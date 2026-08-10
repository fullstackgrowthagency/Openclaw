const REFRESH_MS = 5000;

// Column explanations for the little "i" buttons in the Candidates table
// header. Keep these in sync with the actual logic they describe --
// scanner/candidate_watcher.py (state/score/resistance/reason) and
// dashboard/app.py's /api/candidates (price).
const COLUMN_INFO = {
  state: {
    title: "State",
    body:
      "A candidate moves through a fixed lifecycle:\n" +
      "DISCOVERED → WATCHING → HEATING_UP → ARMED → TRIGGERED → ENTERED → MANAGING → EXITED → COOLDOWN\n\n" +
      "WATCHING: passed initial price/volume/float filters.\n" +
      "HEATING_UP: Momentum Ignition Score crossed 40.\n" +
      "ARMED: score crossed 70 -- handed to the trigger engine for real-time entry monitoring.\n" +
      "TRIGGERED: a strategy's breakout confirmation fired and an entry order was submitted.\n" +
      "ENTERED / MANAGING: order filled; position is open and being actively managed.\n" +
      "EXITED / COOLDOWN: position closed; brief wait before this symbol can be watched again.\n\n" +
      "REJECTED is terminal and can happen from almost any state (e.g. a bad spread/liquidity reading) -- see the Reason column for why. A high score never places a trade by itself -- ARMED only means \"watch closely.\"",
  },
  score: {
    title: "Score",
    body:
      "The Momentum Ignition Score (MIS), 0-100 -- a weighted blend of 11 components recomputed on every tick, weighted so current real-time activity (how \"popular\" a name already is) outranks names that only look structurally attractive:\n\n" +
      "• Float score -- lower free float scores higher\n" +
      "• Float velocity -- % of float traded in the last 5 minutes\n" +
      "• Float turnover -- % of float already traded today (cumulative)\n" +
      "• Relative volume -- today's volume vs. typical for this time of day\n" +
      "• Short-term relative volume -- same idea, windowed to the last 5 minutes for a fresher read\n" +
      "• Volume acceleration -- is volume ramping up, not just high\n" +
      "• Dollar-volume acceleration -- same idea in dollar terms, which also captures price moving between windows\n" +
      "• Price acceleration -- how fast price itself is moving\n" +
      "• Breakout proximity -- distance to resistance / high of day\n" +
      "• Trend quality -- position relative to VWAP\n" +
      "• Liquidity -- spread tightness + dollar volume\n\n" +
      "Crossing 40 promotes WATCHING → HEATING_UP; crossing 70 promotes to ARMED. The candidates table is sorted by this score, so the reweighting directly controls what shows up at the top. The weights are unvalidated starting values, not backtested.",
  },
  price: {
    title: "Price",
    body:
      "The candidate's last known price, from the most recent market snapshot the bot processed for this symbol. It updates once per poll cycle, not continuously -- see the Updated column for exactly when. Shows \"--\" until this candidate's first tick.",
  },
  resistance: {
    title: "Resistance",
    body:
      "The nearest price level expected to act as resistance, combining two sources:\n\n" +
      "1. The running high of day for this candidate (only ever moves up)\n" +
      "2. Static levels from volume-profile analysis -- price zones where a lot of historical volume traded (\"high-volume nodes\"), which tend to act as real support/resistance. Computed at discovery and periodically refreshed (every 5 minutes by default) for as long as this candidate hasn't entered a position yet, so it reflects the day's volume profile filling in, not just whatever existed the moment it was first found\n\n" +
      "Whichever of these is the closest one still above the current price is shown. This is also the actual price a breakout strategy waits to see cleared before entering a trade once a candidate is ARMED.",
  },
  reason: {
    title: "Reason",
    body:
      "Why this candidate is in its current state -- the reason logged the last time it changed state. For example, \"failed liquidity/spread check\" for a REJECTED candidate, or \"MIS 45.2 crossed heating-up threshold\" for one that just started heating up.",
  },
  "score-breakdown": {
    title: "Score Weighting Breakdown",
    body:
      "By default, a sanity check for the MIS weights (scoring/weights.yaml): each component's raw 0-100 sub-score, averaged over recent history, multiplied by its current normalized weight. Sorted by weighted contribution descending -- this is what's actually driving scores up in practice, not just what the weights were intended to emphasize.\n\n" +
      "Click a row in the Candidates table above to instead see that exact candidate's own live score breakdown (no averaging -- its most recent tick only). Click the same row again, or the \"(show all candidates)\" link, to go back to the historical view.\n\n" +
      "The historical view only averages rows from the most recent weights_version, since older/newer formula versions have different components and mixing them would be meaningless. If a component you expect to matter (e.g. after a reweight) isn't near the top here, that's a sign the weights or thresholds need another pass.",
  },
  "score-history": {
    title: "Ticker Scanner",
    body:
      "Enter any ticker and it's run through the broad scanner's structural gates (price range, free float, volume floor) right now, the same checks a symbol has to clear during the bot's normal periodic universe rescan -- except this happens immediately for one ticker instead of waiting for the next full pass, which can take many minutes.\n\n" +
      "If it passes, it's added to the live Candidates table above and starts being watched on the bot's normal cadence. If it's rejected, you'll see why (e.g. price out of range, float too large, or all three volume floors missed). If it's already being tracked, its current real state is shown instead of re-scanning it.\n\n" +
      "Below the result, any recorded Momentum Ignition Score history for that symbol is also shown -- one row per tick it's been scored, most recent first, with its top 3 highest-contributing raw sub-scores that tick. This will be empty right after a fresh scan (scoring only starts once CandidateWatcher ticks it) but fills in on the next poll cycle.",
  },
  "entry-strategies": {
    title: "Entry Strategies",
    body:
      "The bot runs 8 entry strategies at once. Every tick, each ARMED candidate is checked against all 8 in the order below; whichever one fires first for that tick places the trade. They're ordered most selective/confirmed first, most permissive last, so a broad catch-all never crowds out a stricter, more reliable setup that would also have fired.\n\n" +
      "1. Refined Breakout -- price breaks above the resistance level, but only within a narrow window: 0% to 3% above it. Catches a breakout while it's actually happening, not one that already ran far past resistance hours ago. Stop sits just below resistance.\n\n" +
      "2. Opening Range Breakout -- watches the high of the first 5 minutes of the session and enters once price clears it. Works even for a stock with no real resistance history yet, like a recent IPO.\n\n" +
      "3. VWAP Reclaim Continuation -- waits for a meaningful dip below VWAP, then enters once price reclaims VWAP with fresh volume. Catches the \"second leg\" of a move after a shakeout. Stop sits just under VWAP itself, since VWAP holding as support is exactly what this entry is betting on.\n\n" +
      "4. Momentum Breakout -- the original breakout above resistance, with no upper cap (unlike #1). Still fires for breakouts that Refined Breakout's 3% window has already passed.\n\n" +
      "5. Breakout Pullback -- after a resistance breakout, waits for a controlled pullback on fading volume, then enters once price reclaims the pullback high. Avoids buying the very first spike; targets a steadier, less exhausted move.\n\n" +
      "6. Ignition Pullback -- the same pullback-then-reclaim pattern as #5, but anchored to a volume/float-turnover surge instead of a resistance level, so it works even when there's no nearby resistance to react to.\n\n" +
      "7. Volatility Contraction (\"flag/pennant\") -- looks for price tightening into a narrow range after an initial move, then enters once that range expands again with volume. A classic continuation setup for a stock that's \"coiling\" before its next leg.\n\n" +
      "8. Volume Ignition -- the broadest strategy, placed last on purpose so it only fires when none of the above already did. Triggers on a sudden volume acceleration or a float-turnover surge, combined with rising price above VWAP, with no resistance level required. This is what catches a stock whose resistance is too far away to be a useful reference at all.\n\n" +
      "Every strategy's target is computed from the same live \"Minimum reward:risk ratio\" setting in the Settings panel (top right), not a fixed number baked into each strategy -- tightening or loosening that one dial changes what all 8 target on their next signal. Hitting target doesn't fully close a trade, either: it sells half and lets the rest keep riding the stop -- see the Position Management guide for the full exit sequence (breakeven, trailing stop, and why trailing only kicks in after that first partial).\n\n" +
      "Every strategy still flows through the same risk checks (position sizing, per-trade risk, daily loss limit, spread/liquidity gates, cooldowns) before an order is placed -- a strategy signal never bypasses risk management. None of these have been backtested or run live yet; their exact thresholds are starting points, not tuned values.",
  },
  "position-management": {
    title: "Position Management",
    body:
      "Once an entry fills, PositionManager checks the position every poll tick and decides whether to exit -- in this order, first match wins:\n\n" +
      "1. Stop hit -- price at or below the current stop. Closes the entire remaining position.\n\n" +
      "2. Target hit (first time only) -- sells HALF the position instead of closing it entirely. The strategy's own target (see the Entry Strategies guide -- it's the same live \"Minimum reward:risk ratio\" setting for all 8) decides when this fires. This is deliberate: banking some profit at a known level while letting the other half keep riding, instead of a hard cap that exits the whole trade the instant it reaches target. If the position is too small to split into two whole shares, this falls back to closing it entirely.\n\n" +
      "3. VWAP failure -- price drops meaningfully below VWAP, regardless of where the stop sits. Full close.\n\n" +
      "4. Time limit (30 minutes by default) -- if none of the above triggered by then. Full close.\n\n" +
      "Two rules move the stop itself, both adjustable in Settings (top right):\n\n" +
      "• Breakeven -- once price is up 5% from entry, the stop jumps to exactly your entry price (a guaranteed no-loss floor). Active from the moment a position opens.\n\n" +
      "• Trailing stop -- recomputed every tick as 3% below the current price, and only ever moves the stop up, never down. This one does NOT start until AFTER the first target hit (rule 2 above) -- before that, only the breakeven rule can move the stop. The idea: let the trade prove itself by reaching target and banking some profit first, then use the trailing stop to protect the remaining half's gains without capping them at a fixed level.\n\n" +
      "Put together: a fresh position rides on its original stop, then breakeven once it's up 5%, then hits target and banks half its size, then trails the remaining half with a 3%-below-price stop that only ever tightens -- letting a strong move keep running instead of being capped at the target.\n\n" +
      "None of this has been backtested or run live yet -- the 5% breakeven trigger and 3% trailing distance are starting points, not tuned values.",
  },
  "risk-events": {
    title: "Recent Risk Events",
    body:
      "A live log of every time RiskEngine blocks a trade or a fleet-wide safety control fires -- read directly from the running risk engine, not the database, so it reflects the current process exactly.\n\n" +
      "An entry is logged whenever a signal is rejected, or the kill switch is engaged. Event types you'll see:\n\n" +
      "• trade_rejected -- a generic reject, e.g. no stop-loss on the signal, or the computed position size came out to zero\n" +
      "• daily_loss_limit_hit -- the day's max loss limit has been reached\n" +
      "• max_exposure_hit -- taking this trade would push total assumed stop-loss risk across all open positions past the cap\n" +
      "• max_positions_hit -- already at the max number of simultaneous open positions\n" +
      "• max_trades_per_ticker_hit / max_trades_per_day_hit -- per-symbol or per-day trade count caps reached\n" +
      "• spread_too_wide / liquidity_too_low -- the spread or dollar-volume gate failed\n" +
      "• slippage_protection_triggered\n" +
      "• cooldown_active -- this symbol is still in its post-loss cooldown window\n" +
      "• kill_switch_engaged -- either a new trade was blocked because the kill switch is on, or this is the event logged the moment the switch itself gets engaged\n" +
      "• min_risk_reward_not_met -- the signal's reward:risk ratio is below the \"Minimum reward:risk ratio\" setting\n\n" +
      "Each row shows the timestamp, event type, symbol (\"--\" for switch-wide events like the kill switch), and a human-readable reason, e.g. \"Spread 1.20% exceeds max 0.50%.\" It's essentially a real-time audit trail of why a trade was or wasn't taken -- useful for tuning the risk settings in the Settings panel without digging through logs. Shows the most recent 20 events, newest first.",
  },
};

function initInfoModal() {
  const overlay = document.getElementById("info-modal-overlay");
  const title = document.getElementById("info-modal-title");
  const body = document.getElementById("info-modal-body");
  const closeBtn = document.getElementById("info-modal-close");
  if (!overlay) return;

  function open(key) {
    const info = COLUMN_INFO[key];
    if (!info) return;
    title.textContent = info.title;
    body.textContent = info.body;
    overlay.classList.add("open");
  }

  function close() {
    overlay.classList.remove("open");
  }

  document.addEventListener("click", (e) => {
    const btn = e.target.closest(".info-btn, .guide-btn");
    if (btn) {
      open(btn.dataset.info);
    }
  });

  closeBtn.addEventListener("click", close);
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) close();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") close();
  });
}

// Two separate config objects live on the running TradingLoop
// (risk_engine.config vs. position_manager.config), each with its own
// GET/POST pair -- see dashboard/app.py. Keys must match each Update
// model's fields and the input ids ("setting-<key>") in index.html's
// settings modal.
const SETTINGS_GROUPS = [
  { endpoint: "/api/risk-settings", fields: ["risk_per_trade_pct", "min_risk_reward_ratio", "max_position_size_pct", "max_total_risk_pct"] },
  { endpoint: "/api/position-settings", fields: ["breakeven_trigger_pct", "trailing_stop_pct"] },
];

function openSettingsModal() {
  const overlay = document.getElementById("settings-modal-overlay");
  const resultBar = document.getElementById("settings-result");
  if (!overlay) return;
  resultBar.innerHTML = "";
  overlay.classList.add("open");
  Promise.all(SETTINGS_GROUPS.map((g) => fetchJSON(g.endpoint)))
    .then((results) => {
      SETTINGS_GROUPS.forEach((g, i) => {
        g.fields.forEach((f) => {
          const input = document.getElementById(`setting-${f}`);
          if (input) input.value = results[i][f];
        });
      });
    })
    .catch((e) => {
      resultBar.innerHTML = `<span class="neg">Failed to load current settings: ${e.message}</span>`;
    });
}

function closeSettingsModal() {
  document.getElementById("settings-modal-overlay")?.classList.remove("open");
}

function initSettingsModal() {
  const overlay = document.getElementById("settings-modal-overlay");
  const btn = document.getElementById("settings-btn");
  const closeBtn = document.getElementById("settings-modal-close");
  const form = document.getElementById("settings-form");
  if (!overlay || !btn || !form) return;

  btn.addEventListener("click", openSettingsModal);
  closeBtn.addEventListener("click", closeSettingsModal);
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) closeSettingsModal();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeSettingsModal();
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const resultBar = document.getElementById("settings-result");
    resultBar.innerHTML = `<span class="muted">Saving...</span>`;
    try {
      for (const g of SETTINGS_GROUPS) {
        const payload = {};
        for (const f of g.fields) {
          const input = document.getElementById(`setting-${f}`);
          if (input.value !== "") payload[f] = Number(input.value);
        }
        const res = await fetch(g.endpoint, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        const body = await res.json();
        if (!res.ok) throw new Error(body.detail || `Request failed (${res.status})`);
      }
      resultBar.innerHTML = `<span class="pos">Saved -- now live on the running bot.</span>`;
    } catch (err) {
      resultBar.innerHTML = `<span class="neg">${err.message}</span>`;
    }
  });
}

function fmtMoney(n) {
  if (n === null || n === undefined) return "--";
  return n.toLocaleString(undefined, { style: "currency", currency: "USD" });
}

function fmtNum(n, decimals = 2) {
  if (n === null || n === undefined) return "--";
  return Number(n).toFixed(decimals);
}

function fmtWeight(w) {
  // Weights (scoring/weights.yaml) are fractions of 1.0 -- show as a
  // percentage (e.g. 0.15 -> "15%") rather than the raw decimal, since
  // that's what they actually mean and is easier to read/compare at a
  // glance.
  if (w === null || w === undefined) return "--";
  return `${(Number(w) * 100).toFixed(1)}%`;
}

function fmtTime(iso) {
  if (!iso) return "--";
  const d = new Date(iso);
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function pnlClass(n) {
  if (n === null || n === undefined) return "muted";
  return n > 0 ? "pos" : n < 0 ? "neg" : "muted";
}

function emptyRow(colspan, label) {
  return `<tr class="empty-row"><td colspan="${colspan}">${label}</td></tr>`;
}

async function fetchJSON(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${path}: ${res.status}`);
  return res.json();
}

let currentKillSwitchActive = false;

async function refreshStatus() {
  const bar = document.getElementById("status-bar");
  try {
    const s = await fetchJSON("/api/status");
    currentKillSwitchActive = !!s.kill_switch_active;
    const modeBadge = `<span class="badge ${s.trading_mode}">${s.trading_mode}</span>`;
    const killBadge = currentKillSwitchActive
      ? `<button class="badge kill-active kill-switch-btn" id="kill-switch-btn" title="Click to disengage">Kill Switch ON</button>`
      : `<button class="badge kill-inactive kill-switch-btn" id="kill-switch-btn" title="Click to engage">Kill Switch off</button>`;
    bar.innerHTML = `
      <div class="stat"><span class="label">Mode</span><span class="value">${modeBadge}</span></div>
      <div class="stat"><span class="label">Equity</span><span class="value">${fmtMoney(s.equity)}</span></div>
      <div class="stat"><span class="label">Buying Power</span><span class="value">${fmtMoney(s.buying_power)}</span></div>
      <div class="stat"><span class="label">Candidates</span><span class="value">${s.candidate_count}</span></div>
      <div class="stat"><span class="label">Open Positions</span><span class="value">${s.open_position_count}</span></div>
      <div class="stat"><span class="label">Safety</span><span class="value">${killBadge}</span></div>
    `;
    document.getElementById("kill-switch-btn")?.addEventListener("click", openKillSwitchModal);
  } catch (e) {
    bar.innerHTML = `<span class="neg">Failed to load status: ${e.message}</span>`;
  }
}

function openKillSwitchModal() {
  const overlay = document.getElementById("kill-switch-modal-overlay");
  const title = document.getElementById("kill-switch-modal-title");
  const body = document.getElementById("kill-switch-modal-body");
  const confirmBtn = document.getElementById("kill-switch-confirm");
  const resultBar = document.getElementById("kill-switch-result");
  if (!overlay) return;
  resultBar.innerHTML = "";

  if (currentKillSwitchActive) {
    title.textContent = "Disengage Kill Switch?";
    body.textContent = "This resumes normal trading -- the bot can enter new positions again on its usual signals. Open positions (if any) are left exactly as they are.";
    confirmBtn.textContent = "Disengage";
    confirmBtn.className = "";
  } else {
    title.textContent = "Engage Kill Switch?";
    body.textContent = "This immediately blocks all new trades AND force-closes every open position at the current market price. Closing happens within a few seconds, not instantly, and cannot be undone once positions start closing. Are you sure?";
    confirmBtn.textContent = "Engage & Close All Positions";
    confirmBtn.className = "danger";
  }
  overlay.classList.add("open");
}

function closeKillSwitchModal() {
  document.getElementById("kill-switch-modal-overlay")?.classList.remove("open");
}

function initKillSwitchModal() {
  const overlay = document.getElementById("kill-switch-modal-overlay");
  const closeBtn = document.getElementById("kill-switch-modal-close");
  const cancelBtn = document.getElementById("kill-switch-cancel");
  const confirmBtn = document.getElementById("kill-switch-confirm");
  if (!overlay || !confirmBtn) return;

  closeBtn.addEventListener("click", closeKillSwitchModal);
  cancelBtn.addEventListener("click", closeKillSwitchModal);
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) closeKillSwitchModal();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeKillSwitchModal();
  });

  confirmBtn.addEventListener("click", async () => {
    const resultBar = document.getElementById("kill-switch-result");
    const nextActive = !currentKillSwitchActive;
    resultBar.innerHTML = `<span class="muted">${nextActive ? "Engaging and closing positions..." : "Disengaging..."}</span>`;
    try {
      const res = await fetch("/api/kill-switch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ active: nextActive }),
      });
      const body = await res.json();
      if (!res.ok) throw new Error(body.detail || `Request failed (${res.status})`);
      resultBar.innerHTML = `<span class="pos">Done.</span>`;
      await refreshStatus();
      await refreshPositions();
      setTimeout(closeKillSwitchModal, 600);
    } catch (err) {
      resultBar.innerHTML = `<span class="neg">${err.message}</span>`;
    }
  });
}

let selectedCandidateSymbol = null;
let lastCandidateRows = [];

async function refreshCandidates() {
  const body = document.getElementById("candidates-body");
  try {
    const rows = await fetchJSON("/api/candidates");
    lastCandidateRows = rows;
    body.innerHTML = rows.length
      ? rows.map(c => `
        <tr class="candidate-row ${c.symbol === selectedCandidateSymbol ? "selected-row" : ""}" data-symbol="${c.symbol}">
          <td>${c.symbol}</td>
          <td><span class="state-pill state-${c.state}">${c.state.replace("_", " ")}</span></td>
          <td>${fmtNum(c.score, 1)}</td>
          <td>${fmtNum(c.price)}</td>
          <td>${fmtNum(c.resistance_level)}</td>
          <td class="muted">${c.reason || "--"}</td>
          <td class="muted">${fmtTime(c.last_updated_at)}</td>
        </tr>`).join("")
      : emptyRow(7, "No candidates tracked yet");
    return rows;
  } catch (e) {
    body.innerHTML = emptyRow(7, `Failed to load: ${e.message}`);
    lastCandidateRows = [];
    return [];
  }
}

function initCandidateSelection() {
  const body = document.getElementById("candidates-body");
  if (!body) return;
  body.addEventListener("click", (e) => {
    const row = e.target.closest("tr[data-symbol]");
    if (!row) return;
    const symbol = row.dataset.symbol;
    selectedCandidateSymbol = symbol === selectedCandidateSymbol ? null : symbol;
    document.querySelectorAll(".candidate-row").forEach(r => r.classList.toggle("selected-row", r.dataset.symbol === selectedCandidateSymbol));
    refreshScoreBreakdown(lastCandidateRows);
  });
}

async function refreshPositions() {
  const body = document.getElementById("positions-body");
  try {
    const rows = await fetchJSON("/api/positions");
    body.innerHTML = rows.length
      ? rows.map(p => `
        <tr>
          <td>${p.symbol}</td>
          <td>${p.side}</td>
          <td>${fmtNum(p.quantity, 0)}</td>
          <td>${fmtNum(p.avg_entry_price)}</td>
          <td>${fmtNum(p.current_price)}</td>
          <td class="${pnlClass(p.unrealized_pnl)}">${fmtMoney(p.unrealized_pnl)}</td>
          <td>${fmtNum(p.stop_price)}</td>
          <td>${fmtNum(p.target_price)}</td>
        </tr>`).join("")
      : emptyRow(8, "No open positions");
  } catch (e) {
    body.innerHTML = emptyRow(8, `Failed to load: ${e.message}`);
  }
}

async function refreshRiskEvents() {
  const body = document.getElementById("risk-events-body");
  try {
    const rows = await fetchJSON("/api/risk-events?limit=20");
    body.innerHTML = rows.length
      ? rows.map(e => `
        <tr>
          <td class="muted">${fmtTime(e.timestamp)}</td>
          <td>${e.event_type}</td>
          <td>${e.symbol || "--"}</td>
          <td class="muted">${e.reason}</td>
        </tr>`).join("")
      : emptyRow(4, "No risk events");
  } catch (e) {
    body.innerHTML = emptyRow(4, `Failed to load: ${e.message}`);
  }
}

async function refreshPerformance() {
  const bar = document.getElementById("performance-bar");
  try {
    const p = await fetchJSON("/api/performance");
    bar.innerHTML = `
      <div class="stat"><span class="label">Total Trades</span><span class="value">${p.total_trades}</span></div>
      <div class="stat"><span class="label">Win Rate</span><span class="value">${fmtNum(p.win_rate * 100, 1)}%</span></div>
      <div class="stat"><span class="label">Total P&amp;L</span><span class="value ${pnlClass(p.total_pnl)}">${fmtMoney(p.total_pnl)}</span></div>
      <div class="stat"><span class="label">Avg P&amp;L %</span><span class="value ${pnlClass(p.avg_pnl_pct)}">${fmtNum(p.avg_pnl_pct)}%</span></div>
    `;
  } catch (e) {
    bar.innerHTML = `<span class="neg">Failed to load: ${e.message}</span>`;
  }
}

async function refreshTrades() {
  const body = document.getElementById("trades-body");
  try {
    const rows = await fetchJSON("/api/trades?limit=50");
    body.innerHTML = rows.length
      ? rows.map(t => `
        <tr>
          <td class="muted">${fmtTime(t.closed_at)}</td>
          <td>${t.symbol}</td>
          <td>${t.strategy_name}</td>
          <td>${t.side}</td>
          <td>${fmtNum(t.entry_price)}</td>
          <td>${fmtNum(t.exit_price)}</td>
          <td>${fmtNum(t.quantity, 0)}</td>
          <td class="muted">${t.exit_reason.replace("_", " ")}</td>
          <td class="${pnlClass(t.pnl)}">${fmtMoney(t.pnl)}</td>
          <td class="${pnlClass(t.pnl_pct)}">${fmtNum(t.pnl_pct)}%</td>
        </tr>`).join("")
      : emptyRow(10, "No closed trades yet");
  } catch (e) {
    body.innerHTML = emptyRow(10, `Failed to load: ${e.message}`);
  }
}

function componentLabel(name) {
  // "float_turnover_score" -> "Float Turnover"
  return name.replace(/_score$/, "").split("_").map(w => w[0].toUpperCase() + w.slice(1)).join(" ");
}

let cachedMisWeights = null;

async function loadMisWeightsOnce() {
  if (cachedMisWeights) return cachedMisWeights;
  try {
    cachedMisWeights = await fetchJSON("/api/mis-weights");
  } catch (e) {
    cachedMisWeights = { weights_version: null, weights: {} };
  }
  return cachedMisWeights;
}

function setScoreBreakdownHeaders(mode) {
  const labels = mode === "live"
    ? { raw: "Raw Score", contribution: "Weighted Contribution", pct: "% of Score" }
    : { raw: "Avg Raw Score", contribution: "Avg Weighted Contribution", pct: "% of Avg Score" };
  document.getElementById("score-breakdown-th-raw").textContent = labels.raw;
  document.getElementById("score-breakdown-th-contribution").textContent = labels.contribution;
  document.getElementById("score-breakdown-th-pct").textContent = labels.pct;
  document.getElementById("score-breakdown-th-samples").textContent = "Samples";
}

function clearCandidateSelection() {
  selectedCandidateSymbol = null;
  document.querySelectorAll(".candidate-row.selected-row").forEach(r => r.classList.remove("selected-row"));
  refreshScoreBreakdown(lastCandidateRows);
}

async function renderAggregateScoreBreakdown() {
  const meta = document.getElementById("score-breakdown-meta");
  const body = document.getElementById("score-breakdown-body");
  setScoreBreakdownHeaders("aggregate");
  try {
    const data = await fetchJSON("/api/score-breakdown");
    meta.innerHTML = `
      <div class="stat"><span class="label">Showing</span><span class="value">All candidates (historical average)</span></div>
      <div class="stat"><span class="label">Weights Version</span><span class="value">${data.weights_version || "--"}</span></div>
      <div class="stat"><span class="label">Sample Size</span><span class="value">${data.sample_size}</span></div>
    `;
    body.innerHTML = data.components.length
      ? data.components.map(c => `
        <tr>
          <td>${componentLabel(c.name)}</td>
          <td>${fmtWeight(c.weight)}</td>
          <td>${fmtNum(c.avg_raw_score, 1)}</td>
          <td>${fmtNum(c.avg_weighted_contribution, 2)}</td>
          <td>${fmtNum(c.pct_of_avg_score, 1)}%</td>
          <td class="muted">${c.sample_size}</td>
        </tr>`).join("")
      : emptyRow(6, "No momentum scores recorded yet -- this fills in once candidates start being watched.");
  } catch (e) {
    body.innerHTML = emptyRow(6, `Failed to load: ${e.message}`);
  }
}

async function renderCandidateScoreBreakdown(candidateRows) {
  const meta = document.getElementById("score-breakdown-meta");
  const body = document.getElementById("score-breakdown-body");
  setScoreBreakdownHeaders("live");
  const candidate = (candidateRows || []).find(c => c.symbol === selectedCandidateSymbol);

  if (!candidate) {
    meta.innerHTML = `
      <div class="stat"><span class="label">Showing</span><span class="value">${selectedCandidateSymbol} <button class="link-btn" id="clear-candidate-breakdown">(show all candidates)</button></span></div>
    `;
    body.innerHTML = emptyRow(6, `${selectedCandidateSymbol} is no longer tracked.`);
    document.getElementById("clear-candidate-breakdown")?.addEventListener("click", clearCandidateSelection);
    return;
  }

  if (!candidate.components) {
    meta.innerHTML = `
      <div class="stat"><span class="label">Showing</span><span class="value">${candidate.symbol} <button class="link-btn" id="clear-candidate-breakdown">(show all candidates)</button></span></div>
    `;
    body.innerHTML = emptyRow(6, `${candidate.symbol} hasn't been scored yet -- it fills in on this candidate's first tick.`);
    document.getElementById("clear-candidate-breakdown")?.addEventListener("click", clearCandidateSelection);
    return;
  }

  const weightsData = await loadMisWeightsOnce();
  const weights = weightsData.weights || {};
  const rows = Object.entries(candidate.components).map(([name, value]) => ({
    name, value, weight: weights[name] || 0, contribution: value * (weights[name] || 0),
  }));
  const totalContribution = rows.reduce((sum, r) => sum + r.contribution, 0) || 1;
  rows.sort((a, b) => b.contribution - a.contribution);

  meta.innerHTML = `
    <div class="stat"><span class="label">Showing</span><span class="value">${candidate.symbol} <button class="link-btn" id="clear-candidate-breakdown">(show all candidates)</button></span></div>
    <div class="stat"><span class="label">Live Score</span><span class="value">${fmtNum(candidate.score, 1)}</span></div>
    <div class="stat"><span class="label">Weights Version</span><span class="value">${candidate.score_weights_version || weightsData.weights_version || "--"}</span></div>
  `;
  body.innerHTML = rows.map(r => `
    <tr>
      <td>${componentLabel(r.name)}</td>
      <td>${fmtWeight(r.weight)}</td>
      <td>${fmtNum(r.value, 1)}</td>
      <td>${fmtNum(r.contribution, 2)}</td>
      <td>${fmtNum(r.contribution / totalContribution * 100, 1)}%</td>
      <td class="muted">1 (live)</td>
    </tr>`).join("");
  document.getElementById("clear-candidate-breakdown")?.addEventListener("click", clearCandidateSelection);
}

async function refreshScoreBreakdown(candidateRows) {
  if (selectedCandidateSymbol) {
    await renderCandidateScoreBreakdown(candidateRows);
  } else {
    await renderAggregateScoreBreakdown();
  }
}

function topComponents(components, n = 3) {
  return Object.entries(components)
    .filter(([name]) => name.endsWith("_score"))
    .sort((a, b) => b[1] - a[1])
    .slice(0, n)
    .map(([name, value]) => `${componentLabel(name)}: ${fmtNum(value, 1)}`)
    .join(", ");
}

async function loadScoreHistory(symbol) {
  const body = document.getElementById("score-history-body");
  if (!symbol) return;
  body.innerHTML = emptyRow(4, "Loading...");
  try {
    const rows = await fetchJSON(`/api/score-history?symbol=${encodeURIComponent(symbol)}&limit=50`);
    body.innerHTML = rows.length
      ? rows.map(r => `
        <tr>
          <td class="muted">${fmtTime(r.timestamp)}</td>
          <td>${fmtNum(r.score, 1)}</td>
          <td class="muted">${r.weights_version}</td>
          <td class="muted">${topComponents(r.components)}</td>
        </tr>`).join("")
      : emptyRow(4, `No score history recorded yet for ${symbol.toUpperCase()}`);
  } catch (e) {
    body.innerHTML = emptyRow(4, `Failed to load: ${e.message}`);
  }
}

async function scanAndAddTicker(symbol) {
  const resultBar = document.getElementById("scan-result");
  if (!symbol) return;
  const upperSymbol = symbol.toUpperCase();
  resultBar.innerHTML = `<span class="muted">Scanning ${upperSymbol}...</span>`;
  try {
    const res = await fetch(`/api/scan-symbol?symbol=${encodeURIComponent(symbol)}`, { method: "POST" });
    if (!res.ok) throw new Error(`${res.status}`);
    const result = await res.json();
    const statePill = `<span class="state-pill state-${result.state}">${result.state.replace("_", " ")}</span>`;
    const status = result.added ? "Newly added" : result.already_tracked ? "Already tracked" : "Not added";
    resultBar.innerHTML = `
      <div class="stat"><span class="label">Symbol</span><span class="value">${result.symbol}</span></div>
      <div class="stat"><span class="label">State</span><span class="value">${statePill}</span></div>
      <div class="stat"><span class="label">Status</span><span class="value">${status}</span></div>
      <div class="stat"><span class="label">Reason</span><span class="value muted">${result.reason || "--"}</span></div>
    `;
    if (result.added) {
      // Snappier feedback than waiting for the next 5s poll to show the
      // new candidate in the table above.
      const rows = await refreshCandidates();
      await refreshScoreBreakdown(rows);
    }
    await loadScoreHistory(symbol);
  } catch (e) {
    resultBar.innerHTML = `<span class="neg">Failed to scan ${upperSymbol}: ${e.message}</span>`;
  }
}

function initScoreHistoryForm() {
  const form = document.getElementById("score-history-form");
  const input = document.getElementById("score-history-symbol");
  if (!form) return;
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    scanAndAddTicker(input.value.trim());
  });
}

async function refreshAll() {
  // refreshCandidates runs first (not in the Promise.all below) because
  // refreshScoreBreakdown needs its freshly fetched rows when a candidate
  // is selected -- see renderCandidateScoreBreakdown.
  const candidateRows = await refreshCandidates();
  await Promise.all([
    refreshStatus(),
    refreshPositions(),
    refreshRiskEvents(),
    refreshPerformance(),
    refreshTrades(),
    refreshScoreBreakdown(candidateRows),
  ]);
}

initInfoModal();
initSettingsModal();
initKillSwitchModal();
initScoreHistoryForm();
initCandidateSelection();
refreshAll();
setInterval(refreshAll, REFRESH_MS);
