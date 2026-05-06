import path from 'node:path';
import { getEnv } from '../../config/env.js';
import { readJson, writeJson } from '../../lib/fs.js';
import { buildApprovalStatusView } from '../approvals/presenter.js';
import { ApprovalStore } from '../approvals/store.js';
import { FileEncryptedStore } from '../auth/encrypted-store.js';
import { TaskPackRunStore } from '../taskpacks/run-store.js';
import { WebhookStore } from '../webhooks/store.js';

export class ReportingService {
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

  async buildAgencySummary() {
    const [approvals, runs, webhookState, credentials, capabilityRegistry] = await Promise.all([
      this.approvalStore.list(1000),
      this.taskPackRunStore.listRuns(1000),
      this.webhookStore.load(),
      this.credentialStore.list(),
      readJson(path.join(this.env.dataDir, 'generated', 'capability-registry.json'), { capabilities: [] })
    ]);

    const approvalView = buildApprovalStatusView(approvals);
    const summary = {
      generatedAt: new Date().toISOString(),
      scope: 'agency',
      capabilities: {
        total: capabilityRegistry.capabilities?.length || 0,
        supported: (capabilityRegistry.capabilities || []).filter((item) => item.status === 'supported').length,
        conditional: (capabilityRegistry.capabilities || []).filter((item) => item.status === 'conditional').length
      },
      webhooks: summarizeWebhooks(webhookState),
      taskpacks: summarizeRuns(runs),
      approvals: summarizeApprovals(approvalView),
      credentials: summarizeCredentials(credentials),
      operations: {
        topApprovalLocations: topCounts(approvals.map((item) => item.locationId || 'agency')),
        topRejectedTaskPacks: topCounts(runs.filter((item) => item.status === 'rejected').map((item) => item.taskPackName))
      },
      risks: buildRiskFlags({ approvals, runs, webhookState, credentials })
    };

    await writeJson(path.join(this.env.dataDir, 'generated', 'agency-report.json'), summary);
    return summary;
  }

  async buildLocationSummary(locationId) {
    const [approvals, runs, webhookState] = await Promise.all([
      this.approvalStore.list(1000),
      this.taskPackRunStore.listRuns(1000),
      this.webhookStore.load()
    ]);

    const locationApprovals = approvals.filter((item) => item.locationId === locationId);
    const locationRuns = runs.filter((item) => item.locationId === locationId);
    const locationEvents = webhookState.events.filter((item) => item.locationId === locationId);
    const locationQueue = webhookState.queue.filter((item) => item.locationId === locationId);
    const approvalView = buildApprovalStatusView(locationApprovals);

    const summary = {
      generatedAt: new Date().toISOString(),
      scope: 'location',
      locationId,
      webhooks: summarizeWebhooks({ events: locationEvents, queue: locationQueue }),
      taskpacks: summarizeRuns(locationRuns),
      approvals: summarizeApprovals(approvalView),
      topEventTypes: topCounts(locationEvents.map((item) => item.type)),
      topTaskPacks: topCounts(locationRuns.map((item) => item.taskPackName))
    };

    await writeJson(path.join(this.env.dataDir, 'generated', `location-report-${locationId}.json`), summary);
    return summary;
  }
}

function summarizeWebhooks(state) {
  const events = state.events || [];
  const queue = state.queue || [];
  return {
    totalEvents: events.length,
    duplicateEvents: events.filter((item) => item.duplicate).length,
    processedEvents: events.filter((item) => item.status === 'processed').length,
    deadLetterEvents: events.filter((item) => item.status === 'dead_letter').length,
    queuePending: queue.filter((item) => item.status === 'pending').length,
    queueProcessing: queue.filter((item) => item.status === 'processing').length,
    queueProcessed: queue.filter((item) => item.status === 'processed').length,
    queueDeadLetter: queue.filter((item) => item.status === 'dead_letter').length,
    topEventTypes: topCounts(events.map((item) => item.type))
  };
}

function summarizeRuns(runs) {
  return {
    totalRuns: runs.length,
    completedRuns: runs.filter((item) => item.status === 'completed').length,
    failedRuns: runs.filter((item) => item.status === 'failed').length,
    runningRuns: runs.filter((item) => item.status === 'running').length,
    awaitingApprovalRuns: runs.filter((item) => item.status === 'awaiting_approval').length,
    rejectedRuns: runs.filter((item) => item.status === 'rejected').length,
    dryRunCount: runs.filter((item) => item.mode === 'dry_run').length,
    liveRunCount: runs.filter((item) => item.mode === 'live').length,
    awaitingApprovalSteps: runs.reduce((count, run) => count + run.steps.filter((step) => step.status === 'awaiting_approval').length, 0),
    topTaskPacks: topCounts(runs.map((item) => item.taskPackName)),
    topAgents: topCounts(runs.map((item) => item.agentId))
  };
}

function summarizeApprovals(approvalView) {
  return {
    ...approvalView.summary,
    topReasons: topCounts(approvalView.approvals.flatMap((item) => item.reasonLabels || [])),
    topTaskPacks: topCounts(approvalView.approvals.map((item) => item.taskPackName)),
    recentDecisions: approvalView.approvals
      .filter((item) => item.status === 'approved' || item.status === 'rejected')
      .sort((a, b) => String(b.decidedAt || '').localeCompare(String(a.decidedAt || '')))
      .slice(0, 5)
      .map((item) => ({
        id: item.id,
        status: item.status,
        operatorSummary: item.operatorSummary,
        decisionLatencyMinutes: item.decisionLatencyMinutes,
        decider: item.decider,
        locationId: item.locationId || null
      }))
  };
}

function summarizeCredentials(credentials) {
  return {
    total: credentials.length,
    oauth: credentials.filter((item) => item.kind?.includes('oauth')).length,
    pit: credentials.filter((item) => item.kind === 'pit').length,
    agencyScoped: credentials.filter((item) => item.companyId && !item.locationId).length,
    locationScoped: credentials.filter((item) => item.locationId).length,
    topScopes: topCounts(credentials.flatMap((item) => item.scopes || []))
  };
}

function topCounts(values, limit = 10) {
  const counts = new Map();
  for (const value of values.filter(Boolean)) {
    counts.set(value, (counts.get(value) || 0) + 1);
  }
  return [...counts.entries()]
    .sort((a, b) => b[1] - a[1] || String(a[0]).localeCompare(String(b[0])))
    .slice(0, limit)
    .map(([value, count]) => ({ value, count }));
}

function buildRiskFlags({ approvals, runs, webhookState, credentials }) {
  const approvalView = buildApprovalStatusView(approvals);
  const risks = [];
  if (approvalView.summary.pending > 0) risks.push('pending_approvals_exist');
  if (approvalView.summary.pendingOver60Min > 0) risks.push('stale_approvals_exist');
  if (runs.some((item) => item.status === 'failed')) risks.push('failed_taskpack_runs_exist');
  if (runs.some((item) => item.status === 'rejected')) risks.push('rejected_taskpack_runs_exist');
  if ((webhookState.queue || []).some((item) => item.status === 'dead_letter')) risks.push('webhook_dead_letter_items_exist');
  if ((webhookState.events || []).some((item) => item.duplicate)) risks.push('duplicate_webhooks_detected');
  if (!credentials.length) risks.push('no_credentials_configured');
  return risks;
}
