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

function clampText(value, maxLength) {
  const normalized = normalizeString(value);
  if (!normalized || normalized.length <= maxLength) return normalized;
  return `${normalized.slice(0, Math.max(0, maxLength - 1)).trim()}…`;
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

function uniqueCompact(values) {
  return [...new Set((Array.isArray(values) ? values : []).map((value) => normalizeString(value)).filter(Boolean))];
}

function hasAny(tokens, candidates = []) {
  return candidates.some((candidate) => tokens.includes(candidate));
}

function splitSentences(text) {
  const normalized = normalizeString(text);
  if (!normalized) return [];
  return normalized
    .replace(/\s+/g, ' ')
    .split(/(?<=[.!?])\s+/)
    .map((part) => part.trim())
    .filter(Boolean);
}

function titleCase(value) {
  const normalized = normalizeString(value);
  if (!normalized) return null;
  return normalized
    .split(/\s+/)
    .map((part) => part ? `${part.charAt(0).toUpperCase()}${part.slice(1).toLowerCase()}` : part)
    .join(' ')
    .trim() || null;
}

function initials(value) {
  const normalized = normalizeString(value) || 'GO';
  const letters = normalized
    .split(/\s+/)
    .map((part) => part.charAt(0).toUpperCase())
    .filter(Boolean)
    .slice(0, 2)
    .join('');
  return letters || 'GO';
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

function cleanSummary(summary, brand) {
  const normalized = normalizeString(summary) || '';
  const escapedBrand = (normalizeString(brand) || '').replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return normalized
    .replace(/https?:\/\/\S+/g, ' ')
    .replace(/[#@][\p{L}\p{N}_-]+/gu, ' ')
    .replace(escapedBrand ? new RegExp(`${escapedBrand}\\s+(?:helps|builds|creates|delivers|offers|improves|simplifies)\\s+`, 'ig') : /$^/, '')
    .replace(/\s+/g, ' ')
    .trim();
}

function breakHeadline(value, maxLineLength = 15, maxLines = 3) {
  const normalized = normalizeString(value);
  if (!normalized) return '';

  const words = normalized.split(/\s+/);
  const lines = [];
  let current = '';

  for (const word of words) {
    const candidate = current ? `${current} ${word}` : word;
    if (candidate.length <= maxLineLength || !current) {
      current = candidate;
      continue;
    }
    lines.push(current);
    current = word;
    if (lines.length === maxLines - 1) break;
  }

  const consumedWords = lines.join(' ').split(/\s+/).filter(Boolean).length;
  const remaining = words.slice(consumedWords);
  const tail = current ? [current, ...remaining.slice(1)] : remaining;
  if (tail.length > 0 && lines.length < maxLines) {
    lines.push(tail.join(' '));
  }

  const limited = lines.slice(0, maxLines).map((line, index) => {
    if (index === maxLines - 1) return clampText(line, maxLineLength + 2) || line;
    return line;
  }).filter(Boolean);

  return limited.join('\n');
}

function defaultTheme(id, overrides = {}) {
  return {
    id,
    layout: 'orbit',
    eyebrow: 'SYSTEM-LED GROWTH',
    heroWord: 'SYSTEMS',
    headline: 'Build trusted systems',
    support: 'Turn scattered work into a cleaner operating layer.',
    proofs: [
      { label: 'Sharper positioning', detail: 'one clear idea per asset' },
      { label: 'Faster execution', detail: 'better handoffs and less drag' },
      { label: 'Better visibility', detail: 'see what is actually moving' }
    ],
    metrics: [
      { value: 'Clear', label: 'operating layer' },
      { value: 'Fast', label: 'team response' },
      { value: 'Visible', label: 'growth flow' }
    ],
    nodes: ['Plan', 'Build', 'Route', 'Measure'],
    palette: {
      bgA: '#07111f',
      bgB: '#10213d',
      bgC: '#0f4c81',
      glowA: 'rgba(34, 211, 238, 0.30)',
      glowB: 'rgba(129, 140, 248, 0.24)',
      accent: '#7dd3fc',
      accentStrong: '#38bdf8',
      accentAlt: '#a78bfa',
      accentSoft: 'rgba(125, 211, 252, 0.14)',
      panel: 'rgba(8, 16, 31, 0.62)',
      panelStrong: 'rgba(8, 16, 31, 0.82)',
      line: 'rgba(148, 163, 184, 0.28)'
    },
    ...overrides
  };
}

function inferCreativeTheme(summary) {
  const tokens = tokenize(summary);
  const lower = normalizeString(summary)?.toLowerCase() || '';

  const mentionsWebsite = hasAny(tokens, ['website', 'websites', 'design', 'landing', 'page']);
  const mentionsConsistency = hasAny(tokens, ['consistency', 'consistent', 'audience', 'message', 'content', 'social', 'viral']);
  const mentionsAutomation = hasAny(tokens, ['automation', 'automations', 'automated', 'workflow', 'workflows', 'system', 'systems', 'process', 'processes']);
  const mentionsOperations = hasAny(tokens, ['operations', 'operational', 'handoffs', 'delivery', 'crm', 'service']);
  const mentionsConsulting = hasAny(tokens, ['consulting', 'consultant', 'strategy', 'strategic', 'advisor', 'advisory']);
  const mentionsAnalytics = hasAny(tokens, ['analytics', 'attribution', 'reporting', 'tracking', 'data', 'dashboard', 'metrics']);
  const mentionsRevenue = hasAny(tokens, ['sales', 'lead', 'leads', 'followup', 'follow', 'pipeline', 'conversion', 'conversions', 'booked', 'booking']);

  if (mentionsWebsite || /books calls|captures leads|moves people to action|next step obvious/.test(lower)) {
    return defaultTheme('website-conversion', {
      layout: 'editorial',
      eyebrow: 'WEBSITE CONVERSION',
      heroWord: 'CONVERT',
      headline: 'Make action obvious',
      support: 'Guide more visitors from first click to booked call.',
      proofs: [
        { label: 'Clear next step', detail: 'remove guesswork from the page' },
        { label: 'Sharper offer', detail: 'show value without extra noise' },
        { label: 'Higher intent', detail: 'move traffic toward response' }
      ],
      metrics: [
        { value: 'Calls ↑', label: 'stronger path' },
        { value: 'Leads ↑', label: 'less friction' },
        { value: 'Noise ↓', label: 'clear action' }
      ],
      nodes: ['Visit', 'Offer', 'Action', 'Book'],
      palette: {
        bgA: '#130e27',
        bgB: '#221a44',
        bgC: '#5b21b6',
        glowA: 'rgba(192, 132, 252, 0.28)',
        glowB: 'rgba(96, 165, 250, 0.22)',
        accent: '#ddd6fe',
        accentStrong: '#c084fc',
        accentAlt: '#60a5fa',
        accentSoft: 'rgba(192, 132, 252, 0.14)',
        panel: 'rgba(18, 14, 39, 0.62)',
        panelStrong: 'rgba(18, 14, 39, 0.84)',
        line: 'rgba(196, 181, 253, 0.30)'
      }
    });
  }

  if (mentionsConsistency || /right message.*right audience.*right time|consistency compounds/.test(lower)) {
    return defaultTheme('social-consistency', {
      layout: 'editorial',
      eyebrow: 'SOCIAL CONSISTENCY',
      heroWord: 'MOMENTUM',
      headline: 'Consistency compounds',
      support: 'Show up with the right message, for the right audience, at the right time.',
      proofs: [
        { label: 'Right audience', detail: 'less scatter, more relevance' },
        { label: 'Clear message', detail: 'one strong point of view' },
        { label: 'Steady presence', detail: 'momentum that keeps building' }
      ],
      metrics: [
        { value: 'Signal ↑', label: 'sharper message' },
        { value: 'Reach ↑', label: 'better cadence' },
        { value: 'Drift ↓', label: 'consistent voice' }
      ],
      nodes: ['Plan', 'Create', 'Publish', 'Repeat'],
      palette: {
        bgA: '#071c1b',
        bgB: '#0d2c2b',
        bgC: '#0f766e',
        glowA: 'rgba(45, 212, 191, 0.26)',
        glowB: 'rgba(56, 189, 248, 0.18)',
        accent: '#99f6e4',
        accentStrong: '#2dd4bf',
        accentAlt: '#67e8f9',
        accentSoft: 'rgba(45, 212, 191, 0.14)',
        panel: 'rgba(6, 25, 24, 0.62)',
        panelStrong: 'rgba(6, 25, 24, 0.84)',
        line: 'rgba(153, 246, 228, 0.28)'
      }
    });
  }

  if ((mentionsAnalytics && !mentionsAutomation && !mentionsOperations && !mentionsConsulting && !mentionsRevenue)
    || /what is working|what is not|change next|click to client|better tracking|clear growth decisions/.test(lower)) {
    return defaultTheme('analytics-clarity', {
      layout: 'signal',
      eyebrow: 'ANALYTICS CLARITY',
      heroWord: 'SIGNAL',
      headline: 'See what converts',
      support: 'Turn cleaner tracking into sharper decisions and better offers.',
      proofs: [
        { label: 'Clear attribution', detail: 'follow the path from click to client' },
        { label: 'Useful reporting', detail: 'know what changes next' },
        { label: 'Sharper offers', detail: 'back messaging with signal' }
      ],
      metrics: [
        { value: 'Signal', label: 'over noise' },
        { value: 'Tracking', label: 'made useful' },
        { value: 'Decisions', label: 'made clearer' }
      ],
      nodes: ['Source', 'Track', 'Read', 'Act'],
      palette: {
        bgA: '#06101d',
        bgB: '#13294b',
        bgC: '#1d4ed8',
        glowA: 'rgba(96, 165, 250, 0.30)',
        glowB: 'rgba(168, 85, 247, 0.20)',
        accent: '#bfdbfe',
        accentStrong: '#60a5fa',
        accentAlt: '#a78bfa',
        accentSoft: 'rgba(96, 165, 250, 0.14)',
        panel: 'rgba(6, 16, 29, 0.62)',
        panelStrong: 'rgba(6, 16, 29, 0.84)',
        line: 'rgba(191, 219, 254, 0.26)'
      }
    });
  }

  if ((mentionsConsulting && (mentionsAutomation || mentionsOperations)) || /easier to trust|aligned|cleaner growth/.test(lower)) {
    return defaultTheme('consulting-systems', {
      layout: 'orbit',
      eyebrow: 'CONSULTING • OPERATIONS',
      heroWord: 'TRUST',
      headline: 'Trusted operations',
      support: 'Align consulting, follow-up, delivery, and reporting into one clear operating layer.',
      proofs: [
        { label: 'Less friction', detail: 'teams move with more confidence' },
        { label: 'Cleaner handoffs', detail: 'from planning into delivery' },
        { label: 'Faster decisions', detail: 'leaders can trust the system' }
      ],
      metrics: [
        { value: 'Trust', label: 'inside the stack' },
        { value: 'Aligned', label: 'handoffs' },
        { value: 'Ready', label: 'for scale' }
      ],
      nodes: ['Consult', 'Route', 'Deliver', 'Report'],
      palette: {
        bgA: '#0d1021',
        bgB: '#1e1b4b',
        bgC: '#0f766e',
        glowA: 'rgba(45, 212, 191, 0.24)',
        glowB: 'rgba(129, 140, 248, 0.24)',
        accent: '#c4b5fd',
        accentStrong: '#818cf8',
        accentAlt: '#2dd4bf',
        accentSoft: 'rgba(129, 140, 248, 0.14)',
        panel: 'rgba(13, 16, 33, 0.62)',
        panelStrong: 'rgba(13, 16, 33, 0.84)',
        line: 'rgba(196, 181, 253, 0.28)'
      }
    });
  }

  if (mentionsRevenue || /follow-up problem|fast response|simple pipeline|booked calls|closed deals/.test(lower)) {
    return defaultTheme('lead-follow-up', {
      layout: 'pipeline',
      eyebrow: 'LEAD FOLLOW-UP',
      heroWord: 'FOLLOW-UP',
      headline: 'Fix follow-up gaps',
      support: 'Speed up response and keep leads moving toward close.',
      proofs: [
        { label: 'Faster response', detail: 'catch more intent while it is hot' },
        { label: 'Visible pipeline', detail: 'know where movement slows' },
        { label: 'Cleaner routing', detail: 'handoffs stay on track' }
      ],
      metrics: [
        { value: 'Speed', label: 'first response' },
        { value: 'Flow', label: 'through pipeline' },
        { value: 'Close', label: 'with less drag' }
      ],
      nodes: ['Capture', 'Respond', 'Route', 'Close'],
      palette: {
        bgA: '#180c14',
        bgB: '#3b102a',
        bgC: '#9a3412',
        glowA: 'rgba(251, 113, 133, 0.26)',
        glowB: 'rgba(245, 158, 11, 0.18)',
        accent: '#fecdd3',
        accentStrong: '#fb7185',
        accentAlt: '#f59e0b',
        accentSoft: 'rgba(251, 113, 133, 0.14)',
        panel: 'rgba(24, 12, 20, 0.62)',
        panelStrong: 'rgba(24, 12, 20, 0.84)',
        line: 'rgba(254, 205, 211, 0.28)'
      }
    });
  }

  if (mentionsAutomation || mentionsOperations || /systems finally talk to each other|operations get lighter|real conversations|more human/.test(lower)) {
    return defaultTheme('automation-ops', {
      layout: 'pipeline',
      eyebrow: 'OPERATIONS AUTOMATION',
      heroWord: 'SYNC',
      headline: 'Systems in sync',
      support: 'Simplify follow-up, reporting, and internal handoffs without adding more noise.',
      proofs: [
        { label: 'Manual work down', detail: 'free the team for real service' },
        { label: 'Clean handoffs', detail: 'keep work flowing across teams' },
        { label: 'Live visibility', detail: 'see the stack without the chaos' }
      ],
      metrics: [
        { value: 'Sync', label: 'across the stack' },
        { value: 'Flow', label: 'through handoffs' },
        { value: 'Time', label: 'back for service' }
      ],
      nodes: ['Capture', 'Route', 'Execute', 'Report'],
      palette: {
        bgA: '#08111f',
        bgB: '#123153',
        bgC: '#0f766e',
        glowA: 'rgba(34, 211, 238, 0.26)',
        glowB: 'rgba(45, 212, 191, 0.22)',
        accent: '#a5f3fc',
        accentStrong: '#22d3ee',
        accentAlt: '#2dd4bf',
        accentSoft: 'rgba(34, 211, 238, 0.14)',
        panel: 'rgba(8, 17, 31, 0.62)',
        panelStrong: 'rgba(8, 17, 31, 0.84)',
        line: 'rgba(165, 243, 252, 0.28)'
      }
    });
  }

  return defaultTheme('growth-systems', {
    layout: 'orbit',
    eyebrow: 'FULL-STACK GROWTH',
    heroWord: 'MOTION',
    headline: 'Growth stack aligned',
    support: 'Connect strategy, site, content, automation, and follow-up so momentum holds up.',
    proofs: [
      { label: 'One operating layer', detail: 'the stack works together' },
      { label: 'Less drag', detail: 'clearer movement across teams' },
      { label: 'More momentum', detail: 'systems that hold under growth' }
    ],
    metrics: [
      { value: 'Stack', label: 'working together' },
      { value: 'Motion', label: 'that holds up' },
      { value: 'Clarity', label: 'across the flow' }
    ],
    nodes: ['Strategy', 'Site', 'Automate', 'Follow-up']
  });
}

function deriveCreativeHeadline(summary, theme) {
  const lower = normalizeString(summary)?.toLowerCase() || '';
  const candidates = [
    /systems finally talk to each other|systems talk to each other/.test(lower) ? 'Systems in sync' : null,
    /operations are easier to trust|easier to trust/.test(lower) ? 'Trusted operations' : null,
    /tracking|clear growth decisions|click to client|better offers/.test(lower) ? 'See what converts' : null,
    /books calls|captures leads|moves people to action|next step obvious/.test(lower) ? 'Make action obvious' : null,
    /consistency compounds|show up consistently|right message.*right audience/.test(lower) ? 'Consistency compounds' : null,
    /feel more human|real conversations|better service/.test(lower) ? 'More time for service' : null,
    /follow-up problem|fast response|simple pipeline/.test(lower) ? 'Fix follow-up gaps' : null,
    /what is working|what is not|change next/.test(lower) ? 'See what matters' : null,
    /strategy, site, content, automation, and follow-up|working together|predictable/.test(lower) ? 'Growth stack aligned' : null,
    /faster decisions|less friction/.test(lower) ? 'Cut operating friction' : null,
    theme.headline
  ];

  return candidates.find(Boolean) || 'Build trusted systems';
}

function deriveCreativeSupport(summary, theme, brand) {
  const lower = normalizeString(summary)?.toLowerCase() || '';
  const cleaned = cleanSummary(summary, brand);
  const firstSentence = splitSentences(cleaned)[0] || '';
  const candidates = [
    /simplify follow-up,? reporting,? and internal handoffs/.test(lower)
      ? 'Simplify follow-up, reporting, and internal handoffs.'
      : null,
    /consulting,? follow-up,? delivery,? and reporting are aligned/.test(lower)
      ? 'Align consulting, follow-up, delivery, and reporting.'
      : null,
    /faster decisions|less friction/.test(lower)
      ? 'Help leaders decide faster and teams move with less friction.'
      : null,
    /feel more human|real conversations|better service/.test(lower)
      ? 'Automate the repetitive work, not the relationships.'
      : null,
    /books calls|captures leads|moves people to action/.test(lower)
      ? 'Guide more visitors from first click to booked call.'
      : null,
    /tracking|clear growth decisions|click to client|better offers/.test(lower)
      ? 'Turn cleaner tracking into sharper decisions and stronger offers.'
      : null,
    /right message.*right audience.*right time/.test(lower)
      ? 'Stay visible with the right message for the right people.'
      : null,
    /what is working|what is not|change next/.test(lower)
      ? 'Know what is working, what is not, and what changes next.'
      : null,
    /strategy, site, content, automation, and follow-up/.test(lower)
      ? 'Connect the stack so momentum starts to feel predictable.'
      : null,
    clampText(firstSentence, 86),
    theme.support
  ];

  return candidates.find(Boolean) || theme.support;
}

function buildCreativeModel({ locationId, post, businessName } = {}) {
  const summary = normalizeString(post?.summary) || normalizeString(post?.text) || '';
  const brand = extractBrand(summary, businessName, post?.businessName);
  const theme = inferCreativeTheme(summary);
  const headline = deriveCreativeHeadline(summary, theme);
  const support = deriveCreativeSupport(summary, theme, brand);
  const proofRows = uniqueCompact(theme.proofs.map((item) => item?.label ? JSON.stringify(item) : null))
    .map((item) => JSON.parse(item))
    .slice(0, 2);
  const metricRows = uniqueCompact(theme.metrics.map((item) => item?.value ? JSON.stringify(item) : null))
    .map((item) => JSON.parse(item))
    .slice(0, 2);
  const nodes = uniqueCompact(theme.nodes).slice(0, 4);

  return {
    locationId: locationId || null,
    brand,
    monogram: initials(brand),
    summary,
    themeId: theme.id,
    layout: theme.layout,
    eyebrow: theme.eyebrow,
    heroWord: theme.heroWord,
    headline,
    headlineDisplay: breakHeadline(headline, theme.layout === 'editorial' ? 13 : 15, 3),
    support: clampText(support, 92),
    proofs: proofRows,
    metrics: metricRows,
    nodes,
    palette: theme.palette,
    footerTag: `${theme.id.replace(/-/g, ' ')} • generated creative`
  };
}

function renderProofs(model) {
  return model.proofs.map((proof, index) => `
    <div class="proof-row">
      <div class="proof-index">0${index + 1}</div>
      <div>
        <div class="proof-label">${escapeHtml(proof.label)}</div>
        <div class="proof-detail">${escapeHtml(proof.detail)}</div>
      </div>
    </div>`).join('');
}

function renderMetricTiles(model, className = '') {
  return model.metrics.map((metric) => `
    <div class="metric-tile ${escapeHtml(className)}">
      <div class="metric-value">${escapeHtml(metric.value)}</div>
      <div class="metric-label">${escapeHtml(metric.label)}</div>
    </div>`).join('');
}

function renderSignalVisual(model) {
  return `
    <div class="visual-inner signal-layout">
      <div class="hero-word">${escapeHtml(model.heroWord)}</div>
      <div class="signal-grid-card major-card">
        <div class="card-topline">Operational signal</div>
        <div class="pulse-line">
          <span></span><span></span><span></span><span></span><span></span><span></span>
        </div>
        <svg class="signal-chart" viewBox="0 0 420 220" preserveAspectRatio="none" aria-hidden="true">
          <defs>
            <linearGradient id="signalStroke" x1="0%" y1="0%" x2="100%" y2="0%">
              <stop offset="0%" stop-color="${escapeHtml(model.palette.accent)}" />
              <stop offset="100%" stop-color="${escapeHtml(model.palette.accentAlt)}" />
            </linearGradient>
          </defs>
          <path d="M12 168 C70 152, 104 116, 148 120 C198 124, 214 52, 272 66 C330 80, 352 26, 408 48" fill="none" stroke="url(#signalStroke)" stroke-width="8" stroke-linecap="round" />
          <path d="M12 188 C70 176, 104 148, 148 154 C198 160, 214 102, 272 110 C330 118, 352 72, 408 88" fill="none" stroke="rgba(255,255,255,0.14)" stroke-width="4" stroke-linecap="round" />
          <circle cx="148" cy="120" r="8" fill="${escapeHtml(model.palette.accentStrong)}" />
          <circle cx="272" cy="66" r="8" fill="${escapeHtml(model.palette.accentAlt)}" />
          <circle cx="408" cy="48" r="10" fill="${escapeHtml(model.palette.accent)}" />
        </svg>
      </div>
      <div class="metrics-cluster signal-cluster">
        ${renderMetricTiles(model)}
      </div>
    </div>`;
}

function renderPipelineVisual(model) {
  const stages = model.nodes.map((node, index) => `
    <div class="stage-card stage-${index + 1}">
      <div class="stage-number">0${index + 1}</div>
      <div class="stage-label">${escapeHtml(node)}</div>
    </div>`).join('');

  return `
    <div class="visual-inner pipeline-layout">
      <div class="hero-word">${escapeHtml(model.heroWord)}</div>
      <svg class="pipeline-svg" viewBox="0 0 520 460" preserveAspectRatio="none" aria-hidden="true">
        <defs>
          <linearGradient id="pipelineStroke" x1="0%" y1="0%" x2="100%" y2="100%">
            <stop offset="0%" stop-color="${escapeHtml(model.palette.accent)}" />
            <stop offset="100%" stop-color="${escapeHtml(model.palette.accentAlt)}" />
          </linearGradient>
        </defs>
        <path d="M74 360 C126 292, 178 280, 222 214 C270 142, 334 126, 430 96" fill="none" stroke="url(#pipelineStroke)" stroke-width="14" stroke-linecap="round" />
        <path d="M74 360 C126 292, 178 280, 222 214 C270 142, 334 126, 430 96" fill="none" stroke="rgba(255,255,255,0.12)" stroke-width="28" stroke-linecap="round" opacity="0.55" />
        <circle cx="74" cy="360" r="16" fill="${escapeHtml(model.palette.accentStrong)}" />
        <circle cx="222" cy="214" r="16" fill="${escapeHtml(model.palette.accentAlt)}" />
        <circle cx="430" cy="96" r="18" fill="${escapeHtml(model.palette.accent)}" />
      </svg>
      ${stages}
      <div class="metrics-cluster pipeline-cluster">
        ${renderMetricTiles(model)}
      </div>
    </div>`;
}

function renderOrbitVisual(model) {
  const nodes = model.nodes.map((node, index) => `
    <div class="orbit-node orbit-node-${index + 1}">
      <div class="orbit-node-label">${escapeHtml(node)}</div>
    </div>`).join('');

  return `
    <div class="visual-inner orbit-layout">
      <div class="hero-word">${escapeHtml(model.heroWord)}</div>
      <div class="orbit-ring orbit-ring-a"></div>
      <div class="orbit-ring orbit-ring-b"></div>
      <div class="orbit-core">
        <div class="orbit-core-inner">${escapeHtml(model.monogram)}</div>
      </div>
      ${nodes}
      <div class="metrics-cluster orbit-cluster">
        ${renderMetricTiles(model)}
      </div>
    </div>`;
}

function renderEditorialVisual(model) {
  return `
    <div class="visual-inner editorial-layout">
      <div class="hero-word">${escapeHtml(model.heroWord)}</div>
      <div class="editorial-screen main-screen">
        <div class="screen-shell">
          <div class="screen-pill"></div>
          <div class="screen-pill short"></div>
          <div class="screen-headline">${escapeHtml(clampText(model.headline, 24) || model.headline)}</div>
          <div class="screen-bars"><span></span><span></span><span></span><span></span></div>
        </div>
      </div>
      <div class="editorial-screen side-screen">
        <div class="mini-stack">
          ${renderMetricTiles(model, 'mini')}
        </div>
      </div>
    </div>`;
}

function renderVisual(model) {
  if (model.layout === 'signal') return renderSignalVisual(model);
  if (model.layout === 'pipeline') return renderPipelineVisual(model);
  if (model.layout === 'editorial') return renderEditorialVisual(model);
  return renderOrbitVisual(model);
}

function renderHtml(model) {
  const proofs = renderProofs(model);
  const visual = renderVisual(model);

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
      --glow-a: ${model.palette.glowA};
      --glow-b: ${model.palette.glowB};
      --accent: ${model.palette.accent};
      --accent-strong: ${model.palette.accentStrong};
      --accent-alt: ${model.palette.accentAlt};
      --accent-soft: ${model.palette.accentSoft};
      --panel: ${model.palette.panel};
      --panel-strong: ${model.palette.panelStrong};
      --line: ${model.palette.line};
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg-a);
      font-family: Inter, Arial, sans-serif;
    }
    .canvas {
      width: 1080px;
      height: 1080px;
      position: relative;
      overflow: hidden;
      color: white;
      background:
        radial-gradient(circle at 18% 18%, var(--glow-a), transparent 26%),
        radial-gradient(circle at 82% 20%, var(--glow-b), transparent 28%),
        radial-gradient(circle at 78% 78%, rgba(255,255,255,0.08), transparent 20%),
        linear-gradient(140deg, var(--bg-a) 0%, var(--bg-b) 52%, var(--bg-c) 100%);
    }
    .canvas::before,
    .canvas::after {
      content: '';
      position: absolute;
      inset: 0;
      pointer-events: none;
    }
    .canvas::before {
      background-image:
        linear-gradient(rgba(255,255,255,0.05) 1px, transparent 1px),
        linear-gradient(90deg, rgba(255,255,255,0.05) 1px, transparent 1px);
      background-size: 62px 62px;
      mask-image: linear-gradient(180deg, rgba(0,0,0,0.42), transparent 84%);
      opacity: 0.28;
    }
    .canvas::after {
      background:
        repeating-linear-gradient(145deg, rgba(255,255,255,0.03) 0 2px, transparent 2px 28px);
      mix-blend-mode: soft-light;
      opacity: 0.16;
    }
    .ambient-line {
      position: absolute;
      inset: auto;
      border-radius: 999px;
      border: 1px solid rgba(255,255,255,0.08);
      opacity: 0.5;
    }
    .ambient-line.a { width: 760px; height: 760px; right: -250px; top: -170px; }
    .ambient-line.b { width: 620px; height: 620px; left: -210px; bottom: -120px; }
    .ambient-line.c { width: 460px; height: 460px; right: 160px; bottom: 110px; }
    .shell {
      position: absolute;
      inset: 34px;
      border-radius: 38px;
      overflow: hidden;
      background: linear-gradient(180deg, rgba(255,255,255,0.10), rgba(255,255,255,0.03));
      border: 1px solid rgba(255,255,255,0.14);
      box-shadow: 0 36px 90px rgba(0,0,0,0.32);
      backdrop-filter: blur(18px);
    }
    .shell::before {
      content: '';
      position: absolute;
      inset: 0;
      background: linear-gradient(135deg, rgba(255,255,255,0.12), transparent 22%, transparent 78%, rgba(255,255,255,0.06));
      opacity: 0.8;
      pointer-events: none;
    }
    .topbar {
      position: relative;
      z-index: 2;
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 28px 34px 0;
    }
    .brand-lockup {
      display: flex;
      align-items: center;
      gap: 14px;
      min-width: 0;
    }
    .brand-mark {
      width: 48px;
      height: 48px;
      border-radius: 16px;
      display: grid;
      place-items: center;
      font-size: 18px;
      font-weight: 900;
      color: var(--bg-a);
      background: linear-gradient(135deg, var(--accent), var(--accent-alt));
      box-shadow: 0 16px 34px rgba(0,0,0,0.24);
    }
    .brand-name {
      font-size: 22px;
      font-weight: 800;
      letter-spacing: 0.01em;
      max-width: 360px;
      line-height: 1.1;
    }
    .brand-sub {
      margin-top: 4px;
      color: rgba(255,255,255,0.62);
      font-size: 13px;
      text-transform: uppercase;
      letter-spacing: 0.16em;
    }
    .top-signal {
      display: inline-flex;
      align-items: center;
      gap: 10px;
      padding: 10px 16px;
      border-radius: 999px;
      background: rgba(255,255,255,0.08);
      border: 1px solid rgba(255,255,255,0.12);
      color: rgba(255,255,255,0.76);
      font-size: 12px;
      font-weight: 800;
      letter-spacing: 0.14em;
      text-transform: uppercase;
    }
    .top-signal::before {
      content: '';
      width: 10px;
      height: 10px;
      border-radius: 999px;
      background: var(--accent-strong);
      box-shadow: 0 0 18px var(--accent-strong);
    }
    .main-grid {
      position: relative;
      z-index: 2;
      display: grid;
      grid-template-columns: 0.93fr 1.07fr;
      gap: 26px;
      height: calc(100% - 140px);
      padding: 28px 34px 34px;
    }
    .copy-panel,
    .visual-panel {
      min-width: 0;
      position: relative;
    }
    .copy-panel {
      padding: 20px 16px 18px 6px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
    }
    .eyebrow {
      display: inline-flex;
      align-items: center;
      gap: 10px;
      width: fit-content;
      max-width: 100%;
      padding: 10px 16px;
      border-radius: 999px;
      background: rgba(255,255,255,0.08);
      border: 1px solid rgba(255,255,255,0.12);
      color: var(--accent);
      font-size: 16px;
      font-weight: 800;
      letter-spacing: 0.18em;
      text-transform: uppercase;
    }
    .eyebrow::before {
      content: '';
      width: 8px;
      height: 8px;
      border-radius: 999px;
      background: var(--accent-strong);
      box-shadow: 0 0 12px var(--accent-strong);
    }
    .headline {
      margin: 26px 0 0;
      font-size: 102px;
      line-height: 0.9;
      font-weight: 900;
      letter-spacing: -0.06em;
      white-space: pre-line;
      max-width: 404px;
      text-wrap: balance;
    }
    .support {
      margin: 20px 0 0;
      max-width: 390px;
      color: rgba(255,255,255,0.80);
      font-size: 28px;
      line-height: 1.34;
      letter-spacing: -0.01em;
    }
    .proof-list {
      margin-top: 38px;
      display: grid;
      gap: 12px;
      max-width: 392px;
    }
    .proof-row {
      display: grid;
      grid-template-columns: 54px 1fr;
      gap: 14px;
      align-items: start;
      padding: 14px 16px;
      border-radius: 22px;
      background: rgba(255,255,255,0.06);
      border: 1px solid rgba(255,255,255,0.10);
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.08);
    }
    .proof-index {
      font-size: 13px;
      font-weight: 900;
      letter-spacing: 0.20em;
      color: rgba(255,255,255,0.46);
      padding-top: 4px;
    }
    .proof-label {
      font-size: 22px;
      font-weight: 800;
      line-height: 1.08;
      color: rgba(255,255,255,0.96);
    }
    .proof-detail {
      margin-top: 6px;
      font-size: 15px;
      line-height: 1.35;
      color: rgba(255,255,255,0.64);
      letter-spacing: 0.01em;
    }
    .footer-tag {
      display: inline-flex;
      align-items: center;
      gap: 12px;
      padding-top: 16px;
      color: rgba(255,255,255,0.56);
      font-size: 13px;
      font-weight: 800;
      letter-spacing: 0.16em;
      text-transform: uppercase;
    }
    .footer-tag::before {
      content: '';
      width: 38px;
      height: 1px;
      background: linear-gradient(90deg, rgba(255,255,255,0), rgba(255,255,255,0.42), rgba(255,255,255,0));
    }
    .visual-panel {
      display: flex;
      align-items: stretch;
      justify-content: stretch;
      padding: 8px 0 0;
    }
    .visual-inner {
      position: relative;
      flex: 1;
      border-radius: 34px;
      background: linear-gradient(180deg, rgba(255,255,255,0.08), rgba(255,255,255,0.03));
      border: 1px solid rgba(255,255,255,0.12);
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.10);
      overflow: hidden;
      min-height: 0;
    }
    .visual-inner::before {
      content: '';
      position: absolute;
      inset: 0;
      background:
        radial-gradient(circle at 22% 22%, var(--accent-soft), transparent 26%),
        radial-gradient(circle at 78% 24%, rgba(255,255,255,0.08), transparent 20%),
        linear-gradient(135deg, rgba(255,255,255,0.08), transparent 30%, transparent 70%, rgba(255,255,255,0.06));
      opacity: 0.95;
      pointer-events: none;
    }
    .hero-word {
      position: absolute;
      top: 30px;
      right: 28px;
      font-size: 108px;
      font-weight: 900;
      letter-spacing: -0.06em;
      color: rgba(255,255,255,0.06);
      z-index: 1;
      user-select: none;
      pointer-events: none;
      text-transform: uppercase;
    }
    .metrics-cluster {
      position: absolute;
      display: grid;
      gap: 12px;
      z-index: 3;
    }
    .signal-cluster {
      right: 22px;
      bottom: 22px;
      width: 180px;
    }
    .pipeline-cluster {
      right: 22px;
      bottom: 22px;
      width: 172px;
    }
    .orbit-cluster {
      left: 22px;
      bottom: 22px;
      width: 184px;
    }
    .metric-tile {
      padding: 16px 16px 18px;
      border-radius: 24px;
      background: var(--panel);
      border: 1px solid rgba(255,255,255,0.12);
      box-shadow: 0 18px 36px rgba(0,0,0,0.22);
      backdrop-filter: blur(14px);
    }
    .metric-tile.mini {
      padding: 14px 14px 16px;
      border-radius: 20px;
      background: rgba(255,255,255,0.07);
    }
    .metric-value {
      font-size: 28px;
      line-height: 1;
      font-weight: 900;
      letter-spacing: -0.04em;
      color: rgba(255,255,255,0.98);
    }
    .metric-label {
      margin-top: 7px;
      font-size: 13px;
      line-height: 1.3;
      color: rgba(255,255,255,0.62);
      text-transform: uppercase;
      letter-spacing: 0.12em;
    }
    .major-card,
    .editorial-screen,
    .stage-card,
    .orbit-node {
      position: absolute;
      z-index: 3;
      background: var(--panel);
      border: 1px solid rgba(255,255,255,0.12);
      box-shadow: 0 22px 44px rgba(0,0,0,0.24);
      backdrop-filter: blur(16px);
    }
    .major-card {
      left: 24px;
      top: 24px;
      right: 206px;
      bottom: 26px;
      border-radius: 30px;
      padding: 24px;
    }
    .card-topline {
      font-size: 13px;
      font-weight: 800;
      letter-spacing: 0.16em;
      color: rgba(255,255,255,0.58);
      text-transform: uppercase;
    }
    .pulse-line {
      display: flex;
      align-items: center;
      gap: 10px;
      margin-top: 14px;
      opacity: 0.7;
    }
    .pulse-line span {
      height: 8px;
      border-radius: 999px;
      background: rgba(255,255,255,0.14);
      flex: 1;
    }
    .pulse-line span:nth-child(3),
    .pulse-line span:nth-child(6) {
      background: linear-gradient(90deg, var(--accent), var(--accent-alt));
    }
    .signal-chart {
      width: 100%;
      height: calc(100% - 58px);
      margin-top: 18px;
    }
    .pipeline-svg {
      position: absolute;
      inset: 0;
      z-index: 1;
      width: 100%;
      height: 100%;
    }
    .stage-card {
      width: 158px;
      min-height: 88px;
      padding: 18px;
      border-radius: 24px;
    }
    .stage-1 { left: 36px; bottom: 74px; }
    .stage-2 { left: 146px; top: 278px; }
    .stage-3 { right: 154px; top: 164px; }
    .stage-4 { right: 44px; top: 54px; }
    .stage-number {
      font-size: 12px;
      font-weight: 900;
      letter-spacing: 0.16em;
      color: rgba(255,255,255,0.46);
    }
    .stage-label {
      margin-top: 12px;
      font-size: 28px;
      line-height: 0.98;
      font-weight: 900;
      letter-spacing: -0.04em;
    }
    .orbit-ring {
      position: absolute;
      border-radius: 999px;
      border: 1px solid rgba(255,255,255,0.12);
      z-index: 1;
    }
    .orbit-ring-a {
      width: 360px;
      height: 360px;
      left: 126px;
      top: 124px;
    }
    .orbit-ring-b {
      width: 470px;
      height: 470px;
      left: 72px;
      top: 68px;
      opacity: 0.56;
    }
    .orbit-core {
      position: absolute;
      left: 208px;
      top: 206px;
      width: 196px;
      height: 196px;
      border-radius: 999px;
      display: grid;
      place-items: center;
      background: radial-gradient(circle at 32% 28%, rgba(255,255,255,0.22), transparent 34%), linear-gradient(135deg, var(--accentStrong), var(--accentAlt));
      box-shadow: 0 28px 60px rgba(0,0,0,0.28), 0 0 48px var(--accent-soft);
      z-index: 3;
    }
    .orbit-core-inner {
      width: 126px;
      height: 126px;
      border-radius: 999px;
      display: grid;
      place-items: center;
      font-size: 46px;
      font-weight: 900;
      letter-spacing: -0.04em;
      color: var(--bg-a);
      background: rgba(255,255,255,0.82);
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.46);
    }
    .orbit-node {
      padding: 16px 18px 18px;
      border-radius: 22px;
      min-width: 126px;
    }
    .orbit-node-1 { left: 76px; top: 112px; }
    .orbit-node-2 { right: 46px; top: 136px; }
    .orbit-node-3 { left: 92px; bottom: 112px; }
    .orbit-node-4 { right: 92px; bottom: 118px; }
    .orbit-node-label {
      font-size: 22px;
      line-height: 1;
      font-weight: 800;
      letter-spacing: -0.03em;
      color: rgba(255,255,255,0.96);
    }
    .editorial-screen {
      border-radius: 32px;
      overflow: hidden;
    }
    .main-screen {
      left: 28px;
      top: 84px;
      width: 380px;
      height: 468px;
      padding: 22px;
    }
    .side-screen {
      right: 24px;
      top: 186px;
      width: 168px;
      padding: 16px;
    }
    .screen-shell {
      width: 100%;
      height: 100%;
      border-radius: 24px;
      background: linear-gradient(180deg, rgba(255,255,255,0.06), rgba(255,255,255,0.02));
      border: 1px solid rgba(255,255,255,0.10);
      padding: 20px;
    }
    .screen-pill {
      width: 104px;
      height: 10px;
      border-radius: 999px;
      background: rgba(255,255,255,0.14);
    }
    .screen-pill.short {
      width: 72px;
      margin-top: 10px;
      background: linear-gradient(90deg, var(--accent), rgba(255,255,255,0.12));
    }
    .screen-headline {
      margin-top: 38px;
      max-width: 260px;
      font-size: 52px;
      line-height: 0.96;
      font-weight: 900;
      letter-spacing: -0.05em;
    }
    .screen-bars {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 10px;
      height: 160px;
      align-items: end;
      margin-top: 32px;
    }
    .screen-bars span {
      border-radius: 18px 18px 10px 10px;
      background: linear-gradient(180deg, var(--accent), rgba(255,255,255,0.08));
    }
    .screen-bars span:nth-child(1) { height: 52%; }
    .screen-bars span:nth-child(2) { height: 80%; }
    .screen-bars span:nth-child(3) { height: 66%; background: linear-gradient(180deg, var(--accentAlt), rgba(255,255,255,0.08)); }
    .screen-bars span:nth-child(4) { height: 100%; }
    .mini-stack {
      display: grid;
      gap: 12px;
    }
  </style>
</head>
<body>
  <main class="canvas">
    <div class="ambient-line a"></div>
    <div class="ambient-line b"></div>
    <div class="ambient-line c"></div>
    <section class="shell">
      <header class="topbar">
        <div class="brand-lockup">
          <div class="brand-mark">${escapeHtml(model.monogram)}</div>
          <div>
            <div class="brand-name">${escapeHtml(model.brand)}</div>
            <div class="brand-sub">premium social creative</div>
          </div>
        </div>
        <div class="top-signal">system-led design</div>
      </header>
      <div class="main-grid">
        <section class="copy-panel">
          <div>
            <div class="eyebrow">${escapeHtml(model.eyebrow)}</div>
            <h1 class="headline">${escapeHtml(model.headlineDisplay)}</h1>
            <div class="support">${escapeHtml(model.support)}</div>
            <div class="proof-list">${proofs}</div>
          </div>
          <div class="footer-tag">${escapeHtml(model.footerTag)}</div>
        </section>
        <section class="visual-panel">
          ${visual}
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
  const baseDir = path.resolve(outputDir || path.join(process.cwd(), 'data', 'generated', 'creatives'));
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
