import { existsSync } from 'node:fs';
import path from 'node:path';
import { readJson, writeJson } from '../../lib/fs.js';
import { FileEncryptedStore } from '../auth/encrypted-store.js';
import { CredentialBroker } from '../auth/credential-broker.js';
import { GhlApiClient } from '../api/client.js';

function normalizeString(value) {
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

function normalizeStringArray(values) {
  return Array.isArray(values) ? values.map((value) => normalizeString(value)).filter(Boolean) : [];
}

function normalizePlainObject(value) {
  return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
}

function slugify(value) {
  return String(value || '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 60) || 'business';
}

function maskSecret(value) {
  const normalized = normalizeString(value);
  if (!normalized) return null;
  if (normalized.length <= 8) return `${normalized.slice(0, 2)}***${normalized.slice(-1)}`;
  return `${normalized.slice(0, 4)}***${normalized.slice(-4)}`;
}

function extractErrorMessage(payload, fallback) {
  if (!payload) return fallback;
  if (typeof payload === 'string') return payload;
  return payload?.error?.message
    || payload?.message
    || payload?.error_description
    || payload?.error
    || fallback;
}

function resolveWorkspaceRoot(fromDir = process.cwd()) {
  let current = path.resolve(fromDir);
  for (let depth = 0; depth < 4; depth += 1) {
    const candidate = path.join(current, 'START_PROTOCOL.md');
    if (existsSync(candidate)) return current;
    const parent = path.dirname(current);
    if (parent === current) break;
    current = parent;
  }
  return path.resolve(fromDir, '..');
}

function buildKnowledgeBaseRecord(input = {}) {
  const knowledgeBase = normalizePlainObject(input.knowledgeBase || input.kb || {});
  const ref = normalizeString(input.knowledgeBaseRef)
    || normalizeString(input.knowledgeBaseId)
    || normalizeString(knowledgeBase.ref)
    || normalizeString(knowledgeBase.id)
    || null;
  const type = normalizeString(input.knowledgeBaseType)
    || normalizeString(knowledgeBase.type)
    || null;
  const location = normalizeString(input.knowledgeBaseLocation)
    || normalizeString(input.knowledgeBaseUrl)
    || normalizeString(knowledgeBase.location)
    || normalizeString(knowledgeBase.url)
    || null;
  const notes = normalizeString(input.knowledgeBaseNotes)
    || normalizeString(knowledgeBase.notes)
    || null;
  return { ref, type, location, notes };
}

function normalizeStartPayload(input = {}) {
  const payload = normalizePlainObject(input);
  const ghl = normalizePlainObject(payload.ghl || payload.ghlAccess || {});
  return {
    businessName: normalizeString(payload.businessName) || normalizeString(payload.name) || null,
    openaiApiKey: normalizeString(payload.openaiApiKey) || normalizeString(payload.openaiKey) || null,
    knowledgeBase: buildKnowledgeBaseRecord(payload),
    logo: normalizeString(payload.logo) || normalizeString(payload.logoUrl) || null,
    website: normalizeString(payload.website) || normalizeString(payload.domain) || null,
    ghl: {
      credentialRef: normalizeString(ghl.credentialRef) || normalizeString(payload.ghlCredentialRef) || null,
      pitToken: normalizeString(ghl.pitToken) || normalizeString(payload.ghlPitToken) || normalizeString(payload.ghlAccessToken) || null,
      tokenType: normalizeString(ghl.tokenType) || 'private_integration_token',
      locationId: normalizeString(ghl.locationId) || normalizeString(payload.locationId) || null,
      subaccountId: normalizeString(ghl.subaccountId) || normalizeString(payload.subaccountId) || null,
      companyId: normalizeString(ghl.companyId) || normalizeString(payload.companyId) || null,
      scopes: normalizeStringArray(ghl.scopes || payload.ghlScopes)
    },
    optional: {
      kpis: normalizeStringArray(payload?.optional?.kpis || payload.kpis),
      paymentProducts: normalizeStringArray(payload?.optional?.paymentProducts || payload.paymentProducts || payload.products)
    }
  };
}

function missingRequiredFields(payload) {
  const missing = [];
  if (!payload.openaiApiKey) missing.push('openaiApiKey');
  if (!payload.businessName) missing.push('businessName');
  if (!payload.knowledgeBase.ref && !payload.knowledgeBase.location && !payload.knowledgeBase.notes) missing.push('knowledgeBase');
  if (!payload.logo) missing.push('logo');
  if (!payload.website) missing.push('website');
  if (!payload.ghl.credentialRef && !payload.ghl.pitToken) missing.push('ghlAccess');
  if (payload.ghl.pitToken && !payload.ghl.locationId) missing.push('ghlLocationId');
  return missing;
}

export class StartOnboardingService {
  constructor({
    encryptedStore = new FileEncryptedStore(),
    credentialBroker = new CredentialBroker({ encryptedStore }),
    ghlClient = new GhlApiClient({ credentialBroker }),
    fetchImpl = fetch,
    workspaceRoot = resolveWorkspaceRoot(process.cwd())
  } = {}) {
    this.encryptedStore = encryptedStore;
    this.credentialBroker = credentialBroker;
    this.ghlClient = ghlClient;
    this.fetchImpl = fetchImpl;
    this.workspaceRoot = workspaceRoot;
    this.templatePath = path.join(workspaceRoot, 'business', 'ACTIVE_BUSINESS.template.json');
    this.activeBusinessPath = path.join(workspaceRoot, 'business', 'ACTIVE_BUSINESS.json');
  }

  async connect(input = {}) {
    const payload = normalizeStartPayload(input);
    const missing = missingRequiredFields(payload);
    if (missing.length > 0) {
      return {
        ok: false,
        status: 'incomplete',
        missingRequirements: missing,
        message: 'Missing required /start fields.'
      };
    }

    this.encryptedStore.assertReady();

    const openai = await this.connectOpenAi(payload);
    const ghl = await this.connectGhl(payload);
    const activeBusiness = await this.writeActiveBusiness(payload, { openai, ghl });

    return {
      ok: true,
      status: 'connected',
      businessName: payload.businessName,
      connections: {
        openai,
        ghl
      },
      activeBusiness
    };
  }

  async connectOpenAi(payload) {
    const validation = await this.validateOpenAiKey(payload.openaiApiKey);
    if (!validation.ok) {
      throw new Error(`OpenAI validation failed: ${validation.error}`);
    }

    const credentialRef = `openai-${slugify(payload.businessName)}`;
    const metadata = await this.encryptedStore.upsert(credentialRef, {
      kind: 'openai_api_key',
      tokenType: 'api_key',
      scopes: [],
      secret: {
        provider: 'openai',
        apiKey: payload.openaiApiKey,
        validatedAt: new Date().toISOString(),
        source: 'start_onboarding'
      }
    });

    return {
      credentialRef,
      kind: metadata.kind,
      validated: true,
      status: validation.status,
      keyPreview: maskSecret(payload.openaiApiKey)
    };
  }

  async validateOpenAiKey(apiKey) {
    const response = await this.fetchImpl('https://api.openai.com/v1/models?limit=1', {
      method: 'GET',
      headers: {
        Accept: 'application/json',
        Authorization: `Bearer ${apiKey}`
      }
    });

    const text = await response.text();
    let data;
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }

    return {
      ok: response.ok,
      status: response.status,
      error: response.ok ? null : extractErrorMessage(data, `HTTP ${response.status}`)
    };
  }

  async connectGhl(payload) {
    if (payload.ghl.credentialRef) {
      const metadata = await this.credentialBroker.resolveCredentialMetadata(payload.ghl.credentialRef);
      if (!metadata) {
        throw new Error(`GHL credentialRef not found: ${payload.ghl.credentialRef}`);
      }
      const validatedLocationId = payload.ghl.locationId || metadata.locationId || null;
      if (validatedLocationId) {
        await this.validateGhlCredential(payload.ghl.credentialRef, validatedLocationId);
      }
      return {
        credentialRef: payload.ghl.credentialRef,
        source: 'existing_credential_ref',
        locationId: validatedLocationId,
        companyId: metadata.companyId || payload.ghl.companyId || null,
        validated: Boolean(validatedLocationId),
        kind: metadata.kind
      };
    }

    const credentialRef = payload.ghl.locationId
      ? `location-${payload.ghl.locationId}-pit`
      : `ghl-${slugify(payload.businessName)}-pit`;
    const metadata = await this.credentialBroker.storePit({
      credentialRef,
      token: payload.ghl.pitToken,
      tokenType: payload.ghl.tokenType,
      companyId: payload.ghl.companyId,
      locationId: payload.ghl.locationId,
      scopes: payload.ghl.scopes
    });

    await this.validateGhlCredential(credentialRef, payload.ghl.locationId);

    return {
      credentialRef,
      source: 'pit_token',
      locationId: payload.ghl.locationId,
      companyId: payload.ghl.companyId || null,
      validated: true,
      kind: metadata.kind,
      keyPreview: maskSecret(payload.ghl.pitToken)
    };
  }

  async validateGhlCredential(credentialRef, locationId) {
    const response = locationId
      ? await this.ghlClient.request({ credentialRef, method: 'GET', path: `/locations/${locationId}` })
      : await this.ghlClient.request({ credentialRef, method: 'GET', path: '/locations/search', query: { limit: 1 } });

    if (!response.ok) {
      throw new Error(`GHL validation failed: ${extractErrorMessage(response.data, `HTTP ${response.status}`)}`);
    }

    return {
      ok: true,
      status: response.status
    };
  }

  async writeActiveBusiness(payload, { openai, ghl }) {
    const template = await readJson(this.templatePath, {});
    const existing = await readJson(this.activeBusinessPath, {});
    const now = new Date().toISOString();

    const next = {
      ...template,
      ...existing,
      businessName: payload.businessName,
      knowledgeBase: {
        ...(template.knowledgeBase || {}),
        ...(existing.knowledgeBase || {}),
        ...payload.knowledgeBase
      },
      brand: {
        ...(template.brand || {}),
        ...(existing.brand || {}),
        logo: payload.logo,
        website: payload.website
      },
      access: {
        ...(template.access || {}),
        ...(existing.access || {}),
        openai: {
          ...(template.access?.openai || {}),
          ...(existing.access?.openai || {}),
          credentialRef: openai.credentialRef,
          apiKeyProvided: true,
          notes: 'Validated and stored in encrypted credential storage.'
        },
        ghl: {
          ...(template.access?.ghl || {}),
          ...(existing.access?.ghl || {}),
          locationId: ghl.locationId || payload.ghl.locationId || null,
          subaccountId: payload.ghl.subaccountId || null,
          credentialRef: ghl.credentialRef,
          accessProvided: true,
          notes: 'Validated and stored in encrypted credential storage.'
        }
      },
      optional: {
        ...(template.optional || {}),
        ...(existing.optional || {}),
        kpis: payload.optional.kpis,
        paymentProducts: payload.optional.paymentProducts
      },
      meta: {
        ...(template.meta || {}),
        ...(existing.meta || {}),
        active: true,
        singleBusinessMode: true,
        updatedAt: now,
        onboardingStatus: 'connected'
      }
    };

    await writeJson(this.activeBusinessPath, next);

    return {
      path: this.activeBusinessPath,
      businessName: next.businessName,
      openaiCredentialRef: next.access?.openai?.credentialRef || null,
      ghlCredentialRef: next.access?.ghl?.credentialRef || null,
      locationId: next.access?.ghl?.locationId || null,
      onboardingStatus: next.meta?.onboardingStatus || null,
      updatedAt: next.meta?.updatedAt || null
    };
  }
}
