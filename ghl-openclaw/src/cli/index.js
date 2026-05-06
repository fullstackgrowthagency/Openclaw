#!/usr/bin/env node
import { existsSync } from 'node:fs';
import path from 'node:path';
import { getEnv } from '../config/env.js';
import { ensureDir, writeJson } from '../lib/fs.js';
import { AuditService } from '../ghl/audit/service.js';
import { refreshCapabilityRegistry } from '../ghl/docs/capability-registry.js';
import { ReportingService } from '../ghl/reporting/service.js';
import { renderAgentManifests } from '../ghl/agents/definitions.js';
import { renderTaskPacks } from '../ghl/taskpacks/definitions.js';
import { TaskPackRunStore } from '../ghl/taskpacks/run-store.js';
import { TaskPackExecutor } from '../ghl/taskpacks/executor.js';
import { buildApprovalChatPrompt, buildApprovalStatusView, enrichApproval } from '../ghl/approvals/presenter.js';
import { ApprovalStore } from '../ghl/approvals/store.js';
import { CredentialBroker } from '../ghl/auth/credential-broker.js';
import { GhlApiClient } from '../ghl/api/client.js';
import { GhlWebhookServer } from '../ghl/webhooks/server.js';
import { WebhookStore } from '../ghl/webhooks/store.js';
import { WebhookProcessor } from '../ghl/webhooks/processor.js';
import { ingestTestEvent } from '../ghl/webhooks/test-ingest.js';

const command = normalizeCommand(process.argv[2] || 'status');
const args = process.argv.slice(3);

