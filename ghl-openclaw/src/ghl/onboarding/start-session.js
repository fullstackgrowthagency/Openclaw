import path from 'node:path';
import { readJson, writeJson } from '../../lib/fs.js';
import { FileEncryptedStore } from '../auth/encrypted-store.js';
import { StartOnboardingService, normalizeStartPayload, missingRequiredFields } from './start-service.js';

const REQUIRED_FIELDS = ['openaiApiKey', 'businessName', 'knowledgeBase', 'logo', 'website', 'ghlAccess'];

function normalizePlainObject(value) {
  return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
}

function resolveWorkspaceRoot(startService) {
  return startService?.workspaceRoot || path.resolve(process.cwd(), '..');
}

function hasKnowledgeBase(value = {}) {
  return Boolean(value.ref || value.location || value.notes);
}

function buildPrompt() {
  return [
    'Summary: I’m setting up your active business workspace now. We’ll do one business at a time, and I need the business knowledge base before I build anything.',
    '',
    'Please send:',
    '- OpenAI API key',
    '- Business name',
    '- Knowledge base or source-of-truth docs',
    '- Logo',
    '- Website/domain',
    '- GHL access details'
  ].join('\n');
}

function mergeSession(current = {}, incoming = {}) {
  const next = {
    businessName: incoming.businessName ?? current.businessName ?? null,
    knowledgeBase: {
      ...(current.knowledgeBase || {}),
      ...(incoming.knowledgeBase || {})
    },
    logo: incoming.logo ?? current.logo ?? null,
    website: incoming.website ?? current.website ?? null,
    ghl: {
      ...(current.ghl || {}),
      ...(incoming.ghl || {})
    },
    optional: {
      ...(current.optional || {}),
      ...(incoming.optional || {})
    }
  };
  return next;
}

function summarizeCollected(session = {}, secretFlags = {}) {
  return {
    openaiApiKey: Boolean(secretFlags.openaiApiKey),
    businessName: Boolean(session.businessName),
    knowledgeBase: hasKnowledgeBase(session.knowledgeBase),
    logo: Boolean(session.logo),
    website: Boolean(session.website),
    ghlAccess: Boolean(secretFlags.ghlPitToken || session?.ghl?.credentialRef),
    ghlLocationId: Boolean(session?.ghl?.locationId)
  };
}

function composePayload(session = {}, secrets = {}) {
  return {
    businessName: session.businessName || null,
    knowledgeBase: session.knowledgeBase || {},
    logo: session.logo || null,
    website: session.website || null,
    ghl: session.ghl || {},
    optional: session.optional || {},
    ...(secrets.openaiApiKey ? { openaiApiKey: secrets.openaiApiKey } : {}),
    ...(secrets.ghlPitToken ? { ghl: { ...(session.ghl || {}), pitToken: secrets.ghlPitToken } } : {})
  };
}

export class StartSessionService {
  constructor({
    encryptedStore = new FileEncryptedStore(),
    startOnboardingService = new StartOnboardingService({ encryptedStore }),
    workspaceRoot = resolveWorkspaceRoot(startOnboardingService)
  } = {}) {
    this.encryptedStore = encryptedStore;
    this.startOnboardingService = startOnboardingService;
    this.workspaceRoot = workspaceRoot;
    this.sessionPath = path.join(workspaceRoot, 'business', 'ACTIVE_START_SESSION.json');
    this.secretRef = 'start-session-active';
  }

  async begin(extra = {}) {
    const now = new Date().toISOString();
    const session = {
      status: 'collecting',
      startedAt: now,
      updatedAt: now,
      requiredFields: REQUIRED_FIELDS,
      collected: summarizeCollected({}, {}),
      missingRequirements: REQUIRED_FIELDS,
      prompt: buildPrompt(),
      notes: normalizePlainObject(extra)
    };
    await writeJson(this.sessionPath, session);
    return {
      ok: true,
      status: session.status,
      prompt: session.prompt,
      session
    };
  }

  async status() {
    const session = await readJson(this.sessionPath, null);
    if (!session || session.status === 'idle') {
      return { ok: true, active: false, session };
    }
    return { ok: true, active: true, session };
  }

  async ingest(input = {}) {
    const current = await readJson(this.sessionPath, null);
    const normalized = normalizeStartPayload(input);
    const sessionData = mergeSession(current?.data || {}, normalized);

    const existingSecretRecord = await this.encryptedStore.getSecret(this.secretRef);
    const existingSecrets = existingSecretRecord?.secret || {};
    const nextSecrets = {
      ...existingSecrets,
      ...(normalized.openaiApiKey ? { openaiApiKey: normalized.openaiApiKey } : {}),
      ...(normalized.ghl?.pitToken ? { ghlPitToken: normalized.ghl.pitToken } : {})
    };

    if (nextSecrets.openaiApiKey || nextSecrets.ghlPitToken) {
      await this.encryptedStore.upsert(this.secretRef, {
        kind: 'start_session_secret',
        tokenType: 'onboarding_secret',
        scopes: [],
        secret: nextSecrets
      });
    }

    const composed = composePayload(sessionData, nextSecrets);
    const payload = normalizeStartPayload(composed);
    const missing = missingRequiredFields(payload);
    const collected = summarizeCollected(sessionData, nextSecrets);
    const now = new Date().toISOString();

    if (missing.length === 0) {
      const connection = await this.startOnboardingService.connect(payload);
      await this.reset();
      return {
        ok: true,
        status: 'connected',
        missingRequirements: [],
        collected,
        connection
      };
    }

    const session = {
      status: 'collecting',
      startedAt: current?.startedAt || now,
      updatedAt: now,
      requiredFields: REQUIRED_FIELDS,
      collected,
      missingRequirements: missing,
      prompt: buildPrompt(),
      data: sessionData
    };
    await writeJson(this.sessionPath, session);

    return {
      ok: false,
      status: 'collecting',
      missingRequirements: missing,
      collected,
      session
    };
  }

  async reset() {
    await writeJson(this.sessionPath, {
      status: 'idle',
      clearedAt: new Date().toISOString()
    });
    await this.encryptedStore.delete(this.secretRef);
    return { ok: true, status: 'idle' };
  }
}
