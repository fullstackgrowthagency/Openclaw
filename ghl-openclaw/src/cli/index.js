#!/usr/bin/env node
import path from 'node:path';
import { getEnv } from '../config/env.js';
import { ensureDir, writeJson } from '../lib/fs.js';
import { refreshCapabilityRegistry } from '../ghl/docs/capability-registry.js';
import { renderAgentManifests } from '../ghl/agents/definitions.js';
import { renderTaskPacks } from '../ghl/taskpacks/definitions.js';
import { CredentialBroker } from '../ghl/auth/credential-broker.js';
import { GhlWebhookServer } from '../ghl/webhooks/server.js';
import { WebhookStore } from '../ghl/webhooks/store.js';
import { WebhookProcessor } from '../ghl/webhooks/processor.js';
import { ingestTestEvent } from '../ghl/webhooks/test-ingest.js';

const command = process.argv[2] || 'status';
const args = process.argv.slice(3);

async function main() {
  const env = getEnv();
  await ensureDir(env.dataDir);
  const credentialBroker = new CredentialBroker();
  const webhookStore = new WebhookStore();
  const webhookProcessor = new WebhookProcessor({ store: webhookStore });

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

function requireArg(args, name) {
  const value = optionalArg(args, name);
  if (!value) throw new Error(`Missing required argument: ${name}`);
  return value;
}

function optionalArg(args, name) {
  const match = args.find((arg) => arg.startsWith(`${name}=`));
  return match ? match.slice(name.length + 1) : null;
}

function inferCredentialRef(userType) {
  return userType === 'Location' ? 'location-oauth' : 'agency-oauth';
}

main().catch((error) => {
  console.error(JSON.stringify({ ok: false, error: error.message }, null, 2));
  process.exitCode = 1;
});
