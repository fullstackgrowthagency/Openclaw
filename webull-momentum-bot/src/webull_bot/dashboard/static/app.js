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
      "The Momentum Ignition Score (MIS), 0-100 -- a weighted blend of 8 components recomputed on every tick:\n\n" +
      "• Float score -- lower free float scores higher\n" +
      "• Float velocity -- % of float traded in the last 5 minutes\n" +
      "• Relative volume -- today's volume vs. typical for this time of day\n" +
      "• Volume acceleration -- is volume ramping up, not just high\n" +
      "• Price acceleration -- how fast price itself is moving\n" +
      "• Breakout proximity -- distance to resistance / high of day\n" +
      "• Trend quality -- position relative to VWAP\n" +
      "• Liquidity -- spread tightness + dollar volume\n\n" +
      "Crossing 40 promotes WATCHING → HEATING_UP; crossing 70 promotes to ARMED. The weights are unvalidated starting values, not backtested.",
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

async function refreshCandidates() {
  const body = document.getElementById("candidates-body");
  try {
    const rows = await fetchJSON("/api/candidates");
    body.innerHTML = rows.length
      ? rows.map(c => `
        <tr>
          <td>${c.symbol}</td>
          <td><span class="state-pill state-${c.state}">${c.state.replace("_", " ")}</span></td>
          <td>${fmtNum(c.score, 1)}</td>
          <td>${fmtNum(c.price)}</td>
          <td>${fmtNum(c.resistance_level)}</td>
          <td class="muted">${c.reason || "--"}</td>
          <td class="muted">${fmtTime(c.last_updated_at)}</td>
        </tr>`).join("")
      : emptyRow(7, "No candidates tracked yet");
  } catch (e) {
    body.innerHTML = emptyRow(7, `Failed to load: ${e.message}`);
  }
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

async function refreshAll() {
  await Promise.all([
    refreshStatus(),
    refreshCandidates(),
    refreshPositions(),
    refreshRiskEvents(),
    refreshPerformance(),
    refreshTrades(),
  ]);
}

initInfoModal();
refreshAll();
setInterval(refreshAll, REFRESH_MS);
