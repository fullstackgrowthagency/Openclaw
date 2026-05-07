import path from 'node:path';
import { writeFile } from 'node:fs/promises';
import { GoogleAdsAdapter } from '../api/index.js';
import { ensureDir, writeJson } from '../../lib/fs.js';

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
  return String(value || 'google-ad')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 60) || 'google-ad';
}

function inferDisplayPath(finalUrl, path1, path2) {
  if (!finalUrl) return null;
  try {
    const parsed = new URL(finalUrl);
    const parts = [path1, path2].filter(Boolean).map((part) => String(part).replace(/\//g, '›'));
    return `${parsed.hostname}${parts.length ? ' / ' + parts.join(' / ') : ''}`;
  } catch {
    return finalUrl;
  }
}

function firstAdGroup(campaign) {
  return Array.isArray(campaign?.adGroups) ? campaign.adGroups[0] || null : null;
}

function firstAdContent(adGroup) {
  return Array.isArray(adGroup?.adContent) ? adGroup.adContent[0] || null : null;
}

function filterAssetsByResourceNames(assets, resourceNames) {
  const wanted = new Set((Array.isArray(resourceNames) ? resourceNames : []).filter(Boolean));
  if (wanted.size === 0) return [];
  return (Array.isArray(assets) ? assets : []).filter((asset) => wanted.has(asset.resourceName));
}

function buildPreviewModel(campaign, sitelinkAssets, callAssets) {
  const adGroup = firstAdGroup(campaign);
  const adContent = firstAdContent(adGroup);
  const sitelinks = filterAssetsByResourceNames(sitelinkAssets, campaign?.assets?.sitelinks);
  const calls = filterAssetsByResourceNames(callAssets, campaign?.assets?.calls);
  const image = Array.isArray(campaign?.assets?.images) ? campaign.assets.images[0] || null : null;
  const finalUrl = normalizeString(adContent?.finalUrl) || normalizeString(campaign?.finalUrl);
  return {
    adId: campaign?.id || null,
    campaignName: campaign?.name || null,
    publishingStatus: campaign?.publishingStatus || null,
    advertisingChannelType: campaign?.advertisingChannelType || null,
    updatedAt: campaign?.updatedAt || null,
    businessName: normalizeString(adContent?.businessName),
    headlines: Array.isArray(adContent?.headlines) ? adContent.headlines.filter(Boolean) : [],
    longHeadlines: Array.isArray(adContent?.longHeadlines) ? adContent.longHeadlines.filter(Boolean) : [],
    descriptions: Array.isArray(adContent?.descriptions) ? adContent.descriptions.filter(Boolean) : [],
    finalUrl,
    displayPath: inferDisplayPath(finalUrl, adContent?.path1, adContent?.path2),
    path1: normalizeString(adContent?.path1),
    path2: normalizeString(adContent?.path2),
    sitelinks: sitelinks.map((asset) => ({
      linkText: asset.linkText || null,
      description1: asset.description1 || null,
      description2: asset.description2 || null,
      finalUrls: asset.finalUrls || asset.finalUrl || null,
      reviewStatus: asset.reviewStatus || null
    })),
    calls: calls.map((asset) => ({
      phoneNumber: asset.phoneNumber || null,
      reviewStatus: asset.reviewStatus || null,
      approvalStatus: asset.approvalStatus || null
    })),
    imageUrl: image?.url || null,
    keywords: adGroup?.keywords || { positives: [], negatives: [] },
    selectedChannels: Array.isArray(adGroup?.selectedChannels) ? adGroup.selectedChannels : [],
    customChannels: adGroup?.customChannels ?? null,
    audience: adGroup?.audience || { locales: [], geoLocations: [] }
  };
}

function renderSearchCard(model) {
  const lines = [];
  const host = model.displayPath ? `<div class="display-url">Sponsored · ${escapeHtml(model.displayPath)}</div>` : '';
  const headlineHtml = model.headlines.length
    ? `<div class="headline-row">${model.headlines.map((item) => `<span class="headline">${escapeHtml(item)}</span>`).join('<span class="divider">·</span>')}</div>`
    : '<div class="headline-row muted">No headlines found</div>';
  const descriptionHtml = model.descriptions.length
    ? `<div class="description">${model.descriptions.map((item) => escapeHtml(item)).join(' ')}</div>`
    : '';
  const sitelinksHtml = model.sitelinks.length
    ? `<div class="sitelinks">${model.sitelinks.map((link) => `
        <div class="sitelink">
          <div class="sitelink-title">${escapeHtml(link.linkText || 'Link')}</div>
          <div class="sitelink-copy">${[link.description1, link.description2].filter(Boolean).map((item) => escapeHtml(item)).join(' · ')}</div>
        </div>`).join('')}</div>`
    : '';
  const callHtml = model.calls[0]?.phoneNumber
    ? `<div class="callout">Call: ${escapeHtml(model.calls[0].phoneNumber)}</div>`
    : '';
  lines.push(`<section class="card search-card">
    <div class="card-label">Search-style preview</div>
    ${host}
    ${headlineHtml}
    ${descriptionHtml}
    ${callHtml}
    ${sitelinksHtml}
  </section>`);
  return lines.join('');
}

function renderDiscoverCard(model) {
  const title = model.longHeadlines[0] || model.headlines[0] || 'Untitled ad';
  const description = model.descriptions[0] || '';
  return `<section class="card discover-card">
    <div class="card-label">Visual placement preview</div>
    ${model.imageUrl ? `<img class="hero" src="${escapeHtml(model.imageUrl)}" alt="Ad creative" />` : '<div class="hero hero-fallback">No image creative</div>'}
    <div class="discover-body">
      <div class="discover-meta">Sponsored · ${escapeHtml(model.businessName || 'Business')}</div>
      <div class="discover-title">${escapeHtml(title)}</div>
      <div class="discover-description">${escapeHtml(description)}</div>
    </div>
  </section>`;
}

function renderMeta(model) {
  const audienceBits = [
    ...(Array.isArray(model.audience?.geoLocations) ? model.audience.geoLocations.map((item) => item?.name).filter(Boolean) : []),
    ...(Array.isArray(model.audience?.locales) ? model.audience.locales.map((item) => item?.name || item?.code).filter(Boolean) : [])
  ];
  return `<section class="meta">
    <div><strong>Campaign:</strong> ${escapeHtml(model.campaignName || 'Unknown')}</div>
    <div><strong>Ad ID:</strong> ${escapeHtml(model.adId || 'Unknown')}</div>
    <div><strong>Status:</strong> ${escapeHtml(model.publishingStatus || 'Unknown')}</div>
    <div><strong>Channel:</strong> ${escapeHtml(model.advertisingChannelType || 'Unknown')}</div>
    <div><strong>Selected placements:</strong> ${escapeHtml(model.selectedChannels.join(', ') || 'Default')}</div>
    <div><strong>Audience:</strong> ${escapeHtml(audienceBits.join(', ') || 'Not specified')}</div>
    <div><strong>Updated:</strong> ${escapeHtml(model.updatedAt || 'Unknown')}</div>
  </section>`;
}

function renderHtml(model) {
  return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>${escapeHtml(model.campaignName || 'Google ad preview')}</title>
  <style>
    :root { color-scheme: light; }
    body {
      margin: 0;
      font-family: Inter, Arial, sans-serif;
      background: #f5f7fb;
      color: #202124;
    }
    .page {
      max-width: 1200px;
      margin: 0 auto;
      padding: 32px;
    }
    .title {
      font-size: 28px;
      font-weight: 700;
      margin-bottom: 8px;
    }
    .subtitle {
      color: #5f6368;
      margin-bottom: 24px;
    }
    .grid {
      display: grid;
      grid-template-columns: minmax(320px, 1fr) minmax(320px, 1fr);
      gap: 24px;
      align-items: start;
    }
    .card, .meta {
      background: white;
      border-radius: 18px;
      box-shadow: 0 8px 30px rgba(60, 64, 67, 0.12);
      padding: 24px;
    }
    .card-label {
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: #5f6368;
      margin-bottom: 14px;
    }
    .display-url {
      color: #188038;
      font-size: 14px;
      margin-bottom: 10px;
    }
    .headline-row {
      color: #1a0dab;
      font-size: 24px;
      line-height: 1.25;
      margin-bottom: 12px;
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
    }
    .headline { font-weight: 500; }
    .divider { color: #5f6368; }
    .description, .callout, .sitelink-copy, .discover-description { color: #4d5156; line-height: 1.5; }
    .sitelinks {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 14px;
      margin-top: 18px;
    }
    .sitelink-title {
      color: #1a0dab;
      font-weight: 500;
      margin-bottom: 4px;
    }
    .hero {
      width: 100%;
      aspect-ratio: 1.91 / 1;
      object-fit: cover;
      border-radius: 14px;
      background: #e8eaed;
      display: block;
      margin-bottom: 16px;
    }
    .hero-fallback {
      display: grid;
      place-items: center;
      color: #5f6368;
      font-size: 14px;
    }
    .discover-meta {
      color: #5f6368;
      font-size: 14px;
      margin-bottom: 8px;
    }
    .discover-title {
      font-size: 22px;
      font-weight: 700;
      line-height: 1.3;
      margin-bottom: 10px;
    }
    .meta {
      margin-top: 24px;
      display: grid;
      gap: 10px;
      color: #3c4043;
    }
    .muted { color: #5f6368; }
    @media (max-width: 900px) {
      .page { padding: 18px; }
      .grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="page">
    <div class="title">Google ad preview</div>
    <div class="subtitle">Generated from the saved ad object, copy, image asset, call asset, and sitelinks for repeatable preview delivery.</div>
    <div class="grid">
      ${renderSearchCard(model)}
      ${renderDiscoverCard(model)}
    </div>
    ${renderMeta(model)}
  </div>
</body>
</html>`;
}

export async function generateGoogleAdPreview({ credentialRef, locationId, adId, outputDir, adapter = new GoogleAdsAdapter() }) {
  if (!credentialRef) throw new Error('Missing credentialRef');
  if (!locationId) throw new Error('Missing locationId');
  if (!adId) throw new Error('Missing adId');

  const [campaignResult, sitelinksResult, callsResult] = await Promise.all([
    adapter.getCampaign(credentialRef, adId, { locationId }),
    adapter.getAssets(credentialRef, { locationId, type: 'SITELINK' }),
    adapter.getAssets(credentialRef, { locationId, type: 'CALL' })
  ]);

  const campaign = campaignResult?.data || null;
  if (!campaign || typeof campaign !== 'object') {
    throw new Error(`Google ad ${adId} not found or returned no campaign data.`);
  }

  const model = buildPreviewModel(campaign, sitelinksResult?.data || [], callsResult?.data || []);
  const html = renderHtml(model);
  const baseDir = outputDir || path.join(process.cwd(), 'data', 'generated', 'previews');
  const fileBase = `${toSlug(model.campaignName || 'google-ad')}-${adId}`;
  const htmlPath = path.join(baseDir, `${fileBase}.html`);
  const jsonPath = path.join(baseDir, `${fileBase}.json`);

  await ensureDir(baseDir);
  await writeFile(htmlPath, html, 'utf8');
  await writeJson(jsonPath, model);

  return {
    ok: true,
    adId,
    locationId,
    htmlPath,
    jsonPath,
    model
  };
}
