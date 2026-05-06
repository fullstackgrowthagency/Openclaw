import path from 'node:path';
import { getEnv } from '../../config/env.js';
import { writeJson } from '../../lib/fs.js';

export function buildAgencyLeadManifest() {
  return {
    agent_id: 'ghl-agency-lead',
    type: 'primary_admin',
    scope_level: 'agency',
    assigned_location_id: null,
    permissions: [
      'manage_oauth',
      'manage_sub_accounts',
      'manage_users',
      'manage_snapshots',
      'manage_webhooks',
      'maintain_capability_registry',
      'view_all_logs',
      'approve_destructive_actions',
      'provision_sub_agents',
      'monitor_rate_limits'
    ],
    responsibilities: [
      'discover_api_capabilities',
      'maintain_capability_registry',
      'create_location_agents',
      'enforce_rbac',
      'monitor_webhooks',
      'review_escalations',
      'generate_agency_reports',
      'audit_permissions',
      'detect_api_changes'
    ],
    restricted_actions: [
      'no_unconfirmed_destructive_actions',
      'no_raw_secret_logging',
      'no_undocumented_endpoint_usage'
    ]
  };
}

export function buildLocationAgentManifest(locationId) {
  return {
    agent_id: `ghl-sub-account-agent-${locationId}`,
    type: 'sub_account_operator',
    scope_level: 'location',
    assigned_location_id: locationId,
    permissions: [
      'read_location',
      'dispatch_contacts',
      'dispatch_opportunities',
      'dispatch_conversations',
      'dispatch_calendars',
      'dispatch_marketing',
      'dispatch_payments',
      'execute_assigned_task_packs',
      'generate_location_reports'
    ],
    denied_permissions: [
      'manage_other_locations',
      'manage_agency_settings',
      'manage_global_tokens',
      'create_other_agents',
      'delete_location',
      'change_oauth_scopes'
    ],
    children: [
      `ghl-contacts-agent-${locationId}`,
      `ghl-sales-pipeline-agent-${locationId}`,
      `ghl-conversations-agent-${locationId}`,
      `ghl-calendar-agent-${locationId}`,
      `ghl-workflow-agent-${locationId}`,
      `ghl-payments-agent-${locationId}`,
      `ghl-marketing-agent-${locationId}`,
      `ghl-snapshot-agent-${locationId}`,
      `ghl-voice-ai-agent-${locationId}`,
      `ghl-reporting-agent-${locationId}`,
      `ghl-compliance-audit-agent-${locationId}`
    ]
  };
}

export function buildSpecialistManifests(locationId) {
  return [
    ['contacts', ['contacts.read', 'contacts.create', 'contacts.update', 'tags.manage', 'notes.manage', 'tasks.manage']],
    ['sales-pipeline', ['opportunities.read', 'opportunities.write', 'pipelines.read']],
    ['conversations', ['conversations.read', 'conversations.write', 'conversations/message.write']],
    ['calendar', ['calendars.read', 'calendars.write', 'calendars/events.write']],
    ['workflow', ['workflows.read', 'campaigns.read']],
    ['payments', ['payments.read', 'products.read', 'products.write_if_authorized', 'invoices.read', 'invoices.write_if_authorized']],
    ['marketing', ['forms.read', 'surveys.read', 'links.read', 'links.write', 'socialplanner.read', 'socialplanner.write']],
    ['snapshot', ['snapshots.read', 'snapshots.apply_if_approved']],
    ['voice-ai', ['voice_ai.read', 'voice_ai.write_if_authorized']],
    ['reporting', ['reporting.read', 'reporting.export_if_supported']],
    ['compliance-audit', ['audit.read', 'audit.report', 'permissions.read']]
  ].map(([name, permissions]) => ({
    agent_id: `ghl-${name}-agent-${locationId}`,
    parent_agent: `ghl-sub-account-agent-${locationId}`,
    scope_level: 'location',
    assigned_location_id: locationId,
    permissions,
    blocked_actions: [
      'cross_location_access',
      'credential_access',
      'destructive_operations_without_approval'
    ]
  }));
}

export async function renderAgentManifests(locationIds = ['demo-location']) {
  const env = getEnv();
  const outputPath = path.join(env.dataDir, 'generated', 'agent-manifests.json');

  const manifests = {
    generatedAt: new Date().toISOString(),
    agency: buildAgencyLeadManifest(),
    locations: locationIds.map((locationId) => ({
      location: buildLocationAgentManifest(locationId),
      specialists: buildSpecialistManifests(locationId)
    }))
  };

  await writeJson(outputPath, manifests);
  return manifests;
}
