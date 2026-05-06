import crypto from 'node:crypto';
import { GHL_PUBLIC_KEY, LEGACY_RSA_PUBLIC_KEY } from '../../config/constants.js';

export function computePayloadHash(payload) {
  const serialized = typeof payload === 'string' ? payload : JSON.stringify(payload);
  return crypto.createHash('sha256').update(serialized).digest('hex');
}

export function verifyWebhookSignature({ payload, headers = {}, publicKey = null, legacyPublicKey = null }) {
  const serialized = typeof payload === 'string' ? payload : JSON.stringify(payload);
  const ghlSignature = headers['x-ghl-signature'] || headers['X-GHL-Signature'];
  const legacySignature = headers['x-wh-signature'] || headers['X-WH-Signature'];

  if (ghlSignature) {
    return verifyGhl(serialized, ghlSignature, publicKey || GHL_PUBLIC_KEY);
  }

  if (legacySignature) {
    return verifyLegacy(serialized, legacySignature, legacyPublicKey || LEGACY_RSA_PUBLIC_KEY);
  }

  return {
    ok: false,
    reason: 'missing_signature'
  };
}

function verifyGhl(payload, signature, publicKeyPem) {
  try {
    const payloadBuffer = Buffer.from(payload, 'utf8');
    const signatureBuffer = Buffer.from(signature, 'base64');
    const ok = crypto.verify(null, payloadBuffer, publicKeyPem, signatureBuffer);
    return { ok, algorithm: 'ed25519', reason: ok ? null : 'verify_failed' };
  } catch (error) {
    return { ok: false, algorithm: 'ed25519', reason: error.message };
  }
}

function verifyLegacy(payload, signature, publicKeyPem) {
  try {
    const verifier = crypto.createVerify('SHA256');
    verifier.update(payload);
    const ok = verifier.verify(publicKeyPem, signature, 'base64');
    return { ok, algorithm: 'rsa-sha256', reason: ok ? null : 'verify_failed' };
  } catch (error) {
    return { ok: false, algorithm: 'rsa-sha256', reason: error.message };
  }
}

export function normalizeWebhookEvent(payload) {
  return {
    webhookId: payload?.webhookId || null,
    type: payload?.type || null,
    companyId: payload?.companyId || payload?.company_id || null,
    locationId: payload?.locationId || payload?.location_id || payload?.data?.locationId || null,
    objectId: payload?.id || payload?.data?.id || null,
    payload
  };
}

export function routeWebhookEvent({ eventType, locationId }) {
  if (!eventType || !locationId) return 'ghl-agency-lead';

  if (/^Contact|^Note|^Task|ContactTag/.test(eventType)) return `ghl-contacts-agent-${locationId}`;
  if (/^Opportunity/.test(eventType)) return `ghl-sales-pipeline-agent-${locationId}`;
  if (/^Conversation|^InboundMessage|^OutboundMessage|^ProviderOutboundMessage/.test(eventType)) return `ghl-conversations-agent-${locationId}`;
  if (/^Appointment/.test(eventType)) return `ghl-calendar-agent-${locationId}`;
  if (/^Invoice|^Order|^Product|^Price/.test(eventType)) return `ghl-payments-agent-${locationId}`;
  if (/^VoiceAi/.test(eventType)) return `ghl-voice-ai-agent-${locationId}`;
  if (/^User/.test(eventType)) return `ghl-compliance-audit-agent-${locationId}`;
  if (/^App|^Location|^Plan/.test(eventType)) return 'ghl-agency-lead';
  return `ghl-sub-account-agent-${locationId}`;
}
