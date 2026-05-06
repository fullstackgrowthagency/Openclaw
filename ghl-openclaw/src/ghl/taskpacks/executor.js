import {
  CalendarsAdapter,
  ContactsAdapter,
  ConversationsAdapter,
  InvoicesAdapter,
  LocationsAdapter,
  OpportunitiesAdapter,
  PaymentsAdapter,
  ProductsAdapter,
  SnapshotsAdapter,
  SocialPlannerAdapter,
  UsersAdapter,
  VoiceAiAdapter,
  WorkflowsAdapter
} from '../api/index.js';
import { ApprovalStore } from '../approvals/store.js';
import { evaluateApprovalRequirement } from '../approvals/policy.js';
import { CredentialBroker } from '../auth/credential-broker.js';
import { TaskPackRunStore } from './run-store.js';
import { getTaskPackDefinition } from './registry.js';

const ADAPTERS = {
  LocationsAdapter,
  ContactsAdapter,
  ConversationsAdapter,
  OpportunitiesAdapter,
  UsersAdapter,
  CalendarsAdapter,
  InvoicesAdapter,
  PaymentsAdapter,
  ProductsAdapter,
  SnapshotsAdapter,
  SocialPlannerAdapter,
  VoiceAiAdapter,
  WorkflowsAdapter
};

export class TaskPackExecutor {
  constructor({
    runStore = new TaskPackRunStore(),
    credentialBroker = new CredentialBroker(),
    approvalStore = new ApprovalStore()
  } = {}) {
    this.runStore = runStore;
    this.credentialBroker = credentialBroker;
    this.approvalStore = approvalStore;
    this.adapters = Object.fromEntries(Object.entries(ADAPTERS).map(([name, Klass]) => [name, new Klass()]));
  }

  async execute({ taskPackName, agentId, event, credentialRef = null, mode = 'auto', queueId = null }) {
    const taskPack = getTaskPackDefinition(taskPackName);
    if (!taskPack) {
      throw new Error(`Unknown task pack: ${taskPackName}`);
    }

    const resolvedMode = await this.resolveMode(mode, credentialRef);
    const run = await this.runStore.createRun({
      taskPackName,
      agentId,
      event,
      mode: resolvedMode,
      credentialRef,
      queueId
    });

    const context = { taskPack, event, agentId, credentialRef, mode: resolvedMode, queueId };
    const plan = taskPack.buildExecutionPlan(context) || [];
    let pendingApprovals = 0;

    try {
      for (const step of plan) {
        if (step.skipIf?.(context)) {
          await this.runStore.appendStep(run.id, stepResult(step, 'skipped', { reason: 'skip_condition' }));
          continue;
        }

        const approvalPolicy = evaluateApprovalRequirement({ step, mode: resolvedMode, taskPack, event });
        if (approvalPolicy.requiresApproval) {
          const approval = await this.approvalStore.createRequest({
            runId: run.id,
            queueId,
            taskPackName,
            stepName: step.name,
            agentId,
            companyId: event.companyId || null,
            locationId: event.locationId || null,
            eventType: event.type,
            mode: resolvedMode,
            reason: approvalPolicy.reason,
            details: {
              stepKind: step.kind,
              mutation: Boolean(step.mutation),
              safe: Boolean(step.safe),
              pathHint: step.pathHint || null,
              method: step.method || null,
              plannedDetails: step.details || null
            }
          });
          pendingApprovals += 1;
          await this.runStore.appendStep(run.id, stepResult(step, 'awaiting_approval', {
            approvalId: approval.id,
            reason: approvalPolicy.reason,
            reasons: approvalPolicy.reasons
          }));
          continue;
        }

        if (step.kind === 'intent') {
          await this.runStore.appendStep(run.id, stepResult(step, 'planned', { details: step.details || null }));
          continue;
        }

        if (step.kind === 'adapter_call') {
          if (!step.requiresCredential || (resolvedMode === 'live' && credentialRef)) {
            const adapter = this.adapters[step.adapter];
            if (!adapter || typeof adapter[step.method] !== 'function') {
              throw new Error(`Adapter step not wired: ${step.adapter}.${step.method}`);
            }

            const args = step.args ? step.args(context) : [];
            const liveResult = await adapter[step.method](...args);
            await this.runStore.appendStep(run.id, stepResult(step, 'executed', sanitizeLiveResult(liveResult)));
          } else {
            await this.runStore.appendStep(run.id, stepResult(step, 'planned', { reason: 'missing_credentials_for_live_execution' }));
          }
          continue;
        }

        await this.runStore.appendStep(run.id, stepResult(step, 'skipped', { reason: 'unknown_step_kind' }));
      }

      const summary = buildSummary(taskPack, run.id, resolvedMode, event, pendingApprovals);
      await this.runStore.completeRun(run.id, summary);
      return {
        ok: true,
        runId: run.id,
        taskPackName,
        mode: resolvedMode,
        summary
      };
    } catch (error) {
      await this.runStore.failRun(run.id, error.message);
      return {
        ok: false,
        runId: run.id,
        taskPackName,
        mode: resolvedMode,
        error: error.message
      };
    }
  }

  async resolveMode(mode, credentialRef) {
    if (mode === 'live') return 'live';
    if (mode === 'dry_run') return 'dry_run';
    if (!credentialRef) return 'dry_run';
    const metadata = await this.credentialBroker.resolveCredentialMetadata(credentialRef);
    return metadata ? 'live' : 'dry_run';
  }
}

function stepResult(step, status, payload) {
  return {
    name: step.name,
    kind: step.kind,
    status,
    safe: Boolean(step.safe),
    mutation: Boolean(step.mutation),
    createdAt: new Date().toISOString(),
    payload
  };
}

function sanitizeLiveResult(result) {
  return {
    ok: result?.ok ?? false,
    status: result?.status ?? null,
    headers: sanitizeHeaders(result?.headers || {}),
    dataPreview: previewData(result?.data)
  };
}

function sanitizeHeaders(headers) {
  const output = {};
  for (const [key, value] of Object.entries(headers)) {
    output[key] = key.toLowerCase() === 'authorization' ? '[redacted]' : value;
  }
  return output;
}

function previewData(data) {
  if (data == null) return null;
  const serialized = typeof data === 'string' ? data : JSON.stringify(data);
  return serialized.length > 500 ? `${serialized.slice(0, 500)}...` : serialized;
}

function buildSummary(taskPack, runId, mode, event, pendingApprovals = 0) {
  return {
    runId,
    taskPackName: taskPack.name,
    mode,
    eventType: event.type,
    locationId: event.locationId || null,
    companyId: event.companyId || null,
    approvalPendingCount: pendingApprovals,
    completedAt: new Date().toISOString()
  };
}