async function main() {
  const env = getEnv();
  await ensureDir(env.dataDir);
  const credentialBroker = new CredentialBroker();
  const approvalStore = new ApprovalStore();
  const taskPackRunStore = new TaskPackRunStore();
  const taskPackExecutor = new TaskPackExecutor({ runStore: taskPackRunStore, credentialBroker, approvalStore });
  const apiClient = new GhlApiClient({ credentialBroker });
  const webhookStore = new WebhookStore();
  const webhookProcessor = new WebhookProcessor({ store: webhookStore, taskPackExecutor });
  const reportingService = new ReportingService({ approvalStore, credentialStore: credentialBroker.encryptedStore, taskPackRunStore, webhookStore });
  const auditService = new AuditService({ approvalStore, credentialStore: credentialBroker.encryptedStore, taskPackRunStore, webhookStore });

  switch (command) {
    case 'bootstrap': {
      const capabilityRegistry = await refreshCapabilityRegistry();
      const locationIds = parseLocationIds(args);
      const agentManifests = await renderAgentManifests(locationIds);
      const taskpacks = await renderTaskPacks();
      console.log(JSON.stringify({ ok: true, capabilityCount: capabilityRegistry.capabilities.length, locationCount: locationIds.length, taskpackCount: taskpacks.taskpacks.length }, null, 2));
      return;
    }
    case 'capability:refresh': {
      const output = await refreshCapabilityRegistry();
      console.log(JSON.stringify({ ok: true, capabilityCount: output.capabilities.length, docsVersion: output.docsVersion, fetchErrors: output.fetchErrors }, null, 2));
      return;
    }
    case 'agents:render': {
      const locationIds = parseLocationIds(args);
      const output = await renderAgentManifests(locationIds);
      console.log(JSON.stringify({ ok: true, locationCount: output.locations.length }, null, 2));
      return;
    }
    case 'taskpacks:render': {
      const output = await renderTaskPacks();
      console.log(JSON.stringify({ ok: true, taskpackCount: output.taskpacks.length }, null, 2));
      return;
    }
    case 'taskpack:status': {
      const summary = await taskPackRunStore.summary();
      console.log(JSON.stringify({ ok: true, summary }, null, 2));
      return;
    }
    case 'taskpack:runs': {
      const limit = Number(optionalArg(args, '--limit') || 20);
      const runs = await taskPackRunStore.listRuns(limit);
      console.log(JSON.stringify({ ok: true, runs }, null, 2));
      return;
    }
    case 'taskpack:resume': {
      const runId = requireArg(args, '--run-id');
      const result = await taskPackExecutor.resumeRun({ runId });
      console.log(JSON.stringify(await withApprovalPrompt(result, approvalStore), null, 2));
      return;
    }
    case 'approval:status': {
      const approvals = await approvalStore.list(1000);
      const view = buildApprovalStatusView(approvals);
      console.log(JSON.stringify({ ok: true, summary: view.summary }, null, 2));
      return;
    }
    case 'approval:list': {
      const limit = Number(optionalArg(args, '--limit') || 50);
      const status = optionalArg(args, '--status') || null;
      const approvals = await approvalStore.list(limit, status);
      const view = buildApprovalStatusView(approvals);
      console.log(JSON.stringify({ ok: true, summary: view.summary, approvals: view.approvals }, null, 2));
      return;
    }
    case 'approval:show': {
      const id = await resolveApprovalIdArg(args, approvalStore, { requirePending: false });
      const approval = await approvalStore.get(id);
      console.log(JSON.stringify({ ok: Boolean(approval), approval: approval ? enrichApproval(approval) : null, approvalPrompt: approval ? buildApprovalChatPrompt(approval) : null }, null, 2));
      return;
    }
    case 'approval:approve': {
      const id = await resolveApprovalIdArg(args, approvalStore);
      const decider = optionalArg(args, '--decider') || 'human_admin';
      const note = optionalArg(args, '--note') || null;
      const resumeRun = parseBooleanArg(args, '--resume-run', true);
      const approval = await approvalStore.decide(id, { decision: 'approved', decider, note });
      const resumed = approval && approval.runId && resumeRun ? await taskPackExecutor.resumeRun({ runId: approval.runId }) : null;
      console.log(JSON.stringify({ ok: Boolean(approval), approval, resumed }, null, 2));
      return;
    }
    case 'approval:reject': {
      const id = await resolveApprovalIdArg(args, approvalStore);
      const decider = optionalArg(args, '--decider') || 'human_admin';
      const note = optionalArg(args, '--note') || null;
      const resumeRun = parseBooleanArg(args, '--resume-run', true);
      const approval = await approvalStore.decide(id, { decision: 'rejected', decider, note });
      const resumed = approval && approval.runId && resumeRun ? await taskPackExecutor.resumeRun({ runId: approval.runId }) : null;
      console.log(JSON.stringify({ ok: Boolean(approval), approval, resumed }, null, 2));
      return;
    }
    case 'report:generate': {
      const locationId = optionalArg(args, '--location-id') || null;
      const report = locationId
        ? await reportingService.buildLocationSummary(locationId)
        : await reportingService.buildAgencySummary();
      console.log(JSON.stringify({ ok: true, report }, null, 2));
      return;
    }
    case 'audit:status': {
      const audit = await auditService.buildAuditStatus();
      console.log(JSON.stringify({ ok: true, audit }, null, 2));
      return;
    }
    case 'taskpack:run': {
      const taskPackName = requireArg(args, '--name');
      const eventType = optionalArg(args, '--event-type') || 'ManualRun';
      const locationId = optionalArg(args, '--location-id') || 'demo-location';
      const companyId = optionalArg(args, '--company-id') || 'demo-company';
      const payloadJson = parseJsonArg(optionalArg(args, '--payload-json')) || {};
      const payloadData = payloadJson.data && typeof payloadJson.data === 'object' ? payloadJson.data : {};
      const payloadMutationRequest = payloadJson.mutationRequest && typeof payloadJson.mutationRequest === 'object' ? payloadJson.mutationRequest : {};
      const objectId = optionalArg(args, '--object-id') || payloadData.id || payloadMutationRequest.contactId || payloadJson.objectId || 'demo-object';
      const agentId = optionalArg(args, '--agent-id') || inferAgentId(taskPackName, locationId);
      const credentialRef = optionalArg(args, '--credential-ref') || null;
      const mode = optionalArg(args, '--mode') || 'auto';
      const event = {
        type: eventType,
        locationId,
        companyId,
        objectId,
        payload: {
          type: eventType,
          locationId,
          companyId,
          ...payloadJson,
          data: { ...payloadData, id: objectId }
        }
      };
      const result = await taskPackExecutor.execute({ taskPackName, agentId, event, credentialRef, mode });
      console.log(JSON.stringify(await withApprovalPrompt(result, approvalStore), null, 2));
      return;
    }
    case 'auth:status': {
      console.log(JSON.stringify({
        ok: true,
        hasClientId: Boolean(env.clientId),
        hasClientSecret: Boolean(env.clientSecret),
        hasRedirectUri: Boolean(env.redirectUri),
        hasAgencyPit: Boolean(env.agencyPit),
        hasSecretKey: Boolean(env.secretKey),
        apiBaseUrl: env.apiBaseUrl
      }, null, 2));
      return;
    }
    case 'auth:doctor': {
      const storedCredentials = await credentialBroker.listCredentialMetadata();
      const doctor = buildAuthDoctor(env, { cwd: process.cwd(), storedCredentials });
      console.log(JSON.stringify({ ok: doctor.ready, doctor }, null, 2));
      return;
    }
    case 'auth:list': {
      const credentials = await credentialBroker.listCredentialMetadata();
      console.log(JSON.stringify({ ok: true, count: credentials.length, credentials }, null, 2));
      return;
    }
    case 'auth:store-pit': {
      const credentialRef = optionalArg(args, '--credential-ref') || 'agency-pit';
      const token = optionalArg(args, '--token') || env.agencyPit;
      const companyId = optionalArg(args, '--company-id') || null;
      const locationId = optionalArg(args, '--location-id') || null;
      const scopes = parseCsvArg(optionalArg(args, '--scopes'));
      if (!token) throw new Error('Missing PIT token. Provide --token=... or set GHL_AGENCY_PIT.');
      const result = await credentialBroker.storePit({ credentialRef, token, companyId, locationId, scopes });
      console.log(JSON.stringify({ ok: true, credential: result }, null, 2));
      return;
    }
    case 'auth:exchange-code': {
      const code = requireArg(args, '--code');
      const userType = requireArg(args, '--user-type');
      const credentialRef = optionalArg(args, '--credential-ref') || inferCredentialRef(userType);
      const result = await credentialBroker.exchangeAuthorizationCode({ credentialRef, code, userType });
      console.log(JSON.stringify({ ok: true, credentialRef, result }, null, 2));
      return;
    }
    case 'auth:refresh': {
      const credentialRef = requireArg(args, '--credential-ref');
      const result = await credentialBroker.refreshOauthToken(credentialRef);
      console.log(JSON.stringify({ ok: true, credentialRef, result }, null, 2));
      return;
    }
    case 'auth:location-token': {
      const agencyCredentialRef = requireArg(args, '--credential-ref');
      const companyId = requireArg(args, '--company-id');
      const locationId = requireArg(args, '--location-id');
      const targetCredentialRef = optionalArg(args, '--target-credential-ref') || `location-${locationId}`;
      const result = await credentialBroker.exchangeAgencyForLocationToken({
        agencyCredentialRef,
        targetCredentialRef,
        companyId,
        locationId
      });
      console.log(JSON.stringify({ ok: true, targetCredentialRef, result }, null, 2));
      return;
    }
    case 'api:probe': {
      const credentialRef = requireArg(args, '--credential-ref');
      const requestPath = requireArg(args, '--path');
      if (!requestPath.startsWith('/')) throw new Error('--path must start with /.');
      const query = parseKeyValueArg(optionalArg(args, '--query'));
      const version = optionalArg(args, '--version') || undefined;
      const result = await apiClient.request({ credentialRef, method: 'GET', path: requestPath, query, version });
      console.log(JSON.stringify({
        ok: result.ok,
        status: result.status,
        headers: sanitizeHeaders(result.headers || {}),
        dataPreview: previewData(result.data)
      }, null, 2));
      return;
    }
    case 'webhook:status': {
      const summary = await webhookStore.summary();
      console.log(JSON.stringify({ ok: true, host: env.webhookHost, port: env.webhookPort, summary }, null, 2));
      return;
    }
    case 'webhook:serve': {
      const host = optionalArg(args, '--host') || env.webhookHost;
      const port = Number(optionalArg(args, '--port') || env.webhookPort);
      const server = new GhlWebhookServer({ store: webhookStore, processor: webhookProcessor, host, port });
      await server.start();
      console.log(JSON.stringify({ ok: true, listening: true, host, port, path: '/webhooks/ghl' }, null, 2));
      return;
    }
    case 'webhook:drain': {
      const limit = Number(optionalArg(args, '--limit') || 10);
      const result = await webhookProcessor.drain(limit);
      console.log(JSON.stringify(result, null, 2));
      return;
    }
    case 'webhook:test-event': {
      const type = optionalArg(args, '--type') || 'ContactCreate';
      const locationId = optionalArg(args, '--location-id') || 'demo-location';
      const companyId = optionalArg(args, '--company-id') || 'demo-company';
      const payload = {
        type,
        webhookId: optionalArg(args, '--webhook-id') || `test-${Date.now()}`,
        timestamp: new Date().toISOString(),
        locationId,
        companyId,
        data: {
          id: optionalArg(args, '--object-id') || 'demo-object'
        }
      };
      const result = await ingestTestEvent(payload, { store: webhookStore });
      console.log(JSON.stringify(result, null, 2));
      return;
    }
    case 'status': {
      const status = {
        ok: true,
        project: 'ghl-openclaw',
        cwd: env.cwd,
        dataDir: env.dataDir,
        apiBaseUrl: env.apiBaseUrl,
        docsBaseUrl: env.docsBaseUrl,
        webhookHost: env.webhookHost,
        webhookPort: env.webhookPort,
        approvalBulkThreshold: env.approvalBulkThreshold,
        hasClientId: Boolean(env.clientId),
        hasClientSecret: Boolean(env.clientSecret),
        hasAgencyPit: Boolean(env.agencyPit),
        hasSecretKey: Boolean(env.secretKey)
      };
      const outputPath = path.join(env.dataDir, 'generated', 'status.json');
      await writeJson(outputPath, status);
      console.log(JSON.stringify(status, null, 2));
      return;
    }
    default:
      throw new Error(`Unknown command: ${command}`);
  }
}

