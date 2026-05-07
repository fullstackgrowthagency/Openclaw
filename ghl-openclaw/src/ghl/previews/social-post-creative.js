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

function splitParagraphs(text) {
  return String(text || '')
    .split(/\n+/)
    .map((part) => part.trim())
    .filter(Boolean);
}

function buildCreativeModel({ locationId, post, businessName } = {}) {
  const summary = normalizeString(post?.summary) || normalizeString(post?.text) || '';
  const paragraphs = splitParagraphs(summary);
  const first = paragraphs[0] || 'Build cleaner systems for better growth.';
  const second = paragraphs[1] || paragraphs[0] || 'Operations and automation can reduce friction across sales and service.';
  const third = paragraphs[2] || '';
  const brand = normalizeString(businessName)
    || normalizeString(post?.businessName)
    || 'Growth Operations';
  const kicker = 'BUSINESS CONSULTING • OPERATIONS AUTOMATION';
  const headline = first.length > 72 ? `${first.slice(0, 69).trim()}...` : first;
  const body = [second, third].filter(Boolean).join(' ');

  return {
    locationId: locationId || null,
    brand,
    kicker,
    headline,
    body,
    summary
  };
}

function renderHtml(model) {
  return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Social creative</title>
  <style>
    body {
      margin: 0;
      background: #081225;
      font-family: Inter, Arial, sans-serif;
    }
    .canvas {
      width: 1080px;
      height: 1080px;
      position: relative;
      overflow: hidden;
      background:
        radial-gradient(circle at 18% 20%, rgba(34,211,238,0.22), transparent 28%),
        radial-gradient(circle at 82% 22%, rgba(99,102,241,0.24), transparent 24%),
        linear-gradient(135deg, #081225 0%, #102542 48%, #0f766e 100%);
      color: white;
    }
    .glow {
      position: absolute;
      inset: auto;
      border-radius: 999px;
      filter: blur(24px);
      opacity: 0.85;
    }
    .glow.one { width: 240px; height: 240px; left: 60px; top: 90px; background: rgba(34,211,238,0.28); }
    .glow.two { width: 300px; height: 300px; right: 40px; bottom: 120px; background: rgba(168,85,247,0.24); }
    .panel {
      position: absolute;
      inset: 54px;
      border-radius: 38px;
      background: linear-gradient(180deg, rgba(255,255,255,0.09), rgba(255,255,255,0.04));
      border: 1px solid rgba(255,255,255,0.16);
      box-shadow: 0 30px 80px rgba(0,0,0,0.28);
      padding: 64px;
      box-sizing: border-box;
      backdrop-filter: blur(12px);
    }
    .kicker {
      display: inline-block;
      font-size: 22px;
      letter-spacing: 0.16em;
      font-weight: 800;
      color: #8be9fd;
      margin-bottom: 28px;
    }
    .headline {
      font-size: 76px;
      line-height: 1.03;
      font-weight: 900;
      max-width: 800px;
      margin: 0 0 28px;
    }
    .body {
      font-size: 34px;
      line-height: 1.34;
      max-width: 760px;
      color: rgba(255,255,255,0.88);
    }
    .brand {
      position: absolute;
      left: 64px;
      bottom: 58px;
      font-size: 30px;
      font-weight: 800;
      letter-spacing: 0.01em;
    }
    .stack {
      position: absolute;
      right: 64px;
      bottom: 62px;
      display: grid;
      gap: 14px;
      width: 290px;
    }
    .chip {
      border-radius: 20px;
      padding: 16px 18px;
      background: rgba(255,255,255,0.12);
      border: 1px solid rgba(255,255,255,0.14);
      text-align: left;
      font-size: 22px;
      font-weight: 700;
    }
  </style>
</head>
<body>
  <main class="canvas">
    <div class="glow one"></div>
    <div class="glow two"></div>
    <section class="panel">
      <div class="kicker">${escapeHtml(model.kicker)}</div>
      <h1 class="headline">${escapeHtml(model.headline)}</h1>
      <div class="body">${escapeHtml(model.body || 'Reduce friction, improve handoffs, and build cleaner systems for growth.')}</div>
      <div class="brand">${escapeHtml(model.brand)}</div>
      <div class="stack">
        <div class="chip">Lead flow clarity</div>
        <div class="chip">Automation systems</div>
        <div class="chip">Operational visibility</div>
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
  const fileBase = `${toSlug(model.headline)}-${Date.now()}`;
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
