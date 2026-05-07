import path from 'node:path';
import { writeFile } from 'node:fs/promises';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { ensureDir, writeJson } from '../../lib/fs.js';

const execFileAsync = promisify(execFile);

function normalizeString(value) {
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function toSlug(value) {
  return String(value || 'social-creative')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 60) || 'social-creative';
}

function tokenize(text) {
  return String(text || '')
    .toLowerCase()
    .replace(/https?:\/\/\S+/g, ' ')
    .replace(/[#@][\p{L}\p{N}_-]+/gu, ' ')
    .replace(/[^\p{L}\p{N}\s-]/gu, ' ')
    .split(/\s+/)
    .map((part) => part.trim())
    .filter(Boolean);
}

function hasAny(tokens, candidates = []) {
  return candidates.some((candidate) => tokens.includes(candidate));
}

function uniqueCompact(values) {
  return [...new Set(values.map((value) => normalizeString(value)).filter(Boolean))];
}

function extractBrand(summary, explicitBusinessName, postBusinessName) {
  const explicit = normalizeString(explicitBusinessName) || normalizeString(postBusinessName);
  if (explicit) return explicit;

  const text = normalizeString(summary) || '';
  const patterns = [
    /([A-Z][A-Za-z0-9&]+(?:\s+[A-Z][A-Za-z0-9&]+){1,5})\s+(?:helps|builds|creates|delivers|offers|improves|simplifies)\b/,
    /([A-Z][A-Za-z0-9&]+(?:\s+[A-Z][A-Za-z0-9&]+){1,5})\s+(?:Agency|Studio|Consulting|Solutions|Systems)\b/
  ];

  for (const pattern of patterns) {
    const match = text.match(pattern);
    if (match?.[1]) return match[1].trim();
    if (match?.[0]) return match[0].trim();
  }

  return 'Growth Operations';
}

function inferCreativeTheme(summary) {
  const tokens = tokenize(summary);
  const mentionsAutomation = hasAny(tokens, ['automation', 'automations', 'automated', 'workflow', 'workflows', 'system', 'systems', 'process', 'processes']);
  const mentionsOperations = hasAny(tokens, ['operations', 'operational', 'handoffs', 'delivery', 'crm']);
  const mentionsConsulting = hasAny(tokens, ['consulting', 'consultant', 'strategy', 'strategic', 'advisory', 'advisor']);
  const mentionsAnalytics = hasAny(tokens, ['analytics', 'attribution', 'reporting', 'tracking', 'data', 'dashboard', 'metrics']);
  const mentionsRevenue = hasAny(tokens, ['sales', 'lead', 'leads', 'followup', 'follow', 'pipeline', 'conversion', 'conversions']);

  if ((mentionsConsulting && mentionsAutomation) || (mentionsConsulting && mentionsOperations)) {
    return {
      id: 'consulting-automation',
      kicker: 'BUSINESS CONSULTING • OPERATIONS AUTOMATION',
      headline: 'Scale with\ncleaner systems',
      subhead: 'Build the operating layer behind faster decisions and smoother execution.',
      chips: ['Consulting', 'Automation', 'Visibility'],
      nodes: ['Strategy', 'CRM', 'Automation', 'Reporting'],
      palette: {
        bgA: '#06111f',
        bgB: '#0c2441',
        bgC: '#0f766e',
        accent: '#7dd3fc',
        accentStrong: '#38bdf8',
        accentAlt: '#22c55e',
        panel: 'rgba(8, 18, 37, 0.68)'
      }
    };
  }

  if (mentionsAutomation || mentionsOperations) {
    return {
      id: 'operations-automation',
      kicker: 'OPERATIONS AUTOMATION',
      headline: 'Systems that\nmove faster',
      subhead: 'Reduce handoff friction and keep work flowing from lead to delivery.',
      chips: ['Workflows', 'Handoffs', 'Ops clarity'],
      nodes: ['Lead Flow', 'CRM', 'Ops Engine', 'Delivery'],
      palette: {
        bgA: '#081225',
        bgB: '#123056',
        bgC: '#0f766e',
        accent: '#67e8f9',
        accentStrong: '#22d3ee',
        accentAlt: '#60a5fa',
        panel: 'rgba(8, 18, 37, 0.68)'
      }
    };
  }

  if (mentionsAnalytics) {
    return {
      id: 'analytics-visibility',
      kicker: 'REPORTING • ATTRIBUTION',
      headline: 'See what is\nactually working',
      subhead: 'Make decisions from cleaner numbers, clearer tracking, and better visibility.',
      chips: ['Analytics', 'Attribution', 'Dashboards'],
      nodes: ['Sources', 'Tracking', 'Attribution', 'Insights'],
      palette: {
        bgA: '#07111e',
        bgB: '#172554',
        bgC: '#1d4ed8',
        accent: '#93c5fd',
        accentStrong: '#60a5fa',
        accentAlt: '#a78bfa',
        panel: 'rgba(7, 17, 30, 0.68)'
      }
    };
  }

  if (mentionsRevenue) {
    return {
      id: 'revenue-operations',
      kicker: 'REVENUE OPERATIONS',
      headline: 'Follow-up that\nnever stalls',
      subhead: 'Connect lead capture, response, and pipeline movement into one system.',
      chips: ['Leads', 'Follow-up', 'Pipelines'],
      nodes: ['Capture', 'Nurture', 'Pipeline', 'Close'],
      palette: {
        bgA: '#12090f',
        bgB: '#3b0f2f',
        bgC: '#7c2d12',
        accent: '#f9a8d4',
        accentStrong: '#fb7185',
        accentAlt: '#f59e0b',
        panel: 'rgba(26, 10, 18, 0.68)'
      }
    };
  }

  return {
    id: 'growth-systems',
    kicker: 'BUSINESS SYSTEMS',
    headline: 'Build growth\nthat holds up',
    subhead: 'Turn scattered work into a system your team can actually trust.',
    chips: ['Clarity', 'Systems', 'Execution'],
    nodes: ['Planning', 'Execution', 'Visibility', 'Growth'],
    palette: {
      bgA: '#0b1324',
      bgB: '#1e293b',
      bgC: '#155e75',
      accent: '#a5f3fc',
      accentStrong: '#22d3ee',
      accentAlt: '#818cf8',
      panel: 'rgba(11, 19, 36, 0.68)'
    }
  };
}

function buildCreativeModel({ locationId, post, businessName } = {}) {
  const summary = normalizeString(post?.summary) || normalizeString(post?.text) || '';
  const theme = inferCreativeTheme(summary);
  const brand = extractBrand(summary, businessName, post?.businessName);

  return {
    locationId: locationId || null,
    brand,
    summary,
    themeId: theme.id,
    kicker: theme.kicker,
    headline: theme.headline,
    subhead: theme.subhead,
    chips: uniqueCompact(theme.chips).slice(0, 3),
    nodes: uniqueCompact(theme.nodes).slice(0, 4),
    palette: theme.palette
  };
}

function renderChip(chip) {
  return `<div class="chip">${escapeHtml(chip)}</div>`;
}

function renderNode(label, className) {
  return `<div class="node ${escapeHtml(className)}"><span>${escapeHtml(label)}</span></div>`;
}

function renderHtml(model) {
  const chips = model.chips.map(renderChip).join('\n');
  const [nodeA, nodeB, nodeC, nodeD] = [
    model.nodes[0] || 'Strategy',
    model.nodes[1] || 'CRM',
    model.nodes[2] || 'Automation',
    model.nodes[3] || 'Reporting'
  ];

  return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Social creative</title>
  <style>
    :root {
      --bg-a: ${model.palette.bgA};
      --bg-b: ${model.palette.bgB};
      --bg-c: ${model.palette.bgC};
      --accent: ${model.palette.accent};
      --accent-strong: ${model.palette.accentStrong};
      --accent-alt: ${model.palette.accentAlt};
      --panel: ${model.palette.panel};
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Inter, Arial, sans-serif;
      background: var(--bg-a);
    }
    .canvas {
      width: 1080px;
      height: 1080px;
      position: relative;
      overflow: hidden;
      color: white;
      background:
        radial-gradient(circle at 16% 18%, color-mix(in srgb, var(--accent) 28%, transparent), transparent 28%),
        radial-gradient(circle at 78% 18%, color-mix(in srgb, var(--accent-alt) 26%, transparent), transparent 24%),
        radial-gradient(circle at 78% 76%, color-mix(in srgb, var(--accent-strong) 18%, transparent), transparent 32%),
        linear-gradient(140deg, var(--bg-a) 0%, var(--bg-b) 52%, var(--bg-c) 100%);
    }
    .grid {
      position: absolute;
      inset: 0;
      background-image:
        linear-gradient(rgba(255,255,255,0.05) 1px, transparent 1px),
        linear-gradient(90deg, rgba(255,255,255,0.05) 1px, transparent 1px);
      background-size: 54px 54px;
      mask-image: linear-gradient(180deg, rgba(0,0,0,0.35), transparent 82%);
      opacity: 0.42;
    }
    .orb {
      position: absolute;
      border-radius: 999px;
      filter: blur(30px);
      opacity: 0.85;
    }
    .orb.one { width: 260px; height: 260px; left: 42px; top: 94px; background: color-mix(in srgb, var(--accent) 38%, transparent); }
    .orb.two { width: 320px; height: 320px; right: 38px; top: 120px; background: color-mix(in srgb, var(--accent-alt) 36%, transparent); }
    .orb.three { width: 360px; height: 360px; right: 84px; bottom: 64px; background: color-mix(in srgb, var(--accent-strong) 18%, transparent); }
    .frame {
      position: absolute;
      inset: 42px;
      border-radius: 36px;
      background: linear-gradient(180deg, rgba(255,255,255,0.08), rgba(255,255,255,0.03));
      border: 1px solid rgba(255,255,255,0.14);
      box-shadow: 0 30px 90px rgba(0,0,0,0.30);
      backdrop-filter: blur(16px);
      overflow: hidden;
    }
    .content {
      position: absolute;
      inset: 0;
      display: grid;
      grid-template-columns: 1.04fr 0.96fr;
      padding: 62px;
      gap: 24px;
    }
    .left {
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      min-width: 0;
    }
    .kicker {
      display: inline-flex;
      align-items: center;
      gap: 10px;
      padding: 10px 16px;
      border-radius: 999px;
      background: rgba(255,255,255,0.08);
      border: 1px solid rgba(255,255,255,0.12);
      color: var(--accent);
      font-size: 18px;
      font-weight: 800;
      letter-spacing: 0.14em;
      width: fit-content;
      max-width: 100%;
    }
    .headline {
      margin: 26px 0 18px;
      font-size: 92px;
      line-height: 0.94;
      font-weight: 900;
      letter-spacing: -0.04em;
      white-space: pre-line;
      max-width: 500px;
    }
    .subhead {
      max-width: 460px;
      font-size: 28px;
      line-height: 1.34;
      color: rgba(255,255,255,0.82);
    }
    .chips {
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      margin-top: 30px;
    }
    .chip {
      padding: 12px 16px;
      border-radius: 999px;
      font-size: 17px;
      font-weight: 700;
      background: rgba(255,255,255,0.10);
      border: 1px solid rgba(255,255,255,0.12);
      color: rgba(255,255,255,0.92);
    }
    .brand-bar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      margin-top: 26px;
    }
    .brand {
      font-size: 30px;
      font-weight: 800;
      letter-spacing: 0.01em;
      max-width: 360px;
    }
    .signal {
      display: inline-flex;
      align-items: center;
      gap: 10px;
      color: rgba(255,255,255,0.72);
      font-size: 16px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.12em;
    }
    .signal::before {
      content: '';
      width: 10px;
      height: 10px;
      border-radius: 999px;
      background: var(--accent-strong);
      box-shadow: 0 0 16px var(--accent-strong);
    }
    .right {
      position: relative;
      min-width: 0;
    }
    .hero {
      position: absolute;
      inset: 18px 0 0 8px;
      border-radius: 32px;
      background: linear-gradient(180deg, rgba(255,255,255,0.08), rgba(255,255,255,0.04));
      border: 1px solid rgba(255,255,255,0.12);
      overflow: hidden;
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.12);
    }
    .hero::before {
      content: '';
      position: absolute;
      inset: 0;
      background:
        linear-gradient(135deg, rgba(255,255,255,0.04), transparent 28%),
        radial-gradient(circle at 75% 20%, color-mix(in srgb, var(--accent-alt) 30%, transparent), transparent 24%),
        radial-gradient(circle at 34% 76%, color-mix(in srgb, var(--accent-strong) 20%, transparent), transparent 30%);
      opacity: 0.95;
    }
    .dashboard {
      position: absolute;
      inset: 34px;
    }
    .panel-top {
      display: grid;
      grid-template-columns: 1.08fr 0.92fr;
      gap: 18px;
      margin-bottom: 18px;
    }
    .card {
      position: relative;
      border-radius: 24px;
      padding: 22px;
      background: var(--panel);
      border: 1px solid rgba(255,255,255,0.12);
      box-shadow: 0 16px 36px rgba(0,0,0,0.18);
      backdrop-filter: blur(12px);
    }
    .card-label {
      font-size: 13px;
      font-weight: 800;
      letter-spacing: 0.14em;
      text-transform: uppercase;
      color: rgba(255,255,255,0.62);
      margin-bottom: 10px;
    }
    .flow {
      position: relative;
      height: 308px;
    }
    .node {
      position: absolute;
      width: 168px;
      height: 96px;
      border-radius: 22px;
      padding: 18px;
      background: rgba(255,255,255,0.09);
      border: 1px solid rgba(255,255,255,0.14);
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.08);
      font-size: 24px;
      font-weight: 800;
      display: flex;
      align-items: flex-end;
      color: rgba(255,255,255,0.94);
    }
    .node::before {
      content: '';
      position: absolute;
      left: 18px;
      top: 18px;
      width: 14px;
      height: 14px;
      border-radius: 999px;
      background: var(--accent);
      box-shadow: 0 0 14px var(--accent);
    }
    .node.a { left: 0; top: 0; }
    .node.b { right: 0; top: 18px; }
    .node.c { left: 28px; bottom: 22px; }
    .node.d { right: 16px; bottom: 0; }
    .connector {
      position: absolute;
      inset: 0;
      pointer-events: none;
    }
    .connector line {
      stroke: rgba(255,255,255,0.28);
      stroke-width: 4;
      stroke-linecap: round;
    }
    .connector circle {
      fill: var(--accent-strong);
      opacity: 0.9;
    }
    .mini-chart {
      height: 150px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
    }
    .mini-bars {
      display: flex;
      align-items: end;
      gap: 12px;
      height: 96px;
      margin-top: 8px;
    }
    .mini-bars span {
      flex: 1;
      border-radius: 12px 12px 6px 6px;
      background: linear-gradient(180deg, var(--accent), rgba(255,255,255,0.08));
      opacity: 0.92;
    }
    .mini-bars span:nth-child(1) { height: 38%; }
    .mini-bars span:nth-child(2) { height: 54%; }
    .mini-bars span:nth-child(3) { height: 66%; }
    .mini-bars span:nth-child(4) { height: 82%; }
    .mini-bars span:nth-child(5) { height: 100%; background: linear-gradient(180deg, var(--accent-alt), rgba(255,255,255,0.10)); }
    .metric-strip {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 12px;
      margin-top: 18px;
    }
    .metric {
      border-radius: 18px;
      padding: 14px 14px 16px;
      background: rgba(255,255,255,0.08);
      border: 1px solid rgba(255,255,255,0.10);
    }
    .metric-key {
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.12em;
      color: rgba(255,255,255,0.56);
      margin-bottom: 8px;
    }
    .metric-val {
      font-size: 18px;
      font-weight: 800;
      color: rgba(255,255,255,0.92);
    }
    .pipeline {
      height: 252px;
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 12px;
      align-items: stretch;
      margin-top: 18px;
    }
    .lane {
      border-radius: 18px;
      padding: 14px 12px;
      background: rgba(255,255,255,0.06);
      border: 1px solid rgba(255,255,255,0.10);
    }
    .lane-title {
      font-size: 12px;
      font-weight: 800;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      color: rgba(255,255,255,0.58);
      margin-bottom: 12px;
    }
    .lane-card {
      border-radius: 14px;
      padding: 12px;
      background: rgba(255,255,255,0.10);
      border: 1px solid rgba(255,255,255,0.08);
      min-height: 66px;
      display: flex;
      align-items: end;
      font-size: 15px;
      font-weight: 700;
      color: rgba(255,255,255,0.92);
    }
  </style>
