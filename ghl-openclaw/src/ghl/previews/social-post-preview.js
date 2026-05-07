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
  return String(value || 'social-post')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 60) || 'social-post';
}

function normalizePlatform(value) {
  const raw = String(value || '').trim().toLowerCase();
  if (!raw) return 'social';
  if (['facebook', 'fb'].includes(raw)) return 'facebook';
  if (['instagram', 'ig'].includes(raw)) return 'instagram';
  if (['linkedin', 'linked_in'].includes(raw)) return 'linkedin';
  if (['twitter', 'x'].includes(raw)) return 'x';
  if (['google', 'googlebusinessprofile', 'google_business_profile', 'gbp'].includes(raw)) return 'googlebusinessprofile';
  if (['tiktok', 'tik_tok'].includes(raw)) return 'tiktok';
  if (['youtube'].includes(raw)) return 'youtube';
  return raw;
}

function platformLabel(platform) {
  switch (normalizePlatform(platform)) {
    case 'facebook': return 'Facebook';
    case 'instagram': return 'Instagram';
    case 'linkedin': return 'LinkedIn';
    case 'x': return 'X';
    case 'googlebusinessprofile': return 'Google Business Profile';
    case 'tiktok': return 'TikTok';
    case 'youtube': return 'YouTube';
    default: return 'Social';
  }
}

function platformAccent(platform) {
  switch (normalizePlatform(platform)) {
    case 'facebook': return '#1877f2';
    case 'instagram': return '#d62976';
    case 'linkedin': return '#0a66c2';
    case 'x': return '#111827';
    case 'googlebusinessprofile': return '#1a73e8';
    case 'tiktok': return '#111111';
    case 'youtube': return '#ff0033';
    default: return '#475569';
  }
}

function extractMedia(post) {
  const explicitMedia = Array.isArray(post?.media) ? post.media : [];
  const urls = [
    ...explicitMedia.map((item) => item?.url || item?.src || item?.fileUrl || null),
    ...(Array.isArray(post?.mediaUrls) ? post.mediaUrls : []),
    ...(Array.isArray(post?.imageUrls) ? post.imageUrls : [])
  ].filter(Boolean);

  return urls.map((url, index) => {
    const entry = explicitMedia[index] || {};
    const lower = String(url).toLowerCase();
    const inferredType = lower.match(/\.(mp4|mov|webm|m4v)(\?|$)/) ? 'VIDEO' : 'IMAGE';
    return {
      url,
      type: normalizeString(entry.type)?.toUpperCase() || inferredType,
      caption: normalizeString(entry.caption),
      alt: normalizeString(entry.alt)
    };
  });
}

function extractSelectedAccounts(post, socialAccounts = []) {
  const requestedIds = Array.isArray(post?.accountIds) ? post.accountIds.filter(Boolean) : [];
  const accountMap = new Map((Array.isArray(socialAccounts) ? socialAccounts : []).map((account) => [account?.id, account]));
  const selected = requestedIds.map((id) => {
    const account = accountMap.get(id) || {};
    return {
      id,
      name: account?.name || account?.accountName || id,
      platform: normalizePlatform(account?.platform || account?.type || 'social'),
      type: account?.type || null,
      avatar: account?.avatar || null
    };
  });

  if (selected.length > 0) return selected;

  return [
    { id: 'facebook', name: 'Facebook Page', platform: 'facebook' },
    { id: 'instagram', name: 'Instagram Profile', platform: 'instagram' },
    { id: 'linkedin', name: 'LinkedIn Page', platform: 'linkedin' },
    { id: 'x', name: 'X Profile', platform: 'x' }
  ];
}

function buildModel({ locationId, postId, post, socialAccounts }) {
  const summary = normalizeString(post?.summary) || normalizeString(post?.text) || '';
  const media = extractMedia(post);
  const primaryMedia = media[0] || null;
  const selectedAccounts = extractSelectedAccounts(post, socialAccounts);
  const previewCards = selectedAccounts.map((account) => ({
    accountId: account.id,
    accountName: account.name,
    platform: account.platform,
    platformLabel: platformLabel(account.platform),
    accent: platformAccent(account.platform)
  }));

  return {
    locationId: locationId || null,
    postId: postId || null,
    status: normalizeString(post?.status) || null,
    scheduleDate: normalizeString(post?.scheduleDate) || normalizeString(post?.scheduledAt) || null,
    summary,
    media,
    primaryMedia,
    accountIds: Array.isArray(post?.accountIds) ? post.accountIds.filter(Boolean) : [],
    previewCards
  };
}

