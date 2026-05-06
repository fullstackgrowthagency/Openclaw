import path from 'node:path';
import { getEnv } from '../../config/env.js';
import { writeJson } from '../../lib/fs.js';
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

    const audit = {
      generatedAt: new Date().toISOString(),
      compliance: {
        pendingApprovals: approvals.filter((item) => item.status === 'pending').length,
        rejectedApprovals: approvals.filter((item) => item.status === 'rejected').length,
        failedRuns: runs.filter((item) => item.status === 'failed').length,
        deadLetterQueue: (webhookState.queue || []).filter((item) => item.status === 'dead_letter').length,
        duplicateWebhookEvents: (webhookState.events || []).filter((item) => item.duplicate).length,
        credentialCount: credentials.length
      },
      findings: buildFindings({ approvals, runs, webhookState, credentials }),
      byLocation: groupByLocation({ approvals, runs, webhookState, credentials })
    };

    await writeJson(path.join(this.env.dataDir, 'generated', 'audit-status.json'), audit);
    return audit;
  }
}

function buildFindings({ approvals, runs, webhookState, credentials }) {
  const findings = [];

  if (!credentials.length) {
    findings.push({ severity: 'medium', code: 'NO_CREDENTIALS', message: 'No credentials are configured yet.' });
  }

  for (const approval of approvals.filter((item) => item.status === 'pending')) {
    findings.push({
      severity: 'high',
      code: 'PENDING_APPROVAL',
      message: `Pending approval for ${approval.taskPackName}:${approval.stepName}`,
      locationId: approval.locationId || null,
      approvalId: approval.id
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
    failedRuns: runs.filter((item) => item.locationId === locationId && item.status === 'failed').length,
    webhookEvents: (webhookState.events || []).filter((item) => item.locationId === locationId).length,
    duplicateWebhooks: (webhookState.events || []).filter((item) => item.locationId === locationId && item.duplicate).length,
    credentials: credentials.filter((item) => item.locationId === locationId).length
  }));
}