function parseLocationIds(args) {
  const locationArg = args.find((arg) => arg.startsWith('--locations='));
  if (!locationArg) return ['demo-location'];
  return locationArg.replace('--locations=', '').split(',').map((item) => item.trim()).filter(Boolean);
}

function normalizeCommand(command) {
  if (command === 'approve') return 'approval:approve';
  if (command === 'reject') return 'approval:reject';
  if (command === 'approval:approve-latest') return 'approval:approve';
  if (command === 'approval:reject-latest') return 'approval:reject';
  return command;
}

function requireArg(args, name) {
  const value = optionalArg(args, name);
  if (!value) throw new Error(`Missing required argument: ${name}`);
  return value;
}

function optionalArg(args, name) {
  const match = args.find((arg) => arg.startsWith(`${name}=`));
  return match ? match.slice(name.length + 1) : null;
}

function parseBooleanArg(args, name, defaultValue) {
  const value = optionalArg(args, name);
  if (value == null) return defaultValue;
  return value !== 'false' && value !== '0' && value !== 'no';
}

async function resolveApprovalIdArg(args, approvalStore, { requirePending = true } = {}) {
  const explicitId = optionalArg(args, '--id');
  if (explicitId) return explicitId;

  const pending = await approvalStore.list(1000, 'pending');
  if (pending.length === 1) return pending[0].id;
  if (pending.length === 0) {
    if (requirePending) throw new Error('No pending approvals found, pass --id to target a non-pending approval.');
    throw new Error('Missing required argument: --id');
  }
  const ids = pending.slice(0, 10).map((approval) => approval.id).join(', ');
  throw new Error(`Multiple pending approvals found, pass --id explicitly. Pending approvals: ${ids}`);
}

