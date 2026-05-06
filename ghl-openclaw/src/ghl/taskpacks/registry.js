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
    body: request.body || null,
    dueDate: request.dueDate || null,
    assignedTo: request.assignedTo || null,
    completed: typeof request.completed === 'boolean' ? request.completed : null,
    website: request.website ?? null,
    customFields: Array.isArray(request.customFields)
      ? request.customFields
          .filter((field) => field && typeof field === 'object' && field.id)
          .map((field) => ({ id: field.id, value: field.value ?? null }))
      : [],
    workflowId: request.workflowId || null,
    pinned: Boolean(request.pinned),
    noteId: request.noteId || null,
    taskId: request.taskId || null,
    contactId: request.contactId || objectIdFrom(event)
  };
}

function opportunityMutationRequestFrom(event) {
  const request = event?.payload?.mutationRequest || event?.payload?.actionRequest || null;
  if (!request) return null;
  return {
    action: request.action || null,
    opportunityId: request.opportunityId || objectIdFrom(event),
    contactId: request.contactId || null,
    locationId: request.locationId || event?.locationId || event?.payload?.locationId || null,
    pipelineId: request.pipelineId || null,
    pipelineStageId: request.pipelineStageId || null,
    status: request.status || null,
    name: request.name || null,
    monetaryValue: typeof request.monetaryValue === 'number' ? request.monetaryValue : null
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
        const explicitContactUpdate = mutationRequest?.action === 'update_contact' && Object.prototype.hasOwnProperty.call(mutationRequest, 'website');
        const explicitContactCustomFieldUpdate = mutationRequest?.action === 'update_contact_custom_fields' && mutationRequest.customFields.length > 0;
        const explicitWorkflowAdd = mutationRequest?.action === 'add_contact_to_workflow' && Boolean(mutationRequest.workflowId);
        const explicitWorkflowRemove = mutationRequest?.action === 'remove_contact_from_workflow' && Boolean(mutationRequest.workflowId);
        const explicitTaskCreate = mutationRequest?.action === 'create_contact_task' && Boolean(mutationRequest.title);
        const explicitTaskDelete = mutationRequest?.action === 'delete_contact_task' && Boolean(mutationRequest.taskId);
        const explicitTaskUpdate = mutationRequest?.action === 'update_contact_task' && Boolean(mutationRequest.taskId) && Boolean(mutationRequest.title) && typeof mutationRequest.completed === 'boolean';
        const explicitTaskComplete = mutationRequest?.action === 'update_contact_task_completed' && Boolean(mutationRequest.taskId) && typeof mutationRequest.completed === 'boolean';
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
            name: 'update_contact',
            kind: 'adapter_call',
            adapter: 'ContactsAdapter',
            method: 'updateContact',
            httpMethod: 'PUT',
            pathHint: '/contacts/:contactId',
            args: () => [context.credentialRef || defaultLocationCredential(context), contactId, { website: mutationRequest.website }],
            safe: false,
            mutation: true,
            requiresApproval: true,
            requiresCredential: true,
            details: { action: 'update_contact', contactId, website: mutationRequest?.website },
            skipIf: () => !contactId || !explicitContactUpdate
          },
          {
            name: 'update_contact_custom_fields',
            kind: 'adapter_call',
            adapter: 'ContactsAdapter',
            method: 'updateContact',
            httpMethod: 'PUT',
            pathHint: '/contacts/:contactId',
            args: () => [context.credentialRef || defaultLocationCredential(context), contactId, { customFields: mutationRequest.customFields }],
            safe: false,
            mutation: true,
            requiresApproval: true,
            requiresCredential: true,
            details: { action: 'update_contact_custom_fields', contactId, customFields: mutationRequest?.customFields || [] },
            skipIf: () => !contactId || !explicitContactCustomFieldUpdate
          },
          {
            name: 'add_contact_to_workflow',
            kind: 'adapter_call',
            adapter: 'ContactsAdapter',
            method: 'addToWorkflow',
            httpMethod: 'POST',
            pathHint: '/contacts/:contactId/workflow/:workflowId',
            args: () => [context.credentialRef || defaultLocationCredential(context), contactId, mutationRequest.workflowId],
            safe: false,
            mutation: true,
            requiresApproval: true,
            requiresCredential: true,
            details: { action: 'add_contact_to_workflow', contactId, workflowId: mutationRequest?.workflowId || null },
            skipIf: () => !contactId || !explicitWorkflowAdd
          },
          {
            name: 'remove_contact_from_workflow',
            kind: 'adapter_call',
            adapter: 'ContactsAdapter',
            method: 'removeFromWorkflow',
            httpMethod: 'DELETE',
            pathHint: '/contacts/:contactId/workflow/:workflowId',
            args: () => [context.credentialRef || defaultLocationCredential(context), contactId, mutationRequest.workflowId],
            safe: false,
            mutation: true,
            requiresApproval: true,
            requiresCredential: true,
            details: { action: 'remove_contact_from_workflow', contactId, workflowId: mutationRequest?.workflowId || null },
            skipIf: () => !contactId || !explicitWorkflowRemove
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
            name: 'create_contact_task',
            kind: 'adapter_call',
            adapter: 'ContactsAdapter',
            method: 'createTask',
            httpMethod: 'POST',
            pathHint: '/contacts/:contactId/tasks',
            args: () => [context.credentialRef || defaultLocationCredential(context), contactId, {
              title: mutationRequest.title,
              body: mutationRequest.body || 'OpenClaw controlled validation task',
              dueDate: mutationRequest.dueDate || new Date(Date.now() + 24 * 60 * 60 * 1000).toISOString(),
              ...(mutationRequest.assignedTo ? { assignedTo: mutationRequest.assignedTo } : {}),
              completed: mutationRequest.completed === null ? false : mutationRequest.completed
            }],
            safe: false,
            mutation: true,
            requiresApproval: true,
            requiresCredential: true,
            details: {
              action: 'create_contact_task',
              contactId,
              title: mutationRequest?.title || null,
              dueDate: mutationRequest?.dueDate || null
            },
            skipIf: () => !contactId || !explicitTaskCreate
          },
          {
            name: 'delete_contact_task',
            kind: 'adapter_call',
            adapter: 'ContactsAdapter',
            method: 'deleteTask',
            httpMethod: 'DELETE',
            pathHint: '/contacts/:contactId/tasks/:taskId',
            args: () => [context.credentialRef || defaultLocationCredential(context), contactId, mutationRequest.taskId],
            safe: false,
            mutation: true,
            requiresApproval: true,
            requiresCredential: true,
            details: { action: 'delete_contact_task', contactId, taskId: mutationRequest?.taskId || null },
            skipIf: () => !contactId || !explicitTaskDelete
          },
          {
            name: 'update_contact_task',
            kind: 'adapter_call',
            adapter: 'ContactsAdapter',
            method: 'updateTask',
            httpMethod: 'PUT',
            pathHint: '/contacts/:contactId/tasks/:taskId',
            args: () => [context.credentialRef || defaultLocationCredential(context), contactId, mutationRequest.taskId, {
              title: mutationRequest.title,
              body: mutationRequest.body || 'OpenClaw updated validation task',
              dueDate: mutationRequest.dueDate || new Date(Date.now() + 48 * 60 * 60 * 1000).toISOString(),
              ...(mutationRequest.assignedTo ? { assignedTo: mutationRequest.assignedTo } : {}),
              completed: mutationRequest.completed
            }],
            safe: false,
            mutation: true,
            requiresApproval: true,
            requiresCredential: true,
            details: { action: 'update_contact_task', contactId, taskId: mutationRequest?.taskId || null, title: mutationRequest?.title || null, completed: mutationRequest?.completed },
            skipIf: () => !contactId || !explicitTaskUpdate
          },
          {
            name: 'update_contact_task_completed',
            kind: 'adapter_call',
            adapter: 'ContactsAdapter',
            method: 'updateTaskCompleted',
            httpMethod: 'PUT',
            pathHint: '/contacts/:contactId/tasks/:taskId/completed',
            args: () => [context.credentialRef || defaultLocationCredential(context), contactId, mutationRequest.taskId, { completed: mutationRequest.completed }],
            safe: false,
            mutation: true,
            requiresApproval: true,
            requiresCredential: true,
            details: { action: 'update_contact_task_completed', contactId, taskId: mutationRequest?.taskId || null, completed: mutationRequest?.completed },
            skipIf: () => !contactId || !explicitTaskComplete
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
            skipIf: () => explicitContactUpdate || explicitContactCustomFieldUpdate || explicitWorkflowAdd || explicitWorkflowRemove || explicitTagAdd || explicitTagRemove || explicitTaskCreate || explicitTaskDelete || explicitTaskUpdate || explicitTaskComplete || explicitNoteAdd || explicitNoteDelete || explicitEnrichment
          }
        ];
      }
    },
    sales_pipeline_pack: {
      trigger_events: ['OpportunityCreate', 'OpportunityUpdate', 'OpportunityStatusUpdate', 'ManualRun'],
      buildExecutionPlan(context) {
        const mutationRequest = opportunityMutationRequestFrom(context.event);
        const opportunityId = mutationRequest?.opportunityId || objectIdFrom(context.event);
        const explicitOpportunityCreate = mutationRequest?.action === 'create_opportunity'
          && Boolean(mutationRequest.contactId)
          && Boolean(mutationRequest.locationId)
          && Boolean(mutationRequest.pipelineId)
          && Boolean(mutationRequest.pipelineStageId)
          && Boolean(mutationRequest.status)
          && Boolean(mutationRequest.name);
        const explicitOpportunityUpdate = mutationRequest?.action === 'update_opportunity'
          && Boolean(mutationRequest.opportunityId)
          && (
            Boolean(mutationRequest.name)
            || Boolean(mutationRequest.status)
            || Boolean(mutationRequest.pipelineId)
            || Boolean(mutationRequest.pipelineStageId)
            || mutationRequest.monetaryValue !== null
          );
        const explicitOpportunityDelete = mutationRequest?.action === 'delete_opportunity' && Boolean(mutationRequest.opportunityId);
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
            skipIf: () => !opportunityId || explicitOpportunityCreate
          },
          {
            name: 'create_opportunity',
            kind: 'adapter_call',
            adapter: 'OpportunitiesAdapter',
            method: 'createOpportunity',
            httpMethod: 'POST',
            pathHint: '/opportunities',
            args: () => [context.credentialRef || defaultLocationCredential(context), {
              locationId: mutationRequest.locationId,
              contactId: mutationRequest.contactId,
              pipelineId: mutationRequest.pipelineId,
              pipelineStageId: mutationRequest.pipelineStageId,
              status: mutationRequest.status,
              name: mutationRequest.name,
              ...(mutationRequest.monetaryValue === null ? {} : { monetaryValue: mutationRequest.monetaryValue })
            }],
            safe: false,
            mutation: true,
            requiresApproval: true,
            requiresCredential: true,
            details: {
              action: 'create_opportunity',
              locationId: mutationRequest?.locationId || null,
              contactId: mutationRequest?.contactId || null,
              pipelineId: mutationRequest?.pipelineId || null,
              pipelineStageId: mutationRequest?.pipelineStageId || null,
              status: mutationRequest?.status || null,
              name: mutationRequest?.name || null
            },
            skipIf: () => !explicitOpportunityCreate
          },
          {
            name: 'update_opportunity',
            kind: 'adapter_call',
            adapter: 'OpportunitiesAdapter',
            method: 'updateOpportunity',
            httpMethod: 'PUT',
            pathHint: '/opportunities/:id',
            args: () => [context.credentialRef || defaultLocationCredential(context), mutationRequest.opportunityId, {
              ...(mutationRequest.name ? { name: mutationRequest.name } : {}),
              ...(mutationRequest.status ? { status: mutationRequest.status } : {}),
              ...(mutationRequest.pipelineId ? { pipelineId: mutationRequest.pipelineId } : {}),
              ...(mutationRequest.pipelineStageId ? { pipelineStageId: mutationRequest.pipelineStageId } : {}),
              ...(mutationRequest.monetaryValue === null ? {} : { monetaryValue: mutationRequest.monetaryValue })
            }],
            safe: false,
            mutation: true,
            requiresApproval: true,
            requiresCredential: true,
            details: {
              action: 'update_opportunity',
              opportunityId: mutationRequest?.opportunityId || null,
              name: mutationRequest?.name || null,
              status: mutationRequest?.status || null,
              pipelineId: mutationRequest?.pipelineId || null,
              pipelineStageId: mutationRequest?.pipelineStageId || null,
              monetaryValue: mutationRequest?.monetaryValue
            },
            skipIf: () => !explicitOpportunityUpdate
          },
          {
            name: 'delete_opportunity',
            kind: 'adapter_call',
            adapter: 'OpportunitiesAdapter',
            method: 'deleteOpportunity',
            httpMethod: 'DELETE',
            pathHint: '/opportunities/:id',
            args: () => [context.credentialRef || defaultLocationCredential(context), mutationRequest.opportunityId],
            safe: false,
            mutation: true,
            requiresApproval: true,
            requiresCredential: true,
            details: { action: 'delete_opportunity', opportunityId: mutationRequest?.opportunityId || null },
            skipIf: () => !explicitOpportunityDelete
          },
          {
            name: 'evaluate_pipeline_risk',
            kind: 'intent',
            safe: true,
            mutation: false,
            details: { action: 'evaluate_staleness_and_forecast', opportunityId, mutationRequest },
            skipIf: () => explicitOpportunityCreate || explicitOpportunityUpdate || explicitOpportunityDelete
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
