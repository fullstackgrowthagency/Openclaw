import { buildTaskPacks } from './definitions.js';

function objectIdFrom(event) {
  return event?.objectId || event?.payload?.data?.id || event?.payload?.id || null;
}

function leadMutationRequestFrom(event) {
  const request = event?.payload?.mutationRequest || event?.payload?.actionRequest || null;
  if (!request) return null;
  return {
    action: request.action || null,
    tags: Array.isArray(request.tags) ? request.tags.filter(Boolean) : [],
    note: request.note || null,
    title: request.title || null,
    pinned: Boolean(request.pinned),
    noteId: request.noteId || null,
    contactId: request.contactId || objectIdFrom(event)
  };
}

function taskPackHandlers() {
  return {
    sub_account_onboarding_pack: {
      trigger_events: ['INSTALL', 'LocationCreate', 'LocationUpdate'],
      buildExecutionPlan(context) {
        const locationId = context.event.locationId;
        return [
          {
            name: 'fetch_location_details',
            kind: 'adapter_call',
            adapter: 'LocationsAdapter',
            method: 'getLocation',
            pathHint: '/locations/:locationId',
            args: () => [context.credentialRef || 'agency-oauth', locationId],
            safe: true,
            mutation: false,
            requiresCredential: true
          },
          {
            name: 'provision_location_agent_tree',
            kind: 'intent',
            safe: true,
            mutation: false,
            details: { action: 'provision_agents', locationId }
          },
          {
            name: 'schedule_onboarding_validation',
            kind: 'intent',
            safe: true,
            mutation: false,
            details: { action: 'validate_onboarding', locationId }
          }
        ];
      }
    },
    lead_management_pack: {
      trigger_events: ['ContactCreate', 'ContactUpdate', 'ContactTagUpdate', 'TaskCreate', 'NoteCreate', 'ManualRun'],
      buildExecutionPlan(context) {
        const mutationRequest = leadMutationRequestFrom(context.event);
        const contactId = mutationRequest?.contactId || objectIdFrom(context.event);
        const explicitTagAdd = mutationRequest?.action === 'add_contact_tags' && mutationRequest.tags.length > 0;
        const explicitTagRemove = mutationRequest?.action === 'remove_contact_tags' && mutationRequest.tags.length > 0;
        const explicitNoteAdd = mutationRequest?.action === 'add_contact_note' && Boolean(mutationRequest.note);
        const explicitNoteDelete = mutationRequest?.action === 'delete_contact_note' && Boolean(mutationRequest.noteId);
        const explicitEnrichment = mutationRequest?.action === 'enrich_contact' && (mutationRequest.tags.length > 0 || Boolean(mutationRequest.note));
        return [
          {
            name: 'fetch_contact',
            kind: 'adapter_call',
            adapter: 'ContactsAdapter',
            method: 'getContact',
            pathHint: '/contacts/:contactId',
            args: () => [context.credentialRef || defaultLocationCredential(context), contactId],
            safe: true,
            mutation: false,
            requiresCredential: true,
            skipIf: () => !contactId
          },
          {
            name: 'evaluate_follow_up_rules',
            kind: 'intent',
            safe: true,
            mutation: false,
            details: { action: 'evaluate_lead_rules', contactId, mutationRequest }
          },
          {
            name: 'apply_contact_tags',
            kind: 'adapter_call',
            adapter: 'ContactsAdapter',
            method: 'addTags',
            httpMethod: 'POST',
            pathHint: '/contacts/:contactId/tags',
            args: () => [context.credentialRef || defaultLocationCredential(context), contactId, { tags: mutationRequest.tags }],
            safe: false,
            mutation: true,
            requiresApproval: true,
            requiresCredential: true,
            details: { action: explicitEnrichment ? 'enrich_contact:add_tags' : 'add_contact_tags', contactId, tags: mutationRequest?.tags || [] },
            skipIf: () => !contactId || !((explicitTagAdd || explicitEnrichment) && mutationRequest.tags.length > 0)
          },
          {
            name: 'remove_contact_tags',
            kind: 'adapter_call',
            adapter: 'ContactsAdapter',
            method: 'removeTags',
            httpMethod: 'DELETE',
            pathHint: '/contacts/:contactId/tags',
            args: () => [context.credentialRef || defaultLocationCredential(context), contactId, { tags: mutationRequest.tags }],
            safe: false,
            mutation: true,
            requiresApproval: true,
            requiresCredential: true,
            details: { action: 'remove_contact_tags', contactId, tags: mutationRequest?.tags || [] },
            skipIf: () => !contactId || !explicitTagRemove
          },
          {
            name: 'add_contact_note',
            kind: 'adapter_call',
            adapter: 'ContactsAdapter',
            method: 'addNote',
            httpMethod: 'POST',
            pathHint: '/contacts/:contactId/notes',
            args: () => [context.credentialRef || defaultLocationCredential(context), contactId, {
              body: mutationRequest.note,
              title: mutationRequest.title || 'OpenClaw controlled enrichment note',
              pinned: mutationRequest.pinned || false
            }],
            safe: false,
            mutation: true,
            requiresApproval: true,
            requiresCredential: true,
            details: {
              action: explicitEnrichment ? 'enrich_contact:add_note' : 'add_contact_note',
              contactId,
              title: mutationRequest?.title || 'OpenClaw controlled enrichment note',
              notePreview: mutationRequest?.note ? String(mutationRequest.note).slice(0, 120) : null
            },
            skipIf: () => !contactId || !((explicitNoteAdd || explicitEnrichment) && Boolean(mutationRequest.note))
          },
          {
            name: 'delete_contact_note',
            kind: 'adapter_call',
            adapter: 'ContactsAdapter',
            method: 'deleteNote',
            httpMethod: 'DELETE',
            pathHint: '/contacts/:contactId/notes/:id',
            args: () => [context.credentialRef || defaultLocationCredential(context), contactId, mutationRequest.noteId],
            safe: false,
            mutation: true,
            requiresApproval: true,
            requiresCredential: true,
            details: { action: 'delete_contact_note', contactId, noteId: mutationRequest?.noteId || null },
            skipIf: () => !contactId || !explicitNoteDelete
          },
          {
            name: 'plan_follow_up_write_actions',
            kind: 'intent',
            safe: false,
            mutation: true,
            requiresApproval: true,
            details: { action: 'possible_tag_note_task_or_opportunity', contactId },
            skipIf: () => explicitTagAdd || explicitTagRemove || explicitNoteAdd || explicitNoteDelete || explicitEnrichment
          }
        ];
      }
    },
    sales_pipeline_pack: {
      trigger_events: ['OpportunityCreate', 'OpportunityUpdate', 'OpportunityStatusUpdate'],
      buildExecutionPlan(context) {
        const opportunityId = objectIdFrom(context.event);
        return [
          {
            name: 'fetch_opportunity',
            kind: 'adapter_call',
            adapter: 'OpportunitiesAdapter',
            method: 'getOpportunity',
            pathHint: '/opportunities/:id',
            args: () => [context.credentialRef || defaultLocationCredential(context), opportunityId],
            safe: true,
            mutation: false,
            requiresCredential: true,
            skipIf: () => !opportunityId
          },
          {
            name: 'evaluate_pipeline_risk',
            kind: 'intent',
            safe: true,
            mutation: false,
            details: { action: 'evaluate_staleness_and_forecast', opportunityId }
          }
        ];
      }
    },
    conversation_management_pack: {
      trigger_events: ['ConversationUpdate', 'ConversationUnreadWebhook', 'InboundMessage', 'OutboundMessage'],
      buildExecutionPlan(context) {
        const conversationId = objectIdFrom(context.event);
        return [
          {
            name: 'fetch_conversation',
            kind: 'adapter_call',
            adapter: 'ConversationsAdapter',
            method: 'getConversation',
            pathHint: '/conversations/:conversationId',
            args: () => [context.credentialRef || defaultLocationCredential(context), conversationId],
            safe: true,
            mutation: false,
            requiresCredential: true,
            skipIf: () => !conversationId
          },
          {
            name: 'evaluate_sla_and_assignment',
            kind: 'intent',
            safe: true,
            mutation: false,
            details: { action: 'evaluate_response_rules', conversationId }
          }
        ];
      }
    },
    calendar_appointment_pack: {
      trigger_events: ['AppointmentCreate', 'AppointmentUpdate', 'AppointmentDelete'],
      buildExecutionPlan(context) {
        return [
          {
            name: 'refresh_calendar_event_window',
            kind: 'adapter_call',
            adapter: 'CalendarsAdapter',
            method: 'listEvents',
            pathHint: '/calendars/events',
            args: () => [context.credentialRef || defaultLocationCredential(context), {}],
            safe: true,
            mutation: false,
            requiresCredential: true
          },
          {
            name: 'evaluate_booking_state',
            kind: 'intent',
            safe: true,
            mutation: false,
            details: { action: 'evaluate_booking_followups', locationId: context.event.locationId }
          }
        ];
      }
    },
    workflow_automation_qa_pack: {
      trigger_events: ['WorkflowCheck'],
      buildExecutionPlan(context) {
        return [
          {
            name: 'list_workflows',
            kind: 'adapter_call',
            adapter: 'WorkflowsAdapter',
            method: 'listWorkflows',
            pathHint: '/workflows/',
            args: () => [context.credentialRef || defaultLocationCredential(context), {}],
            safe: true,
            mutation: false,
            requiresCredential: true
          }
        ];
      }
    },
    payments_invoicing_pack: {
      trigger_events: ['InvoiceCreate', 'InvoiceUpdate', 'InvoicePaid', 'OrderCreate', 'OrderStatusUpdate', 'ProductCreate', 'PriceCreate'],
      buildExecutionPlan(context) {
        return [
          {
            name: 'refresh_invoices',
            kind: 'adapter_call',
            adapter: 'InvoicesAdapter',
            method: 'listInvoices',
            pathHint: '/invoices/',
            args: () => [context.credentialRef || defaultLocationCredential(context), {}],
            safe: true,
            mutation: false,
            requiresCredential: true
          },
          {
            name: 'refresh_transactions',
            kind: 'adapter_call',
            adapter: 'PaymentsAdapter',
            method: 'listTransactions',
            pathHint: '/payments/transactions/',
            args: () => [context.credentialRef || defaultLocationCredential(context), {}],
            safe: true,
            mutation: false,
            requiresCredential: true
          },
          {
            name: 'flag_financial_followups',
            kind: 'intent',
            safe: false,
            mutation: true,
            requiresApproval: true,
            details: { action: 'possible_collections_or_follow_up', eventType: context.event.type }
          }
        ];
      }
    },
    marketing_asset_pack: {
      trigger_events: ['SocialPostCreate'],
      buildExecutionPlan(context) {
        return [
          {
            name: 'refresh_social_accounts',
            kind: 'adapter_call',
            adapter: 'SocialPlannerAdapter',
            method: 'listAccounts',
            pathHint: '/social-media-posting/:locationId/accounts',
            args: () => [context.credentialRef || defaultLocationCredential(context), context.event.locationId, {}],
            safe: true,
            mutation: false,
            requiresCredential: true
          }
        ];
      }
    },
    user_permission_pack: {
      trigger_events: ['UserCreate', 'UserUpdate', 'UserDelete'],
      buildExecutionPlan(context) {
        return [
          {
            name: 'list_users',
            kind: 'adapter_call',
            adapter: 'UsersAdapter',
            method: 'listUsers',
            pathHint: '/users/',
            args: () => [context.credentialRef || 'agency-oauth', {}],
            safe: true,
            mutation: false,
            requiresCredential: true
          },
          {
            name: 'audit_permission_drift',
            kind: 'intent',
            safe: true,
            mutation: false,
            details: { action: 'audit_permissions', companyId: context.event.companyId }
          }
        ];
      }
    },
    reporting_pack: {
      trigger_events: ['VoiceAiCallEnd', 'ReportTick'],
      buildExecutionPlan(context) {
        return [
          {
            name: 'collect_voice_ai_logs',
            kind: 'adapter_call',
            adapter: 'VoiceAiAdapter',
            method: 'listCallLogs',
            pathHint: '/voice-ai/dashboard/call-logs',
            args: () => [context.credentialRef || defaultLocationCredential(context), {}],
            safe: true,
            mutation: false,
            requiresCredential: true
          },
          {
            name: 'compile_summary',
            kind: 'intent',
            safe: true,
            mutation: false,
            details: { action: 'compile_report_summary', eventType: context.event.type }
          }
        ];
      }
    },
    snapshot_template_pack: {
      trigger_events: ['SnapshotCheck'],
      buildExecutionPlan(context) {
        return [
          {
            name: 'list_snapshots',
            kind: 'adapter_call',
            adapter: 'SnapshotsAdapter',
            method: 'listSnapshots',
            pathHint: '/snapshots',
            args: () => [context.credentialRef || 'agency-oauth', {}],
            safe: true,
            mutation: false,
            requiresCredential: true
          }
        ];
      }
    },
    compliance_audit_pack: {
      trigger_events: ['*'],
      buildExecutionPlan(context) {
        return [
          {
            name: 'record_compliance_review',
            kind: 'intent',
            safe: true,
            mutation: false,
            details: {
              action: 'audit_event',
              eventType: context.event.type,
              companyId: context.event.companyId,
              locationId: context.event.locationId
            }
          }
        ];
      }
    }
  };
}

function defaultLocationCredential(context) {
  return context.event.locationId ? `location-${context.event.locationId}` : 'location-oauth';
}

export function buildTaskPackRegistry() {
  const base = buildTaskPacks();
  const handlers = taskPackHandlers();
  return base.map((taskpack) => ({
    ...taskpack,
    trigger_events: handlers[taskpack.name]?.trigger_events || taskpack.trigger_events,
    buildExecutionPlan: handlers[taskpack.name]?.buildExecutionPlan || (() => [])
  }));
}

export function getTaskPackDefinition(name) {
  return buildTaskPackRegistry().find((item) => item.name === name) || null;
}
