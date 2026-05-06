import { routeWebhookEvent } from './router.js';
import { WebhookStore } from './store.js';
import { TaskPackExecutor } from '../taskpacks/executor.js';

function inferTaskPack(eventType = '') {
  if (/^Contact|^Note|^Task|ContactTag/.test(eventType)) return 'lead_management_pack';
  if (/^Opportunity/.test(eventType)) return 'sales_pipeline_pack';
  if (/^Conversation|^InboundMessage|^OutboundMessage|^ProviderOutboundMessage/.test(eventType)) return 'conversation_management_pack';
  if (/^Appointment/.test(eventType)) return 'calendar_appointment_pack';
  if (/^Invoice|^Order|^Product|^Price/.test(eventType)) return 'payments_invoicing_pack';
  if (/^User/.test(eventType)) return 'user_permission_pack';
  if (/^App|^Location|^Plan|^INSTALL$/.test(eventType)) return 'sub_account_onboarding_pack';
  if (/^VoiceAi/.test(eventType)) return 'reporting_pack';
  return 'compliance_audit_pack';
}

export class WebhookProcessor {
  constructor({ store = new WebhookStore(), taskPackExecutor = new TaskPackExecutor() } = {}) {
    this.store = store;
    this.taskPackExecutor = taskPackExecutor;
  }

  async processNext() {
    const claimed = await this.store.claimNextPending();
    if (!claimed) {
      return { ok: true, processed: false, reason: 'no_pending_events' };
    }

    const { queueItem, event } = claimed;
    if (!event) {
      await this.store.markFailed(queueItem.id, 'event_not_found', { retryable: false });
      return { ok: false, processed: false, reason: 'event_not_found' };
    }

    try {
      const dispatchedAgentId = routeWebhookEvent({ eventType: event.type, locationId: event.locationId });
      const taskPack = inferTaskPack(event.type);
      const credentialRef = event.locationId ? `location-${event.locationId}` : 'agency-oauth';
      const taskPackRun = await this.taskPackExecutor.execute({
        taskPackName: taskPack,
        agentId: dispatchedAgentId,
        event,
        credentialRef,
        mode: 'auto',
        queueId: queueItem.id
      });
      const result = {
        dispatchedAgentId,
        taskPack,
        routedAt: new Date().toISOString(),
        status: taskPackRun.ok ? 'task_pack_executed_or_planned' : 'task_pack_failed',
        eventType: event.type,
        locationId: event.locationId,
        companyId: event.companyId,
        taskPackRun
      };
      await this.store.markProcessed(queueItem.id, result);
      return { ok: true, processed: true, queueId: queueItem.id, result };
    } catch (error) {
      await this.store.markFailed(queueItem.id, error.message, { retryable: true });
      return { ok: false, processed: false, reason: error.message };
    }
  }

  async drain(limit = 10) {
    const results = [];
    for (let i = 0; i < limit; i += 1) {
      const outcome = await this.processNext();
      results.push(outcome);
      if (!outcome.processed) break;
    }
    return {
      ok: true,
      processedCount: results.filter((item) => item.processed).length,
      results
    };
  }
}
