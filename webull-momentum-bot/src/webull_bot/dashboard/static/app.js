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
      "2. Static levels from volume-profile analysis done at discovery -- price zones where a lot of historical volume traded (\"high-volume nodes\"), which tend to act as real support/resistance\n\n" +
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
    title: "Score History Lookup",
    body:
      "Look up a specific symbol's recorded Momentum Ignition Score history -- one row per tick it was scored, most recent first, with its top 3 highest-contributing raw sub-scores that tick. Use this to spot-check that a real mover you remember actually scored the way you'd expect under the current weights (e.g. a low-float stock trading heavy volume should show high relative-volume/float-turnover sub-scores).",
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
    const btn = e.target.closest(".info-btn");
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

async function refreshStatus() {
  const bar = document.getElementById("status-bar");
  try {
    const s = await fetchJSON("/api/status");
    const modeBadge = `<span class="badge ${s.trading_mode}">${s.trading_mode}</span>`;
    const killBadge = s.kill_switch_active
      ? `<span class="badge kill-active">Kill Switch ON</span>`
      : `<span class="badge kill-inactive">Kill Switch off</span>`;
    bar.innerHTML = `
      <div class="stat"><span class="label">Mode</span><span class="value">${modeBadge}</span></div>
      <div class="stat"><span class="label">Equity</span><span class="value">${fmtMoney(s.equity)}</span></div>
      <div class="stat"><span class="label">Buying Power</span><span class="value">${fmtMoney(s.buying_power)}</span></div>
      <div class="stat"><span class="label">Candidates</span><span class="value">${s.candidate_count}</span></div>
      <div class="stat"><span class="label">Open Positions</span><span class="value">${s.open_position_count}</span></div>
      <div class="stat"><span class="label">Safety</span><span class="value">${killBadge}</span></div>
    `;
  } catch (e) {
    bar.innerHTML = `<span class="neg">Failed to load status: ${e.message}</span>`;
  }
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

function initScoreHistoryForm() {
  const form = document.getElementById("score-history-form");
  const input = document.getElementById("score-history-symbol");
  if (!form) return;
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    loadScoreHistory(input.value.trim());
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
initScoreHistoryForm();
initCandidateSelection();
refreshAll();
setInterval(refreshAll, REFRESH_MS);
