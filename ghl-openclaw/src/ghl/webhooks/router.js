import crypto from 'node:crypto';

export function computePayloadHash(payload) {
  const serialized = typeof payload === 'string' ? payload : JSON.stringify(payload);
  return crypto.createHash('sha256').update(serialized).digest('hex');
}

export function verifyWebhookSignature({ payload, signature, publicKey }) {
  if (!signature || !publicKey) {
    return { ok: false, reason: 'missing_signature_or_public_key' };
  }

  return {
    ok: true,
    reason: 'verification_stub_only'
  };
}

export function routeWebhookEvent({ eventType, locationId }) {
  if (!eventType) return 'ghl-agency-lead';

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
