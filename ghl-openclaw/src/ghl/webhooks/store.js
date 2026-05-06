import path from 'node:path';
import crypto from 'node:crypto';
import { getEnv } from '../../config/env.js';
import { readJson, writeJson } from '../../lib/fs.js';

function nowIso() {
  return new Date().toISOString();
}

function createId(prefix = 'evt') {
  return `${prefix}_${crypto.randomUUID()}`;
}

function initialState() {
  return {
    events: [],
    queue: []
  };
}

export class WebhookStore {
  constructor() {
    const env = getEnv();
    this.filePath = path.join(env.dataDir, 'webhooks', 'state.json');
  }

  async load() {
    return (await readJson(this.filePath, initialState())) || initialState();
  }

  async save(state) {
    await writeJson(this.filePath, state);
  }

  async summary() {
    const state = await this.load();
    return {
      eventCount: state.events.length,
      queueCount: state.queue.length,
      pendingCount: state.queue.filter((item) => item.status === 'pending').length,
      processingCount: state.queue.filter((item) => item.status === 'processing').length,
      processedCount: state.queue.filter((item) => item.status === 'processed').length,
      failedCount: state.queue.filter((item) => item.status === 'failed').length,
      deadLetterCount: state.queue.filter((item) => item.status === 'dead_letter').length,
      duplicateCount: state.events.filter((item) => item.duplicate).length
    };
  }

  async findDuplicate({ webhookId, payloadHash }) {
    const state = await this.load();
    return state.events.find((event) => {
      if (webhookId && event.webhookId && event.webhookId === webhookId) return true;
      return event.payloadHash === payloadHash;
    }) || null;
  }

  async recordReceipt({ normalizedEvent, payloadHash, verification, headers }) {
    const state = await this.load();
    const duplicate = state.events.find((event) => {
      if (normalizedEvent.webhookId && event.webhookId === normalizedEvent.webhookId) return true;
      return event.payloadHash === payloadHash;
    });

    const record = {
      id: createId('evt'),
      webhookId: normalizedEvent.webhookId,
      type: normalizedEvent.type,
      companyId: normalizedEvent.companyId,
      locationId: normalizedEvent.locationId,
      objectId: normalizedEvent.objectId,
      payloadHash,
      verification,
      receivedAt: nowIso(),
      duplicate: Boolean(duplicate),
      status: duplicate ? 'duplicate' : 'received',
      headers: redactHeaders(headers),
      payload: normalizedEvent.payload
    };

    state.events.push(record);
    await this.save(state);
    return { record, duplicate };
  }

  async enqueueEvent(eventRecord) {
    const state = await this.load();
    const existing = state.queue.find((item) => item.eventId === eventRecord.id);
    if (existing) return existing;

    const queueItem = {
      id: createId('q'),
      eventId: eventRecord.id,
      webhookId: eventRecord.webhookId,
      type: eventRecord.type,
      locationId: eventRecord.locationId,
      companyId: eventRecord.companyId,
      status: 'pending',
      attemptCount: 0,
      createdAt: nowIso(),
      updatedAt: nowIso(),
      lastError: null,
      dispatchedAgentId: null,
      taskPack: null,
      result: null
    };

    state.queue.push(queueItem);
    await this.save(state);
    return queueItem;
  }

  async claimNextPending() {
    const state = await this.load();
    const next = state.queue.find((item) => item.status === 'pending');
    if (!next) return null;

    next.status = 'processing';
    next.attemptCount += 1;
    next.updatedAt = nowIso();
    await this.save(state);

    const event = state.events.find((item) => item.id === next.eventId) || null;
    return { queueItem: next, event };
  }

  async markProcessed(queueId, result) {
    const state = await this.load();
    const queueItem = state.queue.find((item) => item.id === queueId);
    if (!queueItem) return null;

    queueItem.status = 'processed';
    queueItem.result = result;
    queueItem.lastError = null;
    queueItem.updatedAt = nowIso();

    const event = state.events.find((item) => item.id === queueItem.eventId);
    if (event) {
      event.status = 'processed';
      event.processedAt = nowIso();
    }

    await this.save(state);
    return queueItem;
  }

  async markFailed(queueId, errorMessage, { retryable = true, maxAttempts = 5 } = {}) {
    const state = await this.load();
    const queueItem = state.queue.find((item) => item.id === queueId);
    if (!queueItem) return null;

    const terminal = !retryable || queueItem.attemptCount >= maxAttempts;
    queueItem.status = terminal ? 'dead_letter' : 'pending';
    queueItem.lastError = errorMessage;
    queueItem.updatedAt = nowIso();

    const event = state.events.find((item) => item.id === queueItem.eventId);
    if (event) {
      event.status = terminal ? 'dead_letter' : 'retry_pending';
      event.lastError = errorMessage;
    }

    await this.save(state);
    return queueItem;
  }
}

function redactHeaders(headers = {}) {
  const redacted = {};
  for (const [key, value] of Object.entries(headers)) {
    const lower = key.toLowerCase();
    if (lower === 'authorization') {
      redacted[key] = '[redacted]';
      continue;
    }
    redacted[key] = value;
  }
  return redacted;
}
