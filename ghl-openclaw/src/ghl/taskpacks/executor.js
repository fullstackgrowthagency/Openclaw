import {
  AssociationsAdapter,
  BusinessesAdapter,
  CalendarsAdapter,
  ContactsAdapter,
  ConversationsAdapter,
  CustomMenusAdapter,
  FormsAdapter,
  InvoicesAdapter,
  LocationCustomFieldsAdapter,
  LocationCustomValuesAdapter,
  LocationsAdapter,
  MediaStorageAdapter,
  OpportunitiesAdapter,
  PaymentsAdapter,
  PhoneSystemAdapter,
  ProductsAdapter,
  SaasAdapter,
  SnapshotsAdapter,
  SocialPlannerAdapter,
  SurveysAdapter,
  TriggerLinksAdapter,
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
  AssociationsAdapter,
  BusinessesAdapter,
  CalendarsAdapter,
  ContactsAdapter,
  ConversationsAdapter,
  CustomMenusAdapter,
  FormsAdapter,
  InvoicesAdapter,
  LocationCustomFieldsAdapter,
  LocationCustomValuesAdapter,
  LocationsAdapter,
  MediaStorageAdapter,
  OpportunitiesAdapter,
  PaymentsAdapter,
  PhoneSystemAdapter,
  ProductsAdapter,
  SaasAdapter,
  SnapshotsAdapter,
  SocialPlannerAdapter,
  SurveysAdapter,
  TriggerLinksAdapter,
  UsersAdapter,
  VoiceAiAdapter,
  WorkflowsAdapter
};