function renderMedia(media, platform) {
  if (!media?.url) {
    return `<div class="media fallback ${escapeHtml(normalizePlatform(platform))}">No media attached</div>`;
  }
  if (String(media.type || '').toUpperCase() === 'VIDEO') {
    return `<div class="media video"><div class="play">▶</div><div class="video-label">Video preview</div></div>`;
  }
  return `<img class="media" src="${escapeHtml(media.url)}" alt="${escapeHtml(media.alt || media.caption || 'Social post media')}" />`;
}

function renderSummary(text) {
  if (!text) return '<div class="post-copy muted">No post copy found</div>';
  return `<div class="post-copy">${escapeHtml(text).replace(/\n/g, '<br />')}</div>`;
}

function renderCard(card, model) {
  const media = renderMedia(model.primaryMedia, card.platform);
  const summary = renderSummary(model.summary);
  const metaLine = [card.platformLabel, model.status || 'draft', model.scheduleDate || 'unscheduled'].filter(Boolean).join(' • ');

  switch (card.platform) {
    case 'instagram':
      return `<section class="card instagram">
        <div class="card-head" style="--accent:${card.accent}">
          <div class="badge">${escapeHtml(card.platformLabel)}</div>
          <div class="account">${escapeHtml(card.accountName)}</div>
        </div>
        ${media}
        <div class="card-body compact">
          <div class="meta-line">${escapeHtml(metaLine)}</div>
          ${summary}
        </div>
      </section>`;
    case 'x':
      return `<section class="card x-post">
        <div class="card-head" style="--accent:${card.accent}">
          <div class="badge">${escapeHtml(card.platformLabel)}</div>
          <div class="account">${escapeHtml(card.accountName)}</div>
        </div>
        <div class="card-body">
          <div class="meta-line">${escapeHtml(metaLine)}</div>
          ${summary}
          ${model.primaryMedia ? `<div class="inline-media">${media}</div>` : ''}
        </div>
      </section>`;
    case 'linkedin':
      return `<section class="card linkedin">
        <div class="card-head" style="--accent:${card.accent}">
          <div class="badge">${escapeHtml(card.platformLabel)}</div>
          <div class="account">${escapeHtml(card.accountName)}</div>
        </div>
        <div class="card-body">
          <div class="meta-line">${escapeHtml(metaLine)}</div>
          ${summary}
          ${model.primaryMedia ? `<div class="inline-media">${media}</div>` : ''}
        </div>
      </section>`;
    case 'googlebusinessprofile':
      return `<section class="card gbp">
        <div class="card-head" style="--accent:${card.accent}">
          <div class="badge">${escapeHtml(card.platformLabel)}</div>
          <div class="account">${escapeHtml(card.accountName)}</div>
        </div>
        ${model.primaryMedia ? media : ''}
        <div class="card-body">
          <div class="meta-line">${escapeHtml(metaLine)}</div>
          ${summary}
        </div>
      </section>`;
    default:
      return `<section class="card facebook">
        <div class="card-head" style="--accent:${card.accent}">
          <div class="badge">${escapeHtml(card.platformLabel)}</div>
          <div class="account">${escapeHtml(card.accountName)}</div>
        </div>
        <div class="card-body">
          <div class="meta-line">${escapeHtml(metaLine)}</div>
          ${summary}
          ${model.primaryMedia ? `<div class="inline-media">${media}</div>` : ''}
        </div>
      </section>`;
  }
}