</head>
<body>
  <main class="canvas">
    <div class="grid"></div>
    <div class="orb one"></div>
    <div class="orb two"></div>
    <div class="orb three"></div>
    <section class="frame">
      <div class="content">
        <section class="left">
          <div>
            <div class="kicker">${escapeHtml(model.kicker)}</div>
            <h1 class="headline">${escapeHtml(model.headline)}</h1>
            <div class="subhead">${escapeHtml(model.subhead)}</div>
            <div class="chips">${chips}</div>
          </div>
          <div class="brand-bar">
            <div class="brand">${escapeHtml(model.brand)}</div>
            <div class="signal">system-led growth</div>
          </div>
        </section>
        <section class="right">
          <div class="hero">
            <div class="dashboard">
              <div class="panel-top">
                <div class="card flow">
                  <div class="card-label">Connected workflow</div>
                  ${renderNode(nodeA, 'a')}
                  ${renderNode(nodeB, 'b')}
                  ${renderNode(nodeC, 'c')}
                  ${renderNode(nodeD, 'd')}
                  <svg class="connector" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
                    <line x1="28" y1="24" x2="74" y2="28"></line>
                    <line x1="30" y1="70" x2="76" y2="76"></line>
                    <line x1="24" y1="28" x2="30" y2="68"></line>
                    <line x1="78" y1="30" x2="76" y2="74"></line>
                    <circle cx="52" cy="50" r="3.8"></circle>
                  </svg>
                </div>
                <div class="card mini-chart">
                  <div>
                    <div class="card-label">Operational visibility</div>
                    <div style="font-size:34px;font-weight:900;line-height:1;color:white;">Live flow</div>
                  </div>
                  <div class="mini-bars">
                    <span></span><span></span><span></span><span></span><span></span>
                  </div>
                </div>
              </div>
              <div class="metric-strip">
                <div class="metric"><div class="metric-key">pipeline</div><div class="metric-val">Aligned stages</div></div>
                <div class="metric"><div class="metric-key">handoffs</div><div class="metric-val">Cleaner routing</div></div>
                <div class="metric"><div class="metric-key">reporting</div><div class="metric-val">Faster insight</div></div>
              </div>
              <div class="pipeline">
                <div class="lane"><div class="lane-title">Capture</div><div class="lane-card">New demand enters clearly</div></div>
                <div class="lane"><div class="lane-title">Qualify</div><div class="lane-card">Work gets prioritized</div></div>
                <div class="lane"><div class="lane-title">Execute</div><div class="lane-card">Automation moves the handoff</div></div>
                <div class="lane"><div class="lane-title">Measure</div><div class="lane-card">Reporting closes the loop</div></div>
              </div>
            </div>
          </div>
        </section>
      </div>
    </section>
  </main>
