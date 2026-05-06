#!/usr/bin/env node
import path from 'node:path';
import { getEnv } from '../config/env.js';
import { ensureDir, writeJson } from '../lib/fs.js';
import { refreshCapabilityRegistry } from '../ghl/docs/capability-registry.js';
import { renderAgentManifests } from '../ghl/agents/definitions.js';
import { renderTaskPacks } from '../ghl/taskpacks/definitions.js';

const command = process.argv[2] || 'status';
const args = process.argv.slice(3);

async function main() {
  const env = getEnv();
  await ensureDir(env.dataDir);

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
    case 'status': {
      const status = {
        ok: true,
        project: 'ghl-openclaw',
        cwd: env.cwd,
        dataDir: env.dataDir,
        docsBaseUrl: env.docsBaseUrl,
        approvalBulkThreshold: env.approvalBulkThreshold,
        hasClientId: Boolean(env.clientId),
        hasClientSecret: Boolean(env.clientSecret),
        hasAgencyPit: Boolean(env.agencyPit)
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

main().catch((error) => {
  console.error(JSON.stringify({ ok: false, error: error.message }, null, 2));
  process.exitCode = 1;
});
