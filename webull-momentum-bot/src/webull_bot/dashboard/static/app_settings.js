// /app/settings -- connect/test/disconnect a user's own Webull broker
// credential and toggle self-serve live trading. Separate from app.js
// (the main dashboard's polling script) since this page has its own,
// much smaller, non-polling data flow.

async function postJSON(path, body, method) {
  const res = await fetch(path, {
    method: method || "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (res.status === 401) {
    window.location.href = "/login";
    throw new Error("401");
  }
  const data = await res.json().catch(() => ({}));
  return { ok: res.ok, status: res.status, data };
}

function showResult(el, message, isError) {
  el.textContent = message;
  el.className = "status-bar" + (isError ? " auth-error" : "");
}

function renderCredentialStatus(cred) {
  const statusEl = document.getElementById("credential-status");
  const toggle = document.getElementById("live-trading-toggle");
  if (!cred.connected) {
    statusEl.innerHTML = `<div class="stat"><span class="label">Status</span><span class="value muted">Not connected</span></div>`;
    toggle.disabled = true;
    toggle.checked = false;
    return;
  }
  const verified = !!cred.last_verified_at;
  statusEl.innerHTML = `
    <div class="stat"><span class="label">App Key</span><span class="value">${cred.masked_app_key ?? "?"}</span></div>
    <div class="stat"><span class="label">Trading Mode</span><span class="value">${cred.trading_mode}</span></div>
    <div class="stat"><span class="label">Endpoint</span><span class="value">${cred.base_url}</span></div>
    <div class="stat"><span class="label">Verified</span><span class="value ${verified ? "pos" : "neg"}">${verified ? "Yes" : "No"}</span></div>
    ${cred.last_verify_error ? `<div class="stat"><span class="label">Last Error</span><span class="value neg">${cred.last_verify_error}</span></div>` : ""}
  `;
  document.getElementById("cred-trading-mode").value = cred.trading_mode;
  toggle.disabled = !verified;
  toggle.checked = !!cred.live_trading_enabled;
}

async function refreshCredential() {
  const { ok, data } = await postJSON("/api/broker-credential", undefined, "GET");
  if (ok) renderCredentialStatus(data);
  return data;
}

function initCredentialForm() {
  const form = document.getElementById("credential-form");
  const resultEl = document.getElementById("credential-result");

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    showResult(resultEl, "Verifying...", false);
    const body = {
      app_key: document.getElementById("cred-app-key").value,
      app_secret: document.getElementById("cred-app-secret").value,
      account_id: document.getElementById("cred-account-id").value,
      trading_mode: document.getElementById("cred-trading-mode").value,
    };
    const { ok, data } = await postJSON("/api/broker-credential", body, "POST");
    if (ok) {
      showResult(resultEl, "Connected and verified.", false);
      document.getElementById("cred-app-key").value = "";
      document.getElementById("cred-app-secret").value = "";
      document.getElementById("cred-account-id").value = "";
      renderCredentialStatus(data);
    } else {
      showResult(resultEl, data.detail || "Could not verify these credentials.", true);
    }
  });

  document.getElementById("credential-test-btn").addEventListener("click", async () => {
    showResult(resultEl, "Testing...", false);
    const { ok, data } = await postJSON("/api/broker-credential/test", undefined, "POST");
    if (ok) {
      showResult(resultEl, "Connection verified.", false);
      renderCredentialStatus(data);
    } else {
      showResult(resultEl, data.detail || "Test failed.", true);
    }
  });

  document.getElementById("credential-delete-btn").addEventListener("click", async () => {
    if (!confirm("Disconnect your broker account? Your bot will stop trading until you reconnect.")) return;
    showResult(resultEl, "Disconnecting...", false);
    const { ok, data } = await postJSON("/api/broker-credential", undefined, "DELETE");
    showResult(resultEl, ok ? "Disconnected." : (data.detail || "Could not disconnect."), !ok);
    renderCredentialStatus({ connected: false });
  });
}

function initLiveTradingToggle() {
  const toggle = document.getElementById("live-trading-toggle");
  const overlay = document.getElementById("live-trading-confirm-overlay");
  const resultEl = document.getElementById("live-trading-result");
  const confirmResultEl = document.getElementById("live-trading-confirm-result");

  const openConfirm = () => { confirmResultEl.textContent = ""; overlay.classList.add("open"); };
  const closeConfirm = () => overlay.classList.remove("open");

  toggle.addEventListener("change", async () => {
    if (!toggle.checked) {
      const { ok, data } = await postJSON("/api/broker-credential/live-trading", { enabled: false }, "POST");
      showResult(resultEl, ok ? "Live trading disabled." : (data.detail || "Could not disable live trading."), !ok);
      return;
    }
    toggle.checked = false; // stays off until explicitly confirmed in the modal
    openConfirm();
  });

  document.getElementById("live-trading-confirm-cancel").addEventListener("click", closeConfirm);
  document.getElementById("live-trading-confirm-close").addEventListener("click", closeConfirm);
  overlay.addEventListener("click", (e) => { if (e.target === overlay) closeConfirm(); });

  document.getElementById("live-trading-confirm-yes").addEventListener("click", async () => {
    const { ok, data } = await postJSON("/api/broker-credential/live-trading", { enabled: true, confirm: true }, "POST");
    if (ok) {
      toggle.checked = true;
      showResult(resultEl, "Live trading enabled.", false);
      closeConfirm();
    } else {
      confirmResultEl.textContent = data.detail || "Could not enable live trading.";
      confirmResultEl.className = "status-bar auth-error";
    }
  });
}

document.getElementById("logout-link")?.addEventListener("click", async (e) => {
  e.preventDefault();
  await fetch("/api/auth/logout", { method: "POST" }).catch(() => {});
  window.location.href = "/login";
});

initCredentialForm();
initLiveTradingToggle();
refreshCredential();
