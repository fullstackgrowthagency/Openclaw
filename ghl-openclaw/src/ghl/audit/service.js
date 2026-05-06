import path from 'node:path';
import { getEnv } from '../../config/env.js';
import { writeJson } from '../../lib/fs.js';
import { buildApprovalStatusView } from '../approvals/presenter.js';
import { ApprovalStore } from '../approvals/store.js';
import { FileEncryptedStore } from '../auth/encrypted-store.js';
import { TaskPackRunStore } from '../taskpacks/run-store.js';
import { WebhookStore } from '../webhooks/store.js';

export class AuditService {
  constructor({
    approvalStore = new ApprovalStore(),
    credentialStore = new FileEncryptedStore(),
    taskPackRunStore = new TaskPackRunStore(),
    webhookStore = new WebhookStore()
  } = {}) {
    this.approvalStore = approvalStore;
    this.credentialStore = credentialStore;
    this.taskPackRunStore = taskPackRunStore;
    this.webhookStore = webhookStore;
    this.env = getEnv();
  }

  async buildAuditStatus() {
    const [approvals, runs, webhookState, credentials] = await Promise.all([
      this.approvalStore.list(1000),
      this.taskPackRunStore.listRuns(1000),
      this.webhookStore.load(),
      this.credentialStore.list()
    ]);

    const approvalView = buildApprovalStatusView(approvals);
    const audit = {
      generatedAt: new Date().toISOString(),
      compliance: {
        pendingApprovals: approvalView.summary.pending,
        staleApprovals: approvalView.summary.pendingOver60Min,
        rejectedApprovals: approvalView.summary.rejected,
        avgApprovalDecisionMinutes: approvalView.summary.avgDecisionMinutes,
        failedRuns: runs.filter((item) => item.status === 'failed').length,
        rejectedRuns: runs.filter((item) => item.status === 'rejected').length,
        deadLetterQueue: (webhookState.queue || []).filter((item) => item.status === 'dead_letter').length,
        duplicateWebhookEvents: (webhookState.events || []).filter((item) => item.duplicate).length,
        credentialCount: credentials.length
      },
      attentionQueue: buildAttentionQueue({ approvals: approvalView.approvals, runs, webhookState }),
      findings: buildFindings({ approvals: approvalView.approvals, runs, webhookState, credentials }),
      byLocation: groupByLocation({ approvals: approvalView.approvals, runs, webhookState, credentials })
    };

    await writeJson(path.join(this.env.dataDir, 'generated', 'audit-status.json'), audit);
    return audit;
  }
}

function buildAttentionQueue({ approvals, runs, webhookState }) {
  const items = [];

  approvals
    .filter((item) => item.status === 'pending')
    .forEach((approval) => {
      items.push({
        kind: 'approval',
        priority: priorityForApproval(approval),
        id: approval.id,
        locationId: approval.locationId || null,
        summary: approval.operatorSummary,
        ageMinutes: approval.ageMinutes
      });
    });

  runs
    .filter((item) => item.status === 'failed' || item.status === 'rejected')
    .forEach((run) => {
      items.push({
        kind: 'run',
        priority: run.status === 'failed' ? 'high' : 'medium',
        id: run.id,
        locationId: run.locationId || null,
        summary: `${run.status} run ${run.taskPackName}`,
        ageMinutes: minutesSince(run.updatedAt)
      });
    });

  (webhookState.queue || [])
    .filter((item) => item.status === 'dead_letter')
    .forEach((item) => {
      items.push({
        kind: 'webhook',
        priority: 'high',
        id: item.id,
        locationId: item.locationId || null,
        summary: `dead letter webhook ${item.type}`,
        ageMinutes: minutesSince(item.updatedAt || item.createdAt)
      });
    });

  return items
    .sort((a, b) => comparePriority(b.priority, a.priority) || (b.ageMinutes || 0) - (a.ageMinutes || 0))
    .slice(0, 10);
}

