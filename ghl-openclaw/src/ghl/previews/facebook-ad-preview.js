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
  return String(value || 'facebook-ad')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 60) || 'facebook-ad';
}

function unique(values) {
  return [...new Set((Array.isArray(values) ? values : []).map((value) => normalizeString(value)).filter(Boolean))];
}

function displayHost(url) {
  const normalized = normalizeString(url);
  if (!normalized) return null;
  try {
    return new URL(normalized).hostname.replace(/^www\./, '');
  } catch {
    return normalized;
  }
}

function sourceMediaUrls(sourcePost) {
  const values = [];
  const push = (value) => {
    const normalized = normalizeString(value);
    if (normalized) values.push(normalized);
  };

  if (!sourcePost || typeof sourcePost !== 'object') return [];
  push(sourcePost.mediaUrl);
  push(sourcePost.imageUrl);
  push(sourcePost.linkPreviewImage);
  if (Array.isArray(sourcePost.mediaUrls)) sourcePost.mediaUrls.forEach(push);
  if (Array.isArray(sourcePost.imageUrls)) sourcePost.imageUrls.forEach(push);
  if (Array.isArray(sourcePost.media)) {
    sourcePost.media.forEach((item) => {
      if (typeof item === 'string') push(item);
      else if (item && typeof item === 'object') {
        push(item.url);
        push(item.mediaUrl);
        push(item.fileUrl);
      }
    });
  }

  return unique(values);
}

function firstNonHashtagLine(text) {
  const normalized = normalizeString(text);
  if (!normalized) return null;
  return normalized
    .split('\n')
    .map((part) => part.trim())
    .find((part) => part && !part.startsWith('#')) || normalized;
}

function firstParagraph(text) {
  const normalized = normalizeString(text);
  if (!normalized) return null;
  return normalized.split(/\n\s*\n/).map((part) => part.trim()).find(Boolean) || normalized;
}

function clampText(value, maxLength) {
  const normalized = normalizeString(value);
  if (!normalized || normalized.length <= maxLength) return normalized;
  return `${normalized.slice(0, Math.max(0, maxLength - 1)).trim()}…`;
}

function buildPreviewModel({ locationId, campaignId, adsetId, adId, campaign, adset, ad, sourcePost } = {}) {
  const sourceSummary = normalizeString(sourcePost?.summary) || normalizeString(sourcePost?.text) || null;
  const creative = ad?.creative && typeof ad.creative === 'object' ? ad.creative : {};
  const mediaUrls = unique([
    creative.mediaUrl,
    ...(Array.isArray(creative.mediaUrls) ? creative.mediaUrls : []),
    ...sourceMediaUrls(sourcePost)
  ]);
  const websiteUrl = normalizeString(creative.link) || normalizeString(sourcePost?.websiteUrl) || normalizeString(sourcePost?.link) || null;
  const primaryText = normalizeString(creative.primaryText) || firstParagraph(sourceSummary) || null;
  const headline = normalizeString(creative.headline) || clampText(firstNonHashtagLine(sourceSummary), 48) || normalizeString(ad?.name) || 'New Facebook ad';
  const description = normalizeString(creative.description) || clampText(firstParagraph(sourceSummary), 120) || null;
  const cta = normalizeString(creative.callToAction) || 'LEARN_MORE';

  return {
    locationId: locationId || null,
    campaignId: campaignId || campaign?.id || null,
    adsetId: adsetId || adset?.id || null,
    adId: adId || ad?.id || null,
    campaignName: normalizeString(campaign?.name) || null,
    adsetName: normalizeString(adset?.name) || null,
    adName: normalizeString(ad?.name) || null,
    objective: normalizeString(campaign?.objective) || normalizeString(campaign?.goal) || null,
    status: normalizeString(ad?.status) || normalizeString(adset?.status) || normalizeString(campaign?.status) || null,
    pageName: normalizeString(ad?.pageName) || normalizeString(sourcePost?.pageName) || 'Facebook Page',
    primaryText,
    headline,
    description,
    cta,
    websiteUrl,
    websiteHost: displayHost(websiteUrl),
    imageUrl: mediaUrls[0] || null,
    mediaUrls,
    sourceSummary,
    targetingSummary: {
      budget: adset?.dailyBudget ?? null,
      optimizationGoal: normalizeString(adset?.optimizationGoal) || null,
      billingEvent: normalizeString(adset?.billingEvent) || null
    }
  };
}

