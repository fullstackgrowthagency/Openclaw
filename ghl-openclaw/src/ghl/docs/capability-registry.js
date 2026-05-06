import path from 'node:path';
import { getEnv } from '../../config/env.js';
import { readJson, writeJson } from '../../lib/fs.js';
import { fetchDoc } from './fetch.js';
import { toDocSnapshotRecord } from './parse.js';
import {
  NOT_PUBLICLY_API_SUPPORTED,
  VERIFIED_DOC_SOURCES,
  VERIFIED_ENDPOINT_SEEDS
} from './sources.js';

function buildSeedCapability(seed) {
  return {
    category: seed.category,
    resource: seed.category,
    doc_url: null,
    path: seed.path,
    method: seed.method,
    api_version_header: '2023-02-21',
    auth_methods: ['oauth_2', 'private_integration_token'],
    token_types: seed.tokenTypes,
    scope: seed.scope,
    access_level: seed.accessLevel,
    crud_support: crudSupport(seed.method),
    webhook_events: [],
    request_schema_ref: null,
    response_schema_ref: null,
    pagination_mode: inferPagination(seed.path, seed.method),
    rate_limit_class: 'oauth_v2_per_app_per_resource',
    plan_or_feature_notes: [],
    status: seed.status,
    last_verified_at: new Date().toISOString(),
    doc_fingerprint: null,
    provenance: 'verified_seed'
  };
}

function buildDocCapabilities(snapshot) {
  return snapshot.endpoints.map((endpoint) => ({
    category: snapshot.category,
    resource: snapshot.resource,
    doc_url: snapshot.url,
    path: endpoint.path,
    method: endpoint.method,
    api_version_header: snapshot.version || '2023-02-21',
    auth_methods: snapshot.resource === 'private_integration_tokens' ? ['private_integration_token'] : ['oauth_2', 'private_integration_token'],
    token_types: snapshot.tokenTypes,
    scope: snapshot.scopeHint,
    access_level: snapshot.accessLevel,
    crud_support: crudSupport(endpoint.method),
    webhook_events: [],
    request_schema_ref: `${snapshot.resource}:request:${endpoint.method}:${endpoint.path}`,
    response_schema_ref: `${snapshot.resource}:response:${endpoint.method}:${endpoint.path}`,
    pagination_mode: inferPagination(endpoint.path, endpoint.method),
    rate_limit_class: snapshot.rateLimitHints.length ? 'oauth_v2_per_app_per_resource' : 'unknown',
    plan_or_feature_notes: [],
    status: snapshot.status,
    last_verified_at: snapshot.fetchedAt,
    doc_fingerprint: snapshot.fingerprint,
    provenance: 'live_doc_crawl'
  }));
}

function crudSupport(method) {
  return {
    create: method === 'POST',
    read: method === 'GET',
    update: method === 'PUT' || method === 'PATCH',
    delete: method === 'DELETE'
  };
}

function inferPagination(endpointPath, method) {
  if (method !== 'GET') return 'none';
  return endpointPath.endsWith('/') || endpointPath.includes('/search') ? 'cursor_or_page_unknown' : 'none';
}

function mergeCapabilities(capabilities) {
  const byKey = new Map();
  for (const capability of capabilities) {
    const key = `${capability.method} ${capability.path}`;
    const existing = byKey.get(key);
    if (!existing) {
      byKey.set(key, capability);
      continue;
    }

    byKey.set(key, {
      ...existing,
      ...capability,
      doc_url: capability.doc_url || existing.doc_url,
      doc_fingerprint: capability.doc_fingerprint || existing.doc_fingerprint,
      provenance: existing.provenance === 'live_doc_crawl' ? existing.provenance : capability.provenance
    });
  }

  return [...byKey.values()].sort((a, b) => `${a.category}:${a.method}:${a.path}`.localeCompare(`${b.category}:${b.method}:${b.path}`));
}

export async function refreshCapabilityRegistry() {
  const env = getEnv();
  const snapshotPath = path.join(env.dataDir, 'snapshots', 'ghl-doc-snapshots.json');
  const registryPath = path.join(env.dataDir, 'generated', 'capability-registry.json');
  const statusPath = path.join(env.dataDir, 'generated', 'status.json');

  const snapshots = [];
  const fetchErrors = [];

  for (const source of VERIFIED_DOC_SOURCES) {
    try {
      const doc = await fetchDoc(source.url);
      snapshots.push(toDocSnapshotRecord(source, doc));
    } catch (error) {
      fetchErrors.push({ url: source.url, message: error.message });
    }
  }

  const previousSnapshots = await readJson(snapshotPath, []);
  const effectiveSnapshots = snapshots.length ? snapshots : previousSnapshots;

  const seedCapabilities = VERIFIED_ENDPOINT_SEEDS.map(buildSeedCapability);
  const docCapabilities = effectiveSnapshots.flatMap(buildDocCapabilities);
  const capabilities = mergeCapabilities([...seedCapabilities, ...docCapabilities]);

  const output = {
    generatedAt: new Date().toISOString(),
    docsVersion: effectiveSnapshots.find((item) => item.version)?.version || '2023-02-21',
    rateLimits: {
      burst: '100 API requests per 10 seconds per marketplace app per resource',
      daily: '200,000 API requests per day per marketplace app per resource'
    },
    unsupported: NOT_PUBLICLY_API_SUPPORTED,
    fetchErrors,
    sourceCount: VERIFIED_DOC_SOURCES.length,
    liveSnapshotCount: snapshots.length,
    fallbackSnapshotCount: previousSnapshots.length,
    capabilities
  };

  await writeJson(snapshotPath, effectiveSnapshots);
  await writeJson(registryPath, output);
  await writeJson(statusPath, {
    generatedAt: output.generatedAt,
    docsVersion: output.docsVersion,
    capabilityCount: capabilities.length,
    fetchErrors,
    liveSnapshotCount: snapshots.length,
    usingFallbackSnapshots: !snapshots.length && previousSnapshots.length > 0
  });

  return output;
}