function buildFindings({ approvals, runs, webhookState, credentials }) {
  const findings = [];

  if (!credentials.length) {
    findings.push({ severity: 'medium', code: 'NO_CREDENTIALS', message: 'No credentials are configured yet.' });
  }

  for (const approval of approvals.filter((item) => item.status === 'pending')) {
    findings.push({
      severity: approval.ageMinutes >= 60 ? 'critical' : approval.ageMinutes >= 15 ? 'high' : 'medium',
      code: approval.ageMinutes >= 60 ? 'STALE_PENDING_APPROVAL' : 'PENDING_APPROVAL',
      message: `Pending approval for ${approval.operatorSummary}`,
      locationId: approval.locationId || null,
      approvalId: approval.id,
      ageMinutes: approval.ageMinutes,
      reasons: approval.reasonLabels
    });
  }

  for (const run of runs.filter((item) => item.status === 'failed')) {
    findings.push({
      severity: 'high',
      code: 'FAILED_TASKPACK_RUN',
      message: `Failed task-pack run ${run.taskPackName}`,
      locationId: run.locationId || null,
      runId: run.id
    });
  }

  for (const run of runs.filter((item) => item.status === 'rejected')) {
    findings.push({
      severity: 'medium',
      code: 'REJECTED_TASKPACK_RUN',
      message: `Rejected task-pack run ${run.taskPackName}`,
      locationId: run.locationId || null,
      runId: run.id
    });
  }

  for (const queueItem of (webhookState.queue || []).filter((item) => item.status === 'dead_letter')) {
    findings.push({
      severity: 'high',
      code: 'WEBHOOK_DEAD_LETTER',
      message: `Webhook queue item moved to dead letter: ${queueItem.type}`,
      locationId: queueItem.locationId || null,
      queueId: queueItem.id
    });
  }

  for (const event of (webhookState.events || []).filter((item) => item.duplicate)) {
    findings.push({
      severity: 'low',
      code: 'DUPLICATE_WEBHOOK',
      message: `Duplicate webhook detected: ${event.type}`,
      locationId: event.locationId || null,
      webhookId: event.webhookId || null
    });
  }

  return findings;
}

function groupByLocation({ approvals, runs, webhookState, credentials }) {
  const locationIds = new Set();
  approvals.forEach((item) => item.locationId && locationIds.add(item.locationId));
  runs.forEach((item) => item.locationId && locationIds.add(item.locationId));
  (webhookState.events || []).forEach((item) => item.locationId && locationIds.add(item.locationId));
  credentials.forEach((item) => item.locationId && locationIds.add(item.locationId));

  return [...locationIds].sort().map((locationId) => ({
    locationId,
    pendingApprovals: approvals.filter((item) => item.locationId === locationId && item.status === 'pending').length,
    staleApprovals: approvals.filter((item) => item.locationId === locationId && item.status === 'pending' && (item.ageMinutes || 0) >= 60).length,
    rejectedApprovals: approvals.filter((item) => item.locationId === locationId && item.status === 'rejected').length,
    failedRuns: runs.filter((item) => item.locationId === locationId && item.status === 'failed').length,
    rejectedRuns: runs.filter((item) => item.locationId === locationId && item.status === 'rejected').length,
    webhookEvents: (webhookState.events || []).filter((item) => item.locationId === locationId).length,
    duplicateWebhooks: (webhookState.events || []).filter((item) => item.locationId === locationId && item.duplicate).length,
    credentials: credentials.filter((item) => item.locationId === locationId).length,
    riskLevel: computeLocationRisk({ approvals, runs, webhookState, locationId })
  }));
}

function computeLocationRisk({ approvals, runs, webhookState, locationId }) {
  if (approvals.some((item) => item.locationId === locationId && item.status === 'pending' && (item.ageMinutes || 0) >= 60)) return 'high';
  if (runs.some((item) => item.locationId === locationId && item.status === 'failed')) return 'high';
  if ((webhookState.queue || []).some((item) => item.locationId === locationId && item.status === 'dead_letter')) return 'high';
  if (approvals.some((item) => item.locationId === locationId && item.status === 'pending')) return 'medium';
  if (runs.some((item) => item.locationId === locationId && item.status === 'rejected')) return 'medium';
  return 'low';
}

function priorityForApproval(approval) {
  if ((approval.ageMinutes || 0) >= 60) return 'critical';
  if ((approval.ageMinutes || 0) >= 15) return 'high';
  return 'medium';
}

function minutesSince(iso) {
  if (!iso) return null;
  const value = new Date(iso).getTime();
  if (!Number.isFinite(value)) return null;
  return Math.max(0, Math.round((Date.now() - value) / 60000));
}

function comparePriority(left, right) {
  const rank = { low: 1, medium: 2, high: 3, critical: 4 };
  return (rank[left] || 0) - (rank[right] || 0);
}
