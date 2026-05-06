import { sha256 } from '../../lib/hash.js';

const METHOD_PATH_RE = /\b(GET|POST|PUT|DELETE|PATCH)\s*<[^>]*>\s*\/?([^<\n]+)/gi;
const METHOD_PATH_FALLBACK_RE = /\b(GET|POST|PUT|DELETE|PATCH)\b\s*(\/[-a-zA-Z0-9_:\/{}.]*)/g;
const VERSION_RE = /Version:?\s*([0-9]{4}-[0-9]{2}-[0-9]{2})/i;
const RATE_LIMIT_RE = /(100 API requests per 10 seconds|200,000 API requests per day)/gi;

export function extractDocVersion(body) {
  const match = body.match(VERSION_RE);
  return match ? match[1] : null;
}

export function extractEndpoints(body) {
  const found = new Map();

  for (const regex of [METHOD_PATH_RE, METHOD_PATH_FALLBACK_RE]) {
    for (const match of body.matchAll(regex)) {
      const method = match[1]?.trim().toUpperCase();
      const path = normalizePath(match[2]);
      if (!method || !path || !path.startsWith('/')) continue;
      found.set(`${method} ${path}`, { method, path });
    }
  }

  return [...found.values()];
}

export function extractRateLimitHints(body) {
  return [...new Set((body.match(RATE_LIMIT_RE) || []).map((value) => value.trim()))];
}

export function toDocSnapshotRecord(source, doc) {
  return {
    category: source.category,
    resource: source.resource,
    url: source.url,
    accessLevel: source.accessLevel,
    tokenTypes: source.tokenTypes,
    scopeHint: source.scopeHint,
    status: source.status,
    fetchedAt: doc.fetchedAt,
    statusCode: doc.statusCode,
    fingerprint: doc.fingerprint,
    version: extractDocVersion(doc.body),
    endpoints: extractEndpoints(doc.body),
    rateLimitHints: extractRateLimitHints(doc.body),
    bodyHash: sha256(doc.body)
  };
}

function normalizePath(value = '') {
  return value
    .replace(/<[^>]+>/g, '')
    .replace(/^#+\s*/, '')
    .replace(/[`\s]+/g, ' ')
    .trim()
    .split(' ')[0]
    .replace(/\/$/, '/')
    .trim();
}
