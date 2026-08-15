// Hamburger menu (top-right of the dashboard header) for switching
// between a user's bots (2026-08-15 multi-bot framework). Separate from
// app.js's own polling/rendering logic since this is a one-time-on-load
// fetch plus a simple open/close toggle, not part of the dashboard's
// periodic refresh cycle.

async function loadBotNav() {
  const dropdown = document.getElementById("bot-nav-dropdown");
  if (!dropdown) return;

  let bots = [];
  try {
    const res = await fetch("/api/bots");
    if (res.ok) bots = await res.json();
  } catch (e) {
    // Leave the dropdown showing just the static "Coming Soon" entry below
    // if /api/bots is unreachable -- a broken nav fetch shouldn't block
    // the rest of the dashboard from loading.
  }

  const items = bots.map((bot) => {
    // Every real bot links to /app today (there's only one page) -- marked
    // active since it's necessarily the page you're already on.
    return `<a class="bot-nav-item active" href="/app">${bot.name}</a>`;
  });
  items.push('<span class="bot-nav-item disabled">Coming Soon<span class="bot-nav-badge">Soon</span></span>');
  dropdown.innerHTML = items.join("");
}

function initBotNav() {
  const btn = document.getElementById("bot-nav-btn");
  const dropdown = document.getElementById("bot-nav-dropdown");
  if (!btn || !dropdown) return;

  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    const isOpen = dropdown.classList.toggle("open");
    btn.setAttribute("aria-expanded", isOpen ? "true" : "false");
  });

  document.addEventListener("click", (e) => {
    if (!dropdown.classList.contains("open")) return;
    if (dropdown.contains(e.target) || e.target === btn) return;
    dropdown.classList.remove("open");
    btn.setAttribute("aria-expanded", "false");
  });
}

initBotNav();
loadBotNav();
