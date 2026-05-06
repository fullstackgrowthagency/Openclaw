import { computePayloadHash, normalizeWebhookEvent } from './router.js';
import { WebhookStore } from './store.js';

export async function ingestTestEvent(payload, { store = new WebhookStore() } = {}) {
  const normalizedEvent = normalizeWebhookEvent(payload);
  const payloadHash = computePayloadHash(payload);
  const { record, duplicate } = await store.recordReceipt({
    normalizedEvent,
    payloadHash,
    verification: { ok: true, algorithm: 'internal-test', reason: null },
    headers: { 'x-openclaw-test': 'true' }
  });

  if (!duplicate) {
    await store.enqueueEvent(record);
  }

  return {
    ok: true,
    duplicate: Boolean(duplicate),
    eventId: record.id,
    webhookId: normalizedEvent.webhookId,
    eventType: normalizedEvent.type
  };
}