function renderMedia(model) {
  if (!model.imageUrl) {
    return `<div class="hero hero-fallback">
      <div class="hero-badge">Preview</div>
      <div class="hero-title">No image attached</div>
    </div>`;
  }
  return `<img class="hero" src="${escapeHtml(model.imageUrl)}" alt="Facebook ad creative" />`;
}

function renderHtml(model) {
  return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>${escapeHtml(model.adName || model.campaignName || 'Facebook ad preview')}</title>
  <style>
    body {
      margin: 0;
      font-family: Inter, Arial, sans-serif;
      background: #eef2f7;
      color: #111827;
    }
    .page {
      max-width: 1360px;
      margin: 0 auto;
      padding: 28px;
    }
    .title {
      font-size: 30px;
      font-weight: 800;
      margin-bottom: 8px;
    }
    .subtitle {
      color: #64748b;
      margin-bottom: 24px;
    }
    .layout {
      display: grid;
      grid-template-columns: minmax(380px, 760px) minmax(280px, 1fr);
      gap: 24px;
      align-items: start;
    }
    .card, .meta {
      background: white;
      border-radius: 22px;
      box-shadow: 0 16px 40px rgba(15, 23, 42, 0.10);
      overflow: hidden;
    }
    .feed-header {
      display: flex;
      align-items: center;
      gap: 14px;
      padding: 22px 22px 14px;
    }
    .avatar {
      width: 48px;
      height: 48px;
      border-radius: 999px;
      background: linear-gradient(135deg, #1877f2, #60a5fa);
      display: grid;
      place-items: center;
      color: white;
      font-weight: 800;
      font-size: 18px;
    }
    .name {
      font-size: 18px;
      font-weight: 800;
    }
    .meta-line {
      font-size: 13px;
      color: #64748b;
      margin-top: 2px;
    }
    .copy {
      padding: 0 22px 18px;
      font-size: 18px;
      line-height: 1.55;
      white-space: pre-line;
    }
    .hero {
      width: 100%;
      min-height: 420px;
      max-height: 820px;
      object-fit: cover;
      display: block;
      background: #dbeafe;
    }
    .hero-fallback {
      min-height: 420px;
      display: flex;
      flex-direction: column;
      justify-content: center;
      align-items: center;
      background: linear-gradient(135deg, #0f172a, #1d4ed8);
      color: white;
      gap: 12px;
    }
    .hero-badge {
      font-size: 14px;
      font-weight: 800;
      letter-spacing: 0.14em;
      text-transform: uppercase;
      opacity: 0.72;
    }
    .hero-title {
      font-size: 36px;
      font-weight: 900;
    }
    .link-card {
      margin: 0 22px 22px;
      border: 1px solid #dbe3ee;
      border-radius: 16px;
      overflow: hidden;
      background: #f8fafc;
    }
    .link-body {
      padding: 16px 18px 18px;
    }
    .host {
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: #64748b;
      margin-bottom: 8px;
    }
    .headline {
      font-size: 24px;
      font-weight: 800;
      line-height: 1.2;
      margin-bottom: 10px;
    }
    .description {
      font-size: 16px;
      line-height: 1.45;
      color: #475569;
      margin-bottom: 16px;
    }
    .cta-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }
    .cta-button {
      border-radius: 12px;
      background: #e5e7eb;
      padding: 11px 16px;
      font-size: 14px;
      font-weight: 800;
      color: #0f172a;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }
    .meta {
      padding: 22px;
      display: grid;
      gap: 14px;
    }
    .meta-item {
      border-radius: 14px;
      background: #f8fafc;
      padding: 14px 16px;
    }
    .meta-label {
      font-size: 12px;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: #64748b;
      margin-bottom: 6px;
    }
    .meta-value {
      font-size: 15px;
      font-weight: 700;
      color: #0f172a;
      word-break: break-word;
    }
    @media (max-width: 1080px) {
      .layout { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <main class="page">
    <div class="title">Facebook ad preview</div>
    <div class="subtitle">Generated from the governed promotion payload so draft Facebook ads can be reviewed before publish.</div>
    <section class="layout">
      <article class="card">
        <div class="feed-header">
          <div class="avatar">${escapeHtml((model.pageName || 'FB').slice(0, 2).toUpperCase())}</div>
          <div>
            <div class="name">${escapeHtml(model.pageName || 'Facebook Page')}</div>
            <div class="meta-line">Sponsored · ${escapeHtml(model.status || 'Draft')}</div>
          </div>
        </div>
        <div class="copy">${escapeHtml(model.primaryText || 'No primary text found')}</div>
        ${renderMedia(model)}
        <div class="link-card">
          <div class="link-body">
            <div class="host">${escapeHtml(model.websiteHost || 'landing page')}</div>
            <div class="headline">${escapeHtml(model.headline || 'Untitled ad')}</div>
            <div class="description">${escapeHtml(model.description || 'No description found')}</div>
            <div class="cta-row">
              <div class="meta-line">${escapeHtml(model.websiteUrl || 'No URL set')}</div>
              <div class="cta-button">${escapeHtml((model.cta || 'LEARN_MORE').replace(/_/g, ' '))}</div>
            </div>
          </div>
        </div>
      </article>
      <aside class="meta">
        <div class="meta-item"><div class="meta-label">Campaign</div><div class="meta-value">${escapeHtml(model.campaignName || 'Unknown')}</div></div>
        <div class="meta-item"><div class="meta-label">Ad set</div><div class="meta-value">${escapeHtml(model.adsetName || 'Unknown')}</div></div>
        <div class="meta-item"><div class="meta-label">Ad</div><div class="meta-value">${escapeHtml(model.adName || 'Unknown')}</div></div>
        <div class="meta-item"><div class="meta-label">Objective</div><div class="meta-value">${escapeHtml(model.objective || 'Unknown')}</div></div>
        <div class="meta-item"><div class="meta-label">Budget</div><div class="meta-value">${escapeHtml(model.targetingSummary.budget == null ? 'Not set' : String(model.targetingSummary.budget))}</div></div>
        <div class="meta-item"><div class="meta-label">Optimization</div><div class="meta-value">${escapeHtml(model.targetingSummary.optimizationGoal || 'Not set')}</div></div>
        <div class="meta-item"><div class="meta-label">Billing event</div><div class="meta-value">${escapeHtml(model.targetingSummary.billingEvent || 'Not set')}</div></div>
        <div class="meta-item"><div class="meta-label">Location</div><div class="meta-value">${escapeHtml(model.locationId || 'Unknown')}</div></div>
        <div class="meta-item"><div class="meta-label">Campaign ID</div><div class="meta-value">${escapeHtml(model.campaignId || 'Pending')}</div></div>
        <div class="meta-item"><div class="meta-label">Ad set ID</div><div class="meta-value">${escapeHtml(model.adsetId || 'Pending')}</div></div>
        <div class="meta-item"><div class="meta-label">Ad ID</div><div class="meta-value">${escapeHtml(model.adId || 'Pending')}</div></div>
      </aside>
    </section>
  </main>
</body>
</html>`;
}

async function renderPreviewPng(htmlPath, pngPath) {
  const chromiumBin = process.env.CHROMIUM_BIN || 'chromium';
  await execFileAsync(chromiumBin, [
    '--headless',
    '--disable-gpu',
    '--no-sandbox',
    '--window-size=1600,1800',
    `--screenshot=${pngPath}`,
    `file://${htmlPath}`
  ], { maxBuffer: 10 * 1024 * 1024 });
}

export async function generateFacebookAdPreview({ locationId, campaignId, adsetId, adId, campaign, adset, ad, sourcePost, outputDir } = {}) {
  const model = buildPreviewModel({ locationId, campaignId, adsetId, adId, campaign, adset, ad, sourcePost });
  const html = renderHtml(model);
  const baseDir = outputDir || path.join(process.cwd(), 'data', 'generated', 'previews');
  const fileBase = `${toSlug(model.campaignName || model.adName || model.headline)}-${model.adId || 'draft'}`;
  const htmlPath = path.join(baseDir, `${fileBase}.html`);
  const jsonPath = path.join(baseDir, `${fileBase}.json`);
  const pngPath = path.join(baseDir, `${fileBase}.png`);

  await ensureDir(baseDir);
  await writeFile(htmlPath, html, 'utf8');
  await writeJson(jsonPath, model);
  await renderPreviewPng(htmlPath, pngPath);

  return {
    ok: true,
    locationId: locationId || null,
    campaignId: model.campaignId || null,
    adsetId: model.adsetId || null,
    adId: model.adId || null,
    htmlPath,
    jsonPath,
    pngPath,
    model
  };
}