function renderHtml(model) {
  const cards = model.previewCards.map((card) => renderCard(card, model)).join('\n');
  return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Social post preview</title>
  <style>
    body {
      margin: 0;
      font-family: Inter, Arial, sans-serif;
      background: #f3f6fb;
      color: #111827;
    }
    .page {
      max-width: 1500px;
      margin: 0 auto;
      padding: 28px;
    }
    .title { font-size: 30px; font-weight: 800; margin-bottom: 8px; }
    .subtitle { color: #475569; margin-bottom: 22px; }
    .meta {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 22px;
    }
    .meta-item {
      background: white;
      border-radius: 14px;
      padding: 14px 16px;
      box-shadow: 0 10px 24px rgba(15, 23, 42, 0.08);
    }
    .meta-label { font-size: 12px; text-transform: uppercase; color: #64748b; margin-bottom: 6px; }
    .meta-value { font-size: 14px; font-weight: 600; word-break: break-word; }
    .grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(320px, 1fr));
      gap: 22px;
      align-items: start;
    }
    .card {
      background: white;
      border-radius: 22px;
      box-shadow: 0 14px 36px rgba(15, 23, 42, 0.12);
      overflow: hidden;
      min-height: 420px;
    }
    .card-head {
      border-top: 6px solid var(--accent);
      padding: 18px 20px 10px;
    }
    .badge {
      display: inline-block;
      font-size: 11px;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--accent);
      margin-bottom: 8px;
    }
    .account {
      font-size: 20px;
      font-weight: 700;
    }
    .card-body {
      padding: 0 20px 20px;
    }
    .card-body.compact { padding: 16px 20px 20px; }
    .meta-line {
      font-size: 13px;
      color: #64748b;
      margin: 2px 0 14px;
    }
    .post-copy {
      font-size: 17px;
      line-height: 1.55;
      white-space: normal;
    }
    .muted { color: #94a3b8; }
    .media {
      width: 100%;
      display: block;
      object-fit: cover;
      background: linear-gradient(135deg, #0f172a, #1d4ed8);
      min-height: 320px;
      max-height: 560px;
    }
    .inline-media {
      margin-top: 18px;
      border-radius: 18px;
      overflow: hidden;
      border: 1px solid #e2e8f0;
    }
    .fallback {
      display: flex;
      align-items: center;
      justify-content: center;
      color: white;
      font-weight: 700;
      font-size: 18px;
      min-height: 320px;
      background: linear-gradient(135deg, #1e293b, #334155);
    }
    .video {
      position: relative;
      background: linear-gradient(135deg, #0f172a, #111827);
    }
    .play {
      position: absolute;
      inset: 0;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 64px;
      color: rgba(255,255,255,0.92);
    }
    .video-label {
      position: absolute;
      left: 18px;
      bottom: 18px;
      color: white;
      font-weight: 700;
      background: rgba(15, 23, 42, 0.65);
      padding: 8px 10px;
      border-radius: 999px;
    }
    @media (max-width: 1100px) {
      .grid, .meta { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <main class="page">
    <div class="title">Social post previews</div>
    <div class="subtitle">Generated from the post payload and selected social accounts so each targeted platform gets its own sendable preview.</div>
    <section class="meta">
      <div class="meta-item"><div class="meta-label">Post ID</div><div class="meta-value">${escapeHtml(model.postId || 'Pending')}</div></div>
      <div class="meta-item"><div class="meta-label">Location</div><div class="meta-value">${escapeHtml(model.locationId || 'Unknown')}</div></div>
      <div class="meta-item"><div class="meta-label">Status</div><div class="meta-value">${escapeHtml(model.status || 'Unknown')}</div></div>
      <div class="meta-item"><div class="meta-label">Preview count</div><div class="meta-value">${escapeHtml(String(model.previewCards.length))}</div></div>
    </section>
    <section class="grid">${cards}</section>
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
    '--window-size=1600,2200',
    `--screenshot=${pngPath}`,
    `file://${htmlPath}`
  ], { maxBuffer: 10 * 1024 * 1024 });
}

export async function generateSocialPostPreview({ locationId, postId, post, socialAccounts, outputDir } = {}) {
  if (!post || typeof post !== 'object' || Array.isArray(post)) {
    throw new Error('Missing post payload for social preview generation.');
  }

  const model = buildModel({ locationId, postId, post, socialAccounts });
  const html = renderHtml(model);
  const baseDir = outputDir || path.join(process.cwd(), 'data', 'generated', 'previews');
  const fileBase = `${toSlug(model.summary.split(/\s+/).slice(0, 6).join(' ') || 'social-post')}-${postId || 'draft'}`;
  const htmlPath = path.join(baseDir, `${fileBase}.html`);
  const jsonPath = path.join(baseDir, `${fileBase}.json`);
  const pngPath = path.join(baseDir, `${fileBase}.png`);

  await ensureDir(baseDir);
  await writeFile(htmlPath, html, 'utf8');
  await writeJson(jsonPath, model);

  let pngError = null;
  try {
    await renderPreviewPng(htmlPath, pngPath);
  } catch (error) {
    pngError = error.message;
  }

  return {
    ok: true,
    locationId: locationId || null,
    postId: postId || null,
    htmlPath,
    jsonPath,
    pngPath: pngError ? null : pngPath,
    ...(pngError ? { pngError } : {}),
    model
  };
}