function parseJsonArg(value) {
  if (!value) return null;
  try {
    return JSON.parse(value);
  } catch (error) {
    throw new Error(`Invalid JSON argument: ${error.message}`);
  }
}

function parseCsvArg(value) {
  if (!value) return [];
  return value.split(',').map((item) => item.trim()).filter(Boolean);
}

function parseKeyValueArg(value) {
  const output = {};
  for (const item of parseCsvArg(value)) {
    const idx = item.indexOf('=');
    if (idx === -1) continue;
    const key = item.slice(0, idx).trim();
    const raw = item.slice(idx + 1).trim();
    if (!key) continue;
    output[key] = raw;
  }
  return output;
}

function buildAuthDoctor(env, { cwd, storedCredentials = [] }) {
  const envPath = path.join(cwd, '.env');
  const checks = {
    envFilePresent: existsSync(envPath),
    secretStorageReady: Boolean(env.secretKey),
    oauthClientConfigured: Boolean(env.clientId && env.clientSecret),
    redirectUriConfigured: Boolean(env.redirectUri),
    agencyPitConfigured: Boolean(env.agencyPit),
    storedCredentialCount: storedCredentials.length,
    storedAgencyPitCount: storedCredentials.filter((item) => item.kind === 'pit').length,
    apiBaseUrl: env.apiBaseUrl,
    webhookPublicKeyConfigured: Boolean(env.webhookPublicKey)
  };

  const nextSteps = [];
  if (!checks.envFilePresent) nextSteps.push('Create .env from .env.example.');
  if (!checks.secretStorageReady) nextSteps.push('Set GHL_SECRET_KEY before storing any credentials.');
  if (!checks.oauthClientConfigured && !checks.storedAgencyPitCount) nextSteps.push('Set GHL_CLIENT_ID and GHL_CLIENT_SECRET for OAuth exchange and refresh, or store a PIT.');
  if (!checks.redirectUriConfigured && checks.oauthClientConfigured) nextSteps.push('Set GHL_REDIRECT_URI to the callback registered in HighLevel.');
  if (!checks.agencyPitConfigured && !checks.storedAgencyPitCount) nextSteps.push('Optional: set GHL_AGENCY_PIT or use auth:store-pit for agency-level read probes.');

  return {
    ready: checks.secretStorageReady && (checks.oauthClientConfigured || checks.agencyPitConfigured || checks.storedCredentialCount > 0),
    checks,
    nextSteps,
    suggestedCommands: [
      'npm run auth:status',
      'node src/cli/index.js auth:doctor',
      'node src/cli/index.js auth:store-pit --credential-ref=agency-pit',
      'node src/cli/index.js auth:list',
      'node src/cli/index.js api:probe --credential-ref=agency-pit --path=/locations/search'
    ]
  };
}