</body>
</html>`;
}

async function renderPng(htmlPath, pngPath) {
  const chromiumBin = process.env.CHROMIUM_BIN || 'chromium';
  await execFileAsync(chromiumBin, [
    '--headless',
    '--disable-gpu',
    '--no-sandbox',
    '--window-size=1080,1080',
    `--screenshot=${pngPath}`,
    `file://${htmlPath}`
  ], { maxBuffer: 10 * 1024 * 1024 });
}

export async function generateSocialPostCreative({ locationId, post, businessName, outputDir } = {}) {
  if (!post || typeof post !== 'object' || Array.isArray(post)) {
    throw new Error('Missing post payload for social creative generation.');
  }

  const model = buildCreativeModel({ locationId, post, businessName });
  const html = renderHtml(model);
  const baseDir = outputDir || path.join(process.cwd(), 'data', 'generated', 'creatives');
  const fileBase = `${toSlug(`${model.brand}-${model.themeId}`)}-${Date.now()}`;
  const htmlPath = path.join(baseDir, `${fileBase}.html`);
  const jsonPath = path.join(baseDir, `${fileBase}.json`);
  const pngPath = path.join(baseDir, `${fileBase}.png`);

  await ensureDir(baseDir);
  await writeFile(htmlPath, html, 'utf8');
  await writeJson(jsonPath, model);
  await renderPng(htmlPath, pngPath);

  return {
    ok: true,
    locationId: locationId || null,
    htmlPath,
    jsonPath,
    pngPath,
    model
  };
}
