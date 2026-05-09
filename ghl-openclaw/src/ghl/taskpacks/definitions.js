import path from 'node:path';
import { getEnv } from '../../config/env.js';
import { writeJson } from '../../lib/fs.js';

function pack(name, owner, requiredScopes, requiredEndpoints, approvalRequired = false) {
  return {
    name,
    owner_agent_type: owner,
    purpose: name,
    required_scopes: requiredScopes,
    required_endpoints: requiredEndpoints,
    required_token_type: ['oauth_access_token', 'private_integration_token'],
    input_schema: `${name}:input`,
    output_schema: `${name}:output`,
    trigger_events: [],
    manual_execution_command: name,
    validation_rules: ['scope_check', 'token_context_check', 'capability_check'],
    error_handling_rules: ['retry_safe_reads', 'escalate_missing_scope', 'block_undocumented'],
    logging_requirements: ['correlation_id', 'agent_id', 'location_id', 'result_summary'],
    success_criteria: ['completed_without_policy_violation'],
    rollback_plan: approvalRequired ? ['manual_rollback_review'] : ['best_effort_safe_rollback'],
    permission_level: approvalRequired ? 'approved_write' : 'standard',
    approval_required: approvalRequired
  };
}

export function buildTaskPacks() {
  return [
    pack('sub_account_onboarding_pack', 'ghl-agency-lead', ['locations.write', 'snapshots.readonly'], ['/locations/', '/snapshots'], true),
    pack('lead_management_pack', 'ghl-contacts-agent-{locationId}', ['contacts.write'], ['/contacts/']),
    pack('sales_pipeline_pack', 'ghl-sales-pipeline-agent-{locationId}', ['opportunities.write'], ['/opportunities']),
    pack('conversation_management_pack', 'ghl-conversations-agent-{locationId}', ['conversations.write'], ['/conversations/search', '/conversations/:conversationId', '/conversations/:conversationId/messages', '/conversations/messages']),
    pack('conversational_ai_pack', 'ghl-conversations-agent-{locationId}', ['conversations.write', 'contacts.write', 'opportunities.write'], ['/contacts/', '/contacts/:contactId', '/contacts/:contactId/notes', '/contacts/:contactId/tags', '/conversations/search', '/conversations/:conversationId', '/conversations/:conversationId/messages', '/conversations/messages', '/opportunities/']),
    pack('calendar_appointment_pack', 'ghl-calendar-agent-{locationId}', ['calendars.write'], ['/calendars/', '/calendars/events/appointments', '/contacts/:contactId/appointments']),
    pack('workflow_automation_qa_pack', 'ghl-workflow-agent-{locationId}', ['workflows.readonly'], ['/workflows/']),
    pack('payments_invoicing_pack', 'ghl-payments-agent-{locationId}', ['invoices.write', 'products.write'], ['/invoices', '/products/'], true),
    pack('marketing_asset_pack', 'ghl-marketing-agent-{locationId}', ['links.write', 'socialplanner/posts.write'], ['/social-media-posting/:locationId/accounts', '/social-media-posting/:locationId/posts'], true),
    pack('facebook_ad_pack', 'ghl-marketing-agent-{locationId}', ['admanager/campaigns.write', 'admanager/adsets.write', 'admanager/ads.write'], ['/ad-publishing/facebook/entity', '/ad-publishing/facebook/campaigns', '/ad-publishing/facebook/adsets', '/ad-publishing/facebook/ads'], true),
    pack('google_ad_pack', 'ghl-marketing-agent-{locationId}', ['admanager/campaigns.write', 'admanager/ads.write'], ['/ad-publishing/google/entity', '/ad-publishing/google/ads', '/ad-publishing/google/ads/:adId/publish'], true),
    pack('user_permission_pack', 'ghl-agency-lead', ['users.write'], ['/users/'], true),
    pack('reporting_pack', 'ghl-reporting-agent-{locationId}', ['reporting.read'], []),
    pack('snapshot_template_pack', 'ghl-agency-lead', ['snapshots.write'], ['/snapshots'], true),
    pack('compliance_audit_pack', 'ghl-compliance-audit-agent-{locationId}', ['audit.read'], [])
  ];
}

export async function renderTaskPacks() {
  const env = getEnv();
  const outputPath = path.join(env.dataDir, 'generated', 'taskpacks.json');
  const taskpacks = {
    generatedAt: new Date().toISOString(),
    taskpacks: buildTaskPacks()
  };
  await writeJson(outputPath, taskpacks);
  return taskpacks;
}