const FINAL_STEP_STATUSES = new Set(['executed', 'planned', 'skipped', 'rejected']);

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

    return this.executeExistingRun(run, taskPack);
  }

  async resumeRun({ runId }) {
    const run = await this.runStore.getRun(runId);
    if (!run) {
      throw new Error(`Unknown task pack run: ${runId}`);
    }

    const taskPack = getTaskPackDefinition(run.taskPackName);
    if (!taskPack) {
      throw new Error(`Unknown task pack for run ${runId}: ${run.taskPackName}`);
    }

    if (['completed', 'failed', 'rejected'].includes(run.status)) {
      return {
        ok: run.status === 'completed',
        runId: run.id,
        taskPackName: run.taskPackName,
        mode: run.mode,
        status: run.status,
        summary: run.summary,
        error: run.error
      };
    }

    return this.executeExistingRun(run, taskPack);
  }

  async executeExistingRun(run, taskPack) {
    const runtime = { stepOutputs: {} };
    const context = {
      taskPack,
      event: run.event,
      agentId: run.agentId,
      credentialRef: run.credentialRef,
      mode: run.mode,
      queueId: run.queueId,
      runtime
    };
    const plan = taskPack.buildExecutionPlan(context) || [];

    try {
      await this.runStore.setStatus(run.id, 'running', { error: null });

      for (let index = 0; index < plan.length; index += 1) {
        const step = plan[index];
        const currentRun = await this.runStore.getRun(run.id);
        const existing = currentRun?.steps?.[index] || null;

        if (existing && FINAL_STEP_STATUSES.has(existing.status)) {
          const restoredOutput = restoreStepOutput(existing);
          if (restoredOutput) {
            runtime.stepOutputs[step.name] = restoredOutput;
          }
          continue;
        }

        if (step.skipIf?.(context)) {
          await this.runStore.setStep(run.id, index, stepResult(step, 'skipped', { reason: 'skip_condition' }, index));
          continue;
        }

        let resolvedApproval = null;
        const approvalPolicy = evaluateApprovalRequirement({ step, mode: run.mode, taskPack, event: run.event });
        if (approvalPolicy.requiresApproval) {
          const approvalDedupeKey = buildApprovalDedupeKey(run.id, index, step.name, approvalPolicy.reason);
          const approval = existing?.payload?.approvalId
            ? await this.approvalStore.get(existing.payload.approvalId)
            : await this.approvalStore.findLatestByDedupeKey(approvalDedupeKey);

          if (!approval) {
            const createdApproval = await this.approvalStore.createRequest({
              runId: run.id,
              queueId: run.queueId,
              taskPackName: taskPack.name,
              stepName: step.name,
              stepIndex: index,
              agentId: run.agentId,
              companyId: run.event.companyId || null,
              locationId: run.event.locationId || null,
              eventType: run.event.type,
              mode: run.mode,
              reason: approvalPolicy.reason,
              key: approvalDedupeKey,
              details: {
                stepKind: step.kind,
                mutation: Boolean(step.mutation),
                safe: Boolean(step.safe),
                adapter: step.adapter || null,
                adapterMethod: step.method || null,
                pathHint: step.pathHint || null,
                riskLevel: approvalPolicy.riskLevel,
                reviewHint: approvalPolicy.reviewHint,
                plannedDetails: typeof step.details === 'function' ? step.details(context) : (step.details || null)
              }
            });
            await this.runStore.setStep(run.id, index, stepResult(step, 'awaiting_approval', {
              approvalId: createdApproval.id,
              reason: approvalPolicy.reason,
              reasons: approvalPolicy.reasons
            }, index));
            return this.pauseForApproval(run.id, taskPack, run.mode, run.event, createdApproval.id);
          }

          if (approval.status === 'pending') {
            if (existing?.status !== 'awaiting_approval' || existing?.payload?.approvalId !== approval.id) {
              await this.runStore.setStep(run.id, index, stepResult(step, 'awaiting_approval', {
                approvalId: approval.id,
                reason: approvalPolicy.reason,
                reasons: approvalPolicy.reasons
              }, index));
            }
            return this.pauseForApproval(run.id, taskPack, run.mode, run.event, approval.id);
          }

          if (approval.status === 'rejected') {
            const rejectedStep = stepResult(step, 'rejected', {
              approvalId: approval.id,
              decider: approval.decider,
              note: approval.decisionNote,
              decidedAt: approval.decidedAt,
              reason: approval.reason
            }, index);
            await this.runStore.setStep(run.id, index, rejectedStep);
            const summary = buildSummary(taskPack, run.id, run.mode, run.event, 'rejected');
            await this.runStore.setStatus(run.id, 'rejected', { summary, error: approval.decisionNote || approval.reason });
            return {
              ok: false,
              runId: run.id,
              taskPackName: taskPack.name,
              mode: run.mode,
              status: 'rejected',
              summary,
              error: approval.decisionNote || approval.reason,
              approvalId: approval.id
            };
          }

          resolvedApproval = approval;
        }

        if (step.kind === 'intent') {
          await this.runStore.setStep(run.id, index, stepResult(step, 'planned', {
            details: typeof step.details === 'function' ? step.details(context) : (step.details || null),
            approval: approvalSnapshot(resolvedApproval)
          }, index));
          continue;
        }

        if (step.kind === 'adapter_call') {
          if (!step.requiresCredential || (run.mode === 'live' && run.credentialRef)) {
            const adapter = this.adapters[step.adapter];
            if (!adapter || typeof adapter[step.method] !== 'function') {
              throw new Error(`Adapter step not wired: ${step.adapter}.${step.method}`);
            }

            const args = step.args ? step.args(context) : [];
            const liveResult = await adapter[step.method](...args);
            runtime.stepOutputs[step.name] = liveResult;
            await this.runStore.setStep(run.id, index, stepResult(step, 'executed', {
              ...sanitizeLiveResult(liveResult),
              ...(step.resumeData ? { resumeData: step.resumeData(liveResult, context) } : {}),
              approval: approvalSnapshot(resolvedApproval)
            }, index));
          } else {
            await this.runStore.setStep(run.id, index, stepResult(step, 'planned', {
              reason: 'missing_credentials_for_live_execution',
              approval: approvalSnapshot(resolvedApproval)
            }, index));
          }
          continue;
        }

        await this.runStore.setStep(run.id, index, stepResult(step, 'skipped', { reason: 'unknown_step_kind' }, index));
      }

      const summary = buildSummary(taskPack, run.id, run.mode, run.event, 'completed');
      await this.runStore.completeRun(run.id, summary);
      return {
        ok: true,
        runId: run.id,
        taskPackName: taskPack.name,
        mode: run.mode,
        status: 'completed',
        summary
      };
    } catch (error) {
      await this.runStore.failRun(run.id, error.message);
      return {
        ok: false,
        runId: run.id,
        taskPackName: taskPack.name,
        mode: run.mode,
        status: 'failed',
        error: error.message
      };
    }
  }

  async pauseForApproval(runId, taskPack, mode, event, approvalId) {
    const summary = buildSummary(taskPack, runId, mode, event, 'awaiting_approval', 1);
    await this.runStore.setStatus(runId, 'awaiting_approval', { summary });
    return {
      ok: true,
      runId,
      taskPackName: taskPack.name,
      mode,
      status: 'awaiting_approval',
      approvalId,
      summary
    };
  }

  async resolveMode(mode, credentialRef) {
    if (mode === 'live') return 'live';
    if (mode === 'dry_run') return 'dry_run';
    if (!credentialRef) return 'dry_run';
    const metadata = await this.credentialBroker.resolveCredentialMetadata(credentialRef);
    return metadata ? 'live' : 'dry_run';
  }
}

function stepResult(step, status, payload, index = null) {
  return {
    index,
    name: step.name,
    kind: step.kind,
    status,
    safe: Boolean(step.safe),
    mutation: Boolean(step.mutation),
    createdAt: new Date().toISOString(),
    payload
  };
}

function buildApprovalDedupeKey(runId, stepIndex, stepName, reason) {
  return `${runId}:${stepIndex ?? stepName}:${reason}`;
}

function approvalSnapshot(approval) {
  if (!approval) return null;
  return {
    id: approval.id,
    decision: approval.status,
    decider: approval.decider,
    note: approval.decisionNote,
    decidedAt: approval.decidedAt
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

function restoreStepOutput(step) {
  if (step?.status !== 'executed') return null;
  if (step?.payload?.resumeData) {
    return { data: step.payload.resumeData };
  }
  const preview = step?.payload?.dataPreview;
  if (typeof preview !== 'string' || !preview) return null;
  if (preview.endsWith('...')) return null;
  try {
    return { data: JSON.parse(preview) };
  } catch {
    return null;
  }
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

function buildSummary(taskPack, runId, mode, event, status, pendingApprovals = 0) {
  return {
    runId,
    taskPackName: taskPack.name,
    mode,
    status,
    eventType: event.type,
    locationId: event.locationId || null,
    companyId: event.companyId || null,
    approvalPendingCount: pendingApprovals,
    completedAt: new Date().toISOString()
  };
}