function sanitizeHeaders(headers) {
  const output = {};
  for (const [key, value] of Object.entries(headers || {})) {
    output[key] = key.toLowerCase() === 'authorization' ? '[redacted]' : value;
  }
  return output;
}

function previewData(data) {
  if (data == null) return null;
  const serialized = typeof data === 'string' ? data : JSON.stringify(data);
  return serialized.length > 1000 ? `${serialized.slice(0, 1000)}...` : serialized;
}

async function withApprovalPrompt(result, approvalStore) {
  if (!result || result.status !== 'awaiting_approval' || !result.approvalId) {
    return result;
  }
  const approval = await approvalStore.get(result.approvalId);
  return {
    ...result,
    approvalPrompt: approval ? buildApprovalChatPrompt(approval) : null
  };
}

function inferCredentialRef(userType) {
  return userType === 'Location' ? 'location-oauth' : 'agency-oauth';
}

function inferAgentId(taskPackName, locationId) {
  if (taskPackName === 'sub_account_onboarding_pack' || taskPackName === 'user_permission_pack' || taskPackName === 'snapshot_template_pack') return 'ghl-agency-lead';
  if (taskPackName === 'lead_management_pack') return `ghl-contacts-agent-${locationId}`;
  if (taskPackName === 'sales_pipeline_pack') return `ghl-sales-pipeline-agent-${locationId}`;
  if (taskPackName === 'conversation_management_pack') return `ghl-conversations-agent-${locationId}`;
  if (taskPackName === 'calendar_appointment_pack') return `ghl-calendar-agent-${locationId}`;
  if (taskPackName === 'workflow_automation_qa_pack') return `ghl-workflow-agent-${locationId}`;
  if (taskPackName === 'payments_invoicing_pack') return `ghl-payments-agent-${locationId}`;
  if (taskPackName === 'marketing_asset_pack') return `ghl-marketing-agent-${locationId}`;
  if (taskPackName === 'reporting_pack') return `ghl-reporting-agent-${locationId}`;
  if (taskPackName === 'compliance_audit_pack') return `ghl-compliance-audit-agent-${locationId}`;
  return `ghl-sub-account-agent-${locationId}`;
}

main().catch((error) => {
  console.error(JSON.stringify({ ok: false, error: error.message }, null, 2));
  process.exitCode = 1;
});
