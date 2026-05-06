import { buildTaskPacks } from './definitions.js';

function objectIdFrom(event) {
  return event?.objectId || event?.payload?.data?.id || event?.payload?.id || null;
}

function normalizeString(value) {
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

function normalizeBoolean(value) {
  return typeof value === 'boolean' ? value : null;
}

function normalizeStringArray(values) {
  return Array.isArray(values) ? values.map((value) => normalizeString(value)).filter(Boolean) : [];
}

function normalizeNumber(value) {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string' && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function normalizeTimestampLike(value) {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string' && value.trim()) {
    const asNumber = Number(value);
    if (Number.isFinite(asNumber)) return asNumber;
    const asDate = Date.parse(value);
    return Number.isNaN(asDate) ? null : asDate;
  }
  return null;
}

function contactSearchResumeData(liveResult) {
  return {
    contacts: Array.isArray(liveResult?.data?.contacts)
      ? liveResult.data.contacts.slice(0, 5).map((contact) => ({
          id: contact.id,
          name: contact.contactName || null,
          email: contact.email || null,
          phone: contact.phone || null
        }))
      : []
  };
}

function resolvedSingleContactId(context, stepName, contactName) {
  const contacts = context?.runtime?.stepOutputs?.[stepName]?.data?.contacts;
  if (!Array.isArray(contacts) || contacts.length === 0) {
    throw new Error(`No contact matched \"${contactName || 'unknown'}\".`);
  }
  if (contacts.length > 1) {
    const options = contacts
      .slice(0, 5)
      .map((contact) => contact.name || contact.email || contact.phone || contact.id)
      .join(', ');
    throw new Error(`Multiple contacts matched \"${contactName || 'unknown'}\": ${options}`);
  }
  return contacts[0].id;
}

function leadMutationRequestFrom(event) {
  const request = event?.payload?.mutationRequest || event?.payload?.actionRequest || null;
  if (!request) return null;
  return {
    action: request.action || null,
    contactName: normalizeString(request.contactName) || normalizeString(request.name),
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
    followers: normalizeStringArray(request.followers || request.userIds || request.followerIds),
    pinned: Boolean(request.pinned),
    noteId: request.noteId || null,
    taskId: request.taskId || null,
    contactId: request.contactId || (normalizeString(request.contactName) || normalizeString(request.name) ? null : objectIdFrom(event))
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
    followers: Array.isArray(request.followers) ? request.followers.filter(Boolean) : [],
    monetaryValue: typeof request.monetaryValue === 'number' ? request.monetaryValue : null
  };
}

function conversationMutationRequestFrom(event) {
  const request = event?.payload?.mutationRequest || event?.payload?.actionRequest || null;
  if (!request) return null;
  const normalizedMessageType = normalizeString(request.messageType) || normalizeString(request.type);
  const normalizedContactName = normalizeString(request.contactName) || normalizeString(request.name);
  return {
    action: request.action || null,
    conversationId: request.conversationId || objectIdFrom(event),
    contactId: request.contactId || null,
    contactName: normalizedContactName,
    message: normalizeString(request.message),
    messageType: normalizedMessageType,
    channel: normalizeString(request.channel),
    fromNumber: normalizeString(request.fromNumber),
    toNumber: normalizeString(request.toNumber)
  };
}

function appointmentMutationRequestFrom(event) {
  const request = event?.payload?.mutationRequest || event?.payload?.actionRequest || null;
  if (!request) return null;
  const action = request.action || null;
  const shouldFallbackToEventObjectId = !action || ['update_appointment', 'cancel_appointment', 'delete_appointment'].includes(action);
  return {
    action,
    appointmentId: request.appointmentId || request.eventId || (shouldFallbackToEventObjectId ? objectIdFrom(event) : null),
    locationId: request.locationId || event?.locationId || event?.payload?.locationId || null,
    calendarId: request.calendarId || null,
    contactId: request.contactId || null,
    contactName: normalizeString(request.contactName) || normalizeString(request.name),
    startDate: normalizeTimestampLike(request.startDate),
    endDate: normalizeTimestampLike(request.endDate),
    startTime: normalizeString(request.startTime),
    endTime: normalizeString(request.endTime),
    durationMinutes: normalizeNumber(request.durationMinutes ?? request.duration),
    title: normalizeString(request.title),
    address: normalizeString(request.address),
    assignedUserId: request.assignedUserId || request.assignedTo || null,
    description: normalizeString(request.description),
    meetingLocationType: normalizeString(request.meetingLocationType),
    meetingLocationId: normalizeString(request.meetingLocationId),
    overrideLocationConfig: normalizeBoolean(request.overrideLocationConfig),
    appointmentStatus: normalizeString(request.appointmentStatus),
    timezone: normalizeString(request.timezone),
    toNotify: normalizeBoolean(request.toNotify),
    ignoreDateRange: normalizeBoolean(request.ignoreDateRange),
    ignoreFreeSlotValidation: normalizeBoolean(request.ignoreFreeSlotValidation),
    rrule: normalizeString(request.rrule)
  };
}

function appointmentFetchResumeData(liveResult) {
  const appointment = liveResult?.data?.appointment || liveResult?.data || null;
  if (!appointment || typeof appointment !== 'object') {
    return { appointment: null };
  }
  return {
    appointment: {
      id: appointment.id || null,
      calendarId: appointment.calendarId || null,
      locationId: appointment.locationId || null,
      contactId: appointment.contactId || null,
      assignedUserId: appointment.assignedUserId || null,
      appointmentStatus: appointment.appointmentStatus || appointment.appoinmentStatus || null,
      startTime: appointment.startTime || null,
      endTime: appointment.endTime || null,
      title: appointment.title || null
    }
  };
}

function appointmentListResumeData(liveResult) {
  return {
    appointments: collectAppointmentRecords(liveResult?.data).slice(0, 100),
    raw: liveResult?.data || null
  };
}

function appointmentCalendarListResumeData(liveResult) {
  const appointments = collectAppointmentRecords(liveResult?.data).slice(0, 100);
  return {
    appointments,
    raw: liveResult?.data || null,
    bestEffort: true,
    warning: appointments.length === 0
      ? 'Calendar-scoped /calendars/events may return false-empty results in live HighLevel responses. Prefer contact-scoped listing when a contact is known.'
      : null
  };
}

function appointmentFreeSlotsResumeData(liveResult) {
  return {
    slots: collectAppointmentSlotStartTimes(liveResult?.data).slice(0, 200),
    raw: liveResult?.data || null
  };
}

function fetchedAppointment(context) {
  return context?.runtime?.stepOutputs?.fetch_appointment?.data?.appointment || null;
}

function resolvedAppointmentCalendarId(context, mutationRequest) {
  return mutationRequest?.calendarId || fetchedAppointment(context)?.calendarId || null;
}

function resolvedAppointmentAssignedUserId(context, mutationRequest) {
  return mutationRequest?.assignedUserId || fetchedAppointment(context)?.assignedUserId || null;
}

function resolvedAppointmentListContactId(context, mutationRequest) {
  if (mutationRequest?.contactId) return mutationRequest.contactId;
  if (mutationRequest?.contactName) {
    return resolvedSingleContactId(context, 'search_appointment_contact_by_name', mutationRequest?.contactName);
  }
  return null;
}

function appointmentListQuery(context, mutationRequest) {
  const computedEndTimeValue = mutationRequest?.endTime || computedEndTime(mutationRequest?.startTime, mutationRequest?.durationMinutes);
  const startDate = mutationRequest?.startDate ?? (mutationRequest?.startTime ? new Date(mutationRequest.startTime).getTime() : null);
  const endDate = mutationRequest?.endDate ?? (computedEndTimeValue ? new Date(computedEndTimeValue).getTime() : null);
  const startTime = mutationRequest?.startTime || (startDate !== null ? new Date(startDate).toISOString() : null);
  const endTime = computedEndTimeValue || (endDate !== null ? new Date(endDate).toISOString() : null);
  return {
    ...(mutationRequest?.locationId ? { locationId: mutationRequest.locationId } : {}),
    ...(mutationRequest?.calendarId ? { calendarId: mutationRequest.calendarId } : {}),
    ...(resolvedAppointmentAssignedUserId(context, mutationRequest) ? { userId: resolvedAppointmentAssignedUserId(context, mutationRequest) } : {}),
    ...(mutationRequest?.appointmentStatus ? { appointmentStatus: mutationRequest.appointmentStatus } : {}),
    ...(startTime ? { startTime } : {}),
    ...(endTime ? { endTime } : {})
  };
}

function appointmentUpdateBody(context, mutationRequest, { forceCancelled = false } = {}) {
  const endTime = mutationRequest?.endTime || computedEndTime(mutationRequest?.startTime, mutationRequest?.durationMinutes);
  return {
    ...(resolvedAppointmentCalendarId(context, mutationRequest) ? { calendarId: resolvedAppointmentCalendarId(context, mutationRequest) } : {}),
    ...(mutationRequest?.locationId || fetchedAppointment(context)?.locationId ? { locationId: mutationRequest?.locationId || fetchedAppointment(context)?.locationId } : {}),
    ...(mutationRequest?.contactId || fetchedAppointment(context)?.contactId ? { contactId: mutationRequest?.contactId || fetchedAppointment(context)?.contactId } : {}),
    ...(mutationRequest?.startTime ? { startTime: mutationRequest.startTime } : {}),
    ...(endTime ? { endTime } : {}),
    ...(mutationRequest?.title ? { title: mutationRequest.title } : {}),
    ...(mutationRequest?.address ? { address: mutationRequest.address } : {}),
    ...(resolvedAppointmentAssignedUserId(context, mutationRequest) ? { assignedUserId: resolvedAppointmentAssignedUserId(context, mutationRequest) } : {}),
    ...(mutationRequest?.description ? { description: mutationRequest.description } : {}),
    ...(mutationRequest?.meetingLocationType ? { meetingLocationType: mutationRequest.meetingLocationType } : {}),
    ...(mutationRequest?.meetingLocationId ? { meetingLocationId: mutationRequest.meetingLocationId } : {}),
    ...(mutationRequest?.overrideLocationConfig === null ? {} : { overrideLocationConfig: mutationRequest.overrideLocationConfig }),
    ...((forceCancelled || mutationRequest?.appointmentStatus) ? { appointmentStatus: forceCancelled ? 'cancelled' : mutationRequest.appointmentStatus } : {}),
    ...(mutationRequest?.toNotify === null ? {} : { toNotify: mutationRequest.toNotify }),
    ...(mutationRequest?.ignoreDateRange === null ? {} : { ignoreDateRange: mutationRequest.ignoreDateRange }),
    ...(mutationRequest?.ignoreFreeSlotValidation === null ? {} : { ignoreFreeSlotValidation: mutationRequest.ignoreFreeSlotValidation }),
    ...(mutationRequest?.rrule ? { rrule: mutationRequest.rrule } : {})
  };
}

function appointmentAvailabilityRequested(mutationRequest) {
  return Boolean(mutationRequest?.startTime || mutationRequest?.endTime || mutationRequest?.durationMinutes !== null);
}

function appointmentFreeSlotsRequested(mutationRequest) {
  return Boolean(
    mutationRequest?.startDate !== null
    || mutationRequest?.endDate !== null
    || mutationRequest?.startTime
    || mutationRequest?.endTime
    || mutationRequest?.durationMinutes !== null
  );
}

function appointmentFreeSlotsQuery(context, mutationRequest) {
  const computedEndTimeValue = mutationRequest?.endTime || computedEndTime(mutationRequest?.startTime, mutationRequest?.durationMinutes);
  const startDate = mutationRequest?.startDate ?? (mutationRequest?.startTime ? new Date(mutationRequest.startTime).getTime() : null);
  const endDate = mutationRequest?.endDate ?? (computedEndTimeValue ? new Date(computedEndTimeValue).getTime() : null);
  if (startDate === null || endDate === null) {
    throw new Error('Free-slots lookup requires startDate/endDate or startTime/endTime (or durationMinutes).');
  }
  return {
    startDate,
    endDate,
    ...(resolvedAppointmentAssignedUserId(context, mutationRequest) ? { userId: resolvedAppointmentAssignedUserId(context, mutationRequest) } : {}),
    ...(mutationRequest?.timezone ? { timezone: mutationRequest.timezone } : {})
  };
}

function appointmentAvailabilityQuery(context, mutationRequest) {
  return appointmentFreeSlotsQuery(context, mutationRequest);
}

function collectAppointmentSlotStartTimes(value, output = []) {
  if (!value) return output;
  if (Array.isArray(value)) {
    value.forEach((item) => collectAppointmentSlotStartTimes(item, output));
    return output;
  }
  if (typeof value === 'object') {
    if (typeof value.startTime === 'string') output.push(value.startTime);
    if (typeof value.dateTime === 'string') output.push(value.dateTime);
    if (typeof value.time === 'string') output.push(value.time);
    Object.values(value).forEach((item) => collectAppointmentSlotStartTimes(item, output));
    return output;
  }
  if (typeof value === 'string' && !Number.isNaN(new Date(value).getTime())) {
    output.push(value);
  }
  return output;
}

function collectAppointmentRecords(value, output = []) {
  if (!value) return output;
  if (Array.isArray(value)) {
    value.forEach((item) => collectAppointmentRecords(item, output));
    return output;
  }
  if (typeof value !== 'object') return output;

  const looksLikeAppointment = typeof value.id === 'string'
    && (
      typeof value.startTime === 'string'
      || typeof value.endTime === 'string'
      || typeof value.calendarId === 'string'
      || typeof value.contactId === 'string'
      || typeof value.appointmentStatus === 'string'
      || typeof value.appoinmentStatus === 'string'
    );

  if (looksLikeAppointment) {
    output.push({
      id: value.id,
      calendarId: value.calendarId || null,
      locationId: value.locationId || null,
      contactId: value.contactId || null,
      assignedUserId: value.assignedUserId || null,
      appointmentStatus: value.appointmentStatus || value.appoinmentStatus || null,
      startTime: value.startTime || null,
      endTime: value.endTime || null,
      title: value.title || null
    });
  }

  Object.values(value).forEach((item) => collectAppointmentRecords(item, output));
  return output;
}

function ensureRequestedAppointmentSlotAvailable(context, mutationRequest) {
  const requestedStartTime = mutationRequest?.startTime;
  if (!requestedStartTime) return { requestedStartTime: null, available: true, reason: 'no_time_change_requested' };
  const slotCheck = context?.runtime?.stepOutputs?.check_requested_appointment_slot?.data || null;
  const slotValues = Array.from(new Set(collectAppointmentSlotStartTimes(slotCheck)));
  const requestedMs = new Date(requestedStartTime).getTime();
  const available = slotValues.some((slot) => new Date(slot).getTime() === requestedMs);
  if (!available) {
    const availablePreview = slotValues.slice(0, 10).join(', ');
    throw new Error(availablePreview
      ? `Requested appointment slot is not available. Available starts in range: ${availablePreview}`
      : 'Requested appointment slot is not available according to free-slots lookup.');
  }
  return {
    requestedStartTime,
    available: true,
    matchingSlot: requestedStartTime
  };
}

function computedEndTime(startTime, durationMinutes) {
  if (!startTime || durationMinutes === null) return null;
  const start = new Date(startTime);
  if (Number.isNaN(start.getTime())) {
    throw new Error(`Invalid startTime for duration calculation: ${startTime}`);
  }
  return new Date(start.getTime() + durationMinutes * 60 * 1000).toISOString();
}

function resolvedLeadContactId(context, mutationRequest) {
  if (mutationRequest?.contactId) return mutationRequest.contactId;
  return resolvedSingleContactId(context, 'search_target_contact_by_name', mutationRequest?.contactName);
}

function resolvedLeadContactDetails(context, mutationRequest, action) {
  const resolvedContactId = mutationRequest?.contactId || context?.runtime?.stepOutputs?.search_target_contact_by_name?.data?.contacts?.[0]?.id || null;
  return {
    action,
    contactId: resolvedContactId,
    contactName: mutationRequest?.contactName || null,
    followers: mutationRequest?.followers || []
  };
}

function resolvedConversationContactId(context, mutationRequest) {
  if (mutationRequest?.contactId) return mutationRequest.contactId;
  return resolvedSingleContactId(context, 'search_contacts_by_name', mutationRequest?.contactName);
}

function resolvedConversationContactDetails(context, mutationRequest) {
  const resolvedContactId = mutationRequest?.contactId || context?.runtime?.stepOutputs?.search_contacts_by_name?.data?.contacts?.[0]?.id || null;
  return {
    action: 'send_conversation_message',
    conversationId: mutationRequest?.conversationId || null,
    contactId: resolvedContactId,
    contactName: mutationRequest?.contactName || null,
    messageType: mutationRequest?.messageType || null,
    channel: mutationRequest?.channel || null,
    fromNumber: mutationRequest?.fromNumber || null,
    toNumber: mutationRequest?.toNumber || null,
    messagePreview: mutationRequest?.message ? mutationRequest.message.slice(0, 120) : null
  };
}

function resolvedAppointmentContactId(context, mutationRequest) {
  if (mutationRequest?.contactId) return mutationRequest.contactId;
  return resolvedSingleContactId(context, 'search_appointment_contact_by_name', mutationRequest?.contactName);
}

function appointmentBody(runtimeContext, mutationRequest) {
  const endTime = mutationRequest?.endTime || computedEndTime(mutationRequest?.startTime, mutationRequest?.durationMinutes);
  return {
    calendarId: mutationRequest.calendarId,
    locationId: mutationRequest.locationId,
    contactId: resolvedAppointmentContactId(runtimeContext, mutationRequest),
    startTime: mutationRequest.startTime,
    ...(endTime ? { endTime } : {}),
    ...(mutationRequest.title ? { title: mutationRequest.title } : {}),
    ...(mutationRequest.address ? { address: mutationRequest.address } : {}),
    ...(mutationRequest.assignedUserId ? { assignedUserId: mutationRequest.assignedUserId } : {}),
    ...(mutationRequest.description ? { description: mutationRequest.description } : {}),
    ...(mutationRequest.meetingLocationType ? { meetingLocationType: mutationRequest.meetingLocationType } : {}),
    ...(mutationRequest.meetingLocationId ? { meetingLocationId: mutationRequest.meetingLocationId } : {}),
    ...(mutationRequest.overrideLocationConfig === null ? {} : { overrideLocationConfig: mutationRequest.overrideLocationConfig }),
    ...(mutationRequest.appointmentStatus ? { appointmentStatus: mutationRequest.appointmentStatus } : {}),
    ...(mutationRequest.toNotify === null ? {} : { toNotify: mutationRequest.toNotify }),
    ...(mutationRequest.ignoreDateRange === null ? {} : { ignoreDateRange: mutationRequest.ignoreDateRange }),
    ...(mutationRequest.ignoreFreeSlotValidation === null ? {} : { ignoreFreeSlotValidation: mutationRequest.ignoreFreeSlotValidation }),
    ...(mutationRequest.rrule ? { rrule: mutationRequest.rrule } : {})
  };
}

function resolvedAppointmentDetails(context, mutationRequest) {
  const resolvedContactId = mutationRequest?.contactId || context?.runtime?.stepOutputs?.search_appointment_contact_by_name?.data?.contacts?.[0]?.id || null;
  return {
    action: 'create_appointment',
    locationId: mutationRequest?.locationId || null,
    calendarId: mutationRequest?.calendarId || null,
    contactId: resolvedContactId,
    contactName: mutationRequest?.contactName || null,
    startTime: mutationRequest?.startTime || null,
    endTime: mutationRequest?.endTime || computedEndTime(mutationRequest?.startTime, mutationRequest?.durationMinutes),
    durationMinutes: mutationRequest?.durationMinutes ?? null,
    title: mutationRequest?.title || null,
    address: mutationRequest?.address || null,
    assignedUserId: mutationRequest?.assignedUserId || null,
    appointmentStatus: mutationRequest?.appointmentStatus || null,
    toNotify: mutationRequest?.toNotify
  };
}

function resolvedAppointmentMutationDetails(context, mutationRequest, action, extra = {}) {
  return {
    action,
    appointmentId: mutationRequest?.appointmentId || null,
    locationId: mutationRequest?.locationId || fetchedAppointment(context)?.locationId || null,
    calendarId: resolvedAppointmentCalendarId(context, mutationRequest),
    contactId: mutationRequest?.contactId || fetchedAppointment(context)?.contactId || null,
    startTime: mutationRequest?.startTime || null,
    endTime: mutationRequest?.endTime || computedEndTime(mutationRequest?.startTime, mutationRequest?.durationMinutes),
    durationMinutes: mutationRequest?.durationMinutes ?? null,
    title: mutationRequest?.title || null,
    address: mutationRequest?.address || null,
    assignedUserId: resolvedAppointmentAssignedUserId(context, mutationRequest),
    appointmentStatus: mutationRequest?.appointmentStatus || null,
    toNotify: mutationRequest?.toNotify,
    ...extra
  };
}

function resolvedAppointmentListDetails(context, mutationRequest) {
  return {
    action: 'list_appointments',
    locationId: mutationRequest?.locationId || null,
    calendarId: mutationRequest?.calendarId || null,
    contactId: resolvedAppointmentListContactId(context, mutationRequest),
    contactName: mutationRequest?.contactName || null,
    assignedUserId: resolvedAppointmentAssignedUserId(context, mutationRequest),
    appointmentStatus: mutationRequest?.appointmentStatus || null,
    query: appointmentListQuery(context, mutationRequest)
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
        const explicitContactFollowerAdd = mutationRequest?.action === 'add_contact_followers'
          && (Boolean(mutationRequest.contactId) || Boolean(mutationRequest.contactName))
          && mutationRequest.followers.length > 0;
        const explicitContactFollowerRemove = mutationRequest?.action === 'remove_contact_followers'
          && (Boolean(mutationRequest.contactId) || Boolean(mutationRequest.contactName))
          && mutationRequest.followers.length > 0;
        const explicitEnrichment = mutationRequest?.action === 'enrich_contact' && (mutationRequest.tags.length > 0 || Boolean(mutationRequest.note));
        return [
          {
            name: 'search_target_contact_by_name',
            kind: 'adapter_call',
            adapter: 'ContactsAdapter',
            method: 'listContacts',
            pathHint: '/contacts/',
            args: () => [context.credentialRef || defaultLocationCredential(context), {
              locationId: context.event.locationId || null,
              query: mutationRequest.contactName,
              limit: 5
            }],
            safe: true,
            mutation: false,
            requiresCredential: true,
            resumeData: contactSearchResumeData,
            details: {
              action: 'search_target_contact_by_name',
              contactName: mutationRequest?.contactName || null
            },
            skipIf: () => !(explicitContactFollowerAdd || explicitContactFollowerRemove) || Boolean(mutationRequest.contactId) || !mutationRequest.contactName
          },
          {
            name: 'fetch_contact',
            kind: 'adapter_call',
            adapter: 'ContactsAdapter',
            method: 'getContact',
            pathHint: '/contacts/:contactId',
            args: (runtimeContext) => [runtimeContext.credentialRef || defaultLocationCredential(runtimeContext), resolvedLeadContactId(runtimeContext, mutationRequest)],
            safe: true,
            mutation: false,
            requiresCredential: true,
            skipIf: () => !(contactId || explicitContactFollowerAdd || explicitContactFollowerRemove)
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
            name: 'add_contact_followers',
            kind: 'adapter_call',
            adapter: 'ContactsAdapter',
            method: 'addFollowers',
            httpMethod: 'POST',
            pathHint: '/contacts/:contactId/followers',
            args: (runtimeContext) => [runtimeContext.credentialRef || defaultLocationCredential(runtimeContext), resolvedLeadContactId(runtimeContext, mutationRequest), { followers: mutationRequest.followers }],
            safe: false,
            mutation: true,
            requiresApproval: true,
            requiresCredential: true,
            details: (runtimeContext) => resolvedLeadContactDetails(runtimeContext, mutationRequest, 'add_contact_followers'),
            skipIf: () => !explicitContactFollowerAdd
          },
          {
            name: 'remove_contact_followers',
            kind: 'adapter_call',
            adapter: 'ContactsAdapter',
            method: 'removeFollowers',
            httpMethod: 'DELETE',
            pathHint: '/contacts/:contactId/followers',
            args: (runtimeContext) => [runtimeContext.credentialRef || defaultLocationCredential(runtimeContext), resolvedLeadContactId(runtimeContext, mutationRequest), { followers: mutationRequest.followers }],
            safe: false,
            mutation: true,
            requiresApproval: true,
            requiresCredential: true,
            details: (runtimeContext) => resolvedLeadContactDetails(runtimeContext, mutationRequest, 'remove_contact_followers'),
            skipIf: () => !explicitContactFollowerRemove
          },
          {
            name: 'plan_follow_up_write_actions',
            kind: 'intent',
            safe: false,
            mutation: true,
            requiresApproval: true,
            details: { action: 'possible_tag_note_task_or_opportunity', contactId },
            skipIf: () => explicitContactUpdate || explicitContactCustomFieldUpdate || explicitWorkflowAdd || explicitWorkflowRemove || explicitTagAdd || explicitTagRemove || explicitTaskCreate || explicitTaskDelete || explicitTaskUpdate || explicitTaskComplete || explicitNoteAdd || explicitNoteDelete || explicitContactFollowerAdd || explicitContactFollowerRemove || explicitEnrichment
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
        const explicitOpportunityStatusUpdate = mutationRequest?.action === 'update_opportunity_status'
          && Boolean(mutationRequest.opportunityId)
          && Boolean(mutationRequest.status);
        const explicitOpportunityFollowerAdd = mutationRequest?.action === 'add_opportunity_followers'
          && Boolean(mutationRequest.opportunityId)
          && mutationRequest.followers.length > 0;
        const explicitOpportunityFollowerRemove = mutationRequest?.action === 'remove_opportunity_followers'
          && Boolean(mutationRequest.opportunityId)
          && mutationRequest.followers.length > 0;
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
            name: 'update_opportunity_status',
            kind: 'adapter_call',
            adapter: 'OpportunitiesAdapter',
            method: 'updateOpportunityStatus',
            httpMethod: 'PUT',
            pathHint: '/opportunities/:id/status',
            args: () => [context.credentialRef || defaultLocationCredential(context), mutationRequest.opportunityId, { status: mutationRequest.status }],
            safe: false,
            mutation: true,
            requiresApproval: true,
            requiresCredential: true,
            details: {
              action: 'update_opportunity_status',
              opportunityId: mutationRequest?.opportunityId || null,
              status: mutationRequest?.status || null
            },
            skipIf: () => !explicitOpportunityStatusUpdate
          },
          {
            name: 'add_opportunity_followers',
            kind: 'adapter_call',
            adapter: 'OpportunitiesAdapter',
            method: 'addFollowers',
            httpMethod: 'POST',
            pathHint: '/opportunities/:id/followers',
            args: () => [context.credentialRef || defaultLocationCredential(context), mutationRequest.opportunityId, { followers: mutationRequest.followers }],
            safe: false,
            mutation: true,
            requiresApproval: true,
            requiresCredential: true,
            details: {
              action: 'add_opportunity_followers',
              opportunityId: mutationRequest?.opportunityId || null,
              followers: mutationRequest?.followers || []
            },
            skipIf: () => !explicitOpportunityFollowerAdd
          },
          {
            name: 'remove_opportunity_followers',
            kind: 'adapter_call',
            adapter: 'OpportunitiesAdapter',
            method: 'removeFollowers',
            httpMethod: 'DELETE',
            pathHint: '/opportunities/:id/followers',
            args: () => [context.credentialRef || defaultLocationCredential(context), mutationRequest.opportunityId, { followers: mutationRequest.followers }],
            safe: false,
            mutation: true,
            requiresApproval: true,
            requiresCredential: true,
            details: {
              action: 'remove_opportunity_followers',
              opportunityId: mutationRequest?.opportunityId || null,
              followers: mutationRequest?.followers || []
            },
            skipIf: () => !explicitOpportunityFollowerRemove
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
            skipIf: () => explicitOpportunityCreate || explicitOpportunityUpdate || explicitOpportunityStatusUpdate || explicitOpportunityFollowerAdd || explicitOpportunityFollowerRemove || explicitOpportunityDelete
          }
        ];
      }
    },
    conversation_management_pack: {
      trigger_events: ['ConversationUpdate', 'ConversationUnreadWebhook', 'InboundMessage', 'OutboundMessage', 'ManualRun'],
      buildExecutionPlan(context) {
        const mutationRequest = conversationMutationRequestFrom(context.event);
        const conversationId = mutationRequest?.conversationId || objectIdFrom(context.event);
        const explicitConversationSend = mutationRequest?.action === 'send_conversation_message'
          && (Boolean(mutationRequest.contactId) || Boolean(mutationRequest.contactName))
          && Boolean(mutationRequest.message)
          && Boolean(mutationRequest.messageType);
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
            skipIf: () => !conversationId || explicitConversationSend
          },
          {
            name: 'search_contacts_by_name',
            kind: 'adapter_call',
            adapter: 'ContactsAdapter',
            method: 'listContacts',
            pathHint: '/contacts/',
            args: () => [context.credentialRef || defaultLocationCredential(context), {
              locationId: context.event.locationId || mutationRequest?.locationId || null,
              query: mutationRequest.contactName,
              limit: 5
            }],
            safe: true,
            mutation: false,
            requiresCredential: true,
            resumeData: contactSearchResumeData,
            details: {
              action: 'search_contacts_by_name',
              contactName: mutationRequest?.contactName || null
            },
            skipIf: () => !explicitConversationSend || Boolean(mutationRequest.contactId) || !mutationRequest.contactName
          },
          {
            name: 'send_conversation_message',
            kind: 'adapter_call',
            adapter: 'ConversationsAdapter',
            method: 'sendMessage',
            httpMethod: 'POST',
            pathHint: '/conversations/messages',
            args: (runtimeContext) => [runtimeContext.credentialRef || defaultLocationCredential(runtimeContext), {
              contactId: resolvedConversationContactId(runtimeContext, mutationRequest),
              message: mutationRequest.message,
              type: mutationRequest.messageType,
              ...(mutationRequest.channel ? { channel: mutationRequest.channel } : {}),
              ...(mutationRequest.fromNumber ? { fromNumber: mutationRequest.fromNumber } : {}),
              ...(mutationRequest.toNumber ? { toNumber: mutationRequest.toNumber } : {})
            }],
            safe: false,
            mutation: true,
            requiresApproval: true,
            requiresCredential: true,
            details: (runtimeContext) => resolvedConversationContactDetails(runtimeContext, mutationRequest),
            skipIf: () => !explicitConversationSend
          },
          {
            name: 'evaluate_sla_and_assignment',
            kind: 'intent',
            safe: true,
            mutation: false,
            details: { action: 'evaluate_response_rules', conversationId, mutationRequest },
            skipIf: () => explicitConversationSend
          }
        ];
      }
    },
    calendar_appointment_pack: {
      trigger_events: ['AppointmentCreate', 'AppointmentUpdate', 'AppointmentDelete', 'ManualRun'],
      buildExecutionPlan(context) {
        const mutationRequest = appointmentMutationRequestFrom(context.event);
        const explicitListAppointments = mutationRequest?.action === 'list_appointments';
        const explicitGetAppointment = mutationRequest?.action === 'get_appointment'
          && Boolean(mutationRequest.appointmentId);
        const explicitAppointmentCreate = mutationRequest?.action === 'create_appointment'
          && Boolean(mutationRequest.locationId)
          && Boolean(mutationRequest.calendarId)
          && (Boolean(mutationRequest.contactId) || Boolean(mutationRequest.contactName))
          && Boolean(mutationRequest.startTime);
        const explicitGetFreeSlots = mutationRequest?.action === 'get_free_slots'
          && Boolean(mutationRequest.calendarId || mutationRequest.appointmentId)
          && appointmentFreeSlotsRequested(mutationRequest);
        const explicitAppointmentReschedule = mutationRequest?.action === 'reschedule_appointment'
          && Boolean(mutationRequest.appointmentId)
          && Boolean(mutationRequest.startTime)
          && Object.keys(appointmentUpdateBody(context, mutationRequest)).length > 0;
        const explicitAppointmentUpdate = mutationRequest?.action === 'update_appointment'
          && Boolean(mutationRequest.appointmentId)
          && Object.keys(appointmentUpdateBody(context, mutationRequest)).length > 0;
        const explicitAppointmentCancel = mutationRequest?.action === 'cancel_appointment'
          && Boolean(mutationRequest.appointmentId);
        const explicitAppointmentDelete = mutationRequest?.action === 'delete_appointment'
          && Boolean(mutationRequest.appointmentId);
        const explicitAppointmentAvailabilityCheck = (explicitAppointmentUpdate || explicitAppointmentReschedule) && appointmentAvailabilityRequested(mutationRequest);
        return [
          {
            name: 'search_appointment_contact_by_name',
            kind: 'adapter_call',
            adapter: 'ContactsAdapter',
            method: 'listContacts',
            pathHint: '/contacts/',
            args: () => [context.credentialRef || defaultLocationCredential(context), {
              locationId: mutationRequest?.locationId || context.event.locationId || null,
              query: mutationRequest.contactName,
              limit: 5
            }],
            safe: true,
            mutation: false,
            requiresCredential: true,
            resumeData: contactSearchResumeData,
            details: {
              action: 'search_appointment_contact_by_name',
              contactName: mutationRequest?.contactName || null
            },
            skipIf: () => !(explicitAppointmentCreate || explicitListAppointments) || Boolean(mutationRequest.contactId) || !mutationRequest.contactName
          },
          {
            name: 'fetch_appointment',
            kind: 'adapter_call',
            adapter: 'CalendarsAdapter',
            method: 'getAppointment',
            pathHint: '/calendars/events/appointments/:eventId',
            args: () => [context.credentialRef || defaultLocationCredential(context), mutationRequest.appointmentId],
            safe: true,
            mutation: false,
            requiresCredential: true,
            resumeData: appointmentFetchResumeData,
            details: {
              action: explicitGetAppointment ? 'get_appointment' : 'fetch_appointment',
              appointmentId: mutationRequest?.appointmentId || null
            },
            skipIf: () => !mutationRequest?.appointmentId || explicitAppointmentCreate || explicitListAppointments
          },
          {
            name: 'list_contact_appointments',
            kind: 'adapter_call',
            adapter: 'ContactsAdapter',
            method: 'listAppointments',
            pathHint: '/contacts/:contactId/appointments',
            args: (runtimeContext) => [runtimeContext.credentialRef || defaultLocationCredential(runtimeContext), resolvedAppointmentListContactId(runtimeContext, mutationRequest)],
            safe: true,
            mutation: false,
            requiresCredential: true,
            resumeData: appointmentListResumeData,
            details: (runtimeContext) => ({
              ...resolvedAppointmentListDetails(runtimeContext, mutationRequest),
              path: '/contacts/:contactId/appointments',
              strategy: 'contact_appointments'
            }),
            skipIf: () => !explicitListAppointments || !resolvedAppointmentListContactId(context, mutationRequest)
          },
          {
            name: 'list_appointments',
            kind: 'adapter_call',
            adapter: 'CalendarsAdapter',
            method: 'listEvents',
            pathHint: '/calendars/events',
            args: (runtimeContext) => [runtimeContext.credentialRef || defaultLocationCredential(runtimeContext), appointmentListQuery(runtimeContext, mutationRequest)],
            safe: true,
            mutation: false,
            requiresCredential: true,
            resumeData: appointmentCalendarListResumeData,
            details: (runtimeContext) => ({
              ...resolvedAppointmentListDetails(runtimeContext, mutationRequest),
              path: '/calendars/events',
              strategy: 'calendar_events',
              bestEffort: true,
              recommendation: 'Prefer contact-scoped listing when contactId or contactName is available.'
            }),
            skipIf: () => !explicitListAppointments || Boolean(resolvedAppointmentListContactId(context, mutationRequest))
          },
          {
            name: 'check_requested_appointment_slot',
            kind: 'adapter_call',
            adapter: 'CalendarsAdapter',
            method: 'getFreeSlots',
            pathHint: '/calendars/:calendarId/free-slots',
            args: (runtimeContext) => [
              runtimeContext.credentialRef || defaultLocationCredential(runtimeContext),
              resolvedAppointmentCalendarId(runtimeContext, mutationRequest),
              appointmentAvailabilityQuery(runtimeContext, mutationRequest)
            ],
            safe: true,
            mutation: false,
            requiresCredential: true,
            resumeData: appointmentFreeSlotsResumeData,
            details: (runtimeContext) => ({
              action: 'check_requested_appointment_slot',
              appointmentId: mutationRequest?.appointmentId || null,
              calendarId: resolvedAppointmentCalendarId(runtimeContext, mutationRequest),
              requestedStartTime: mutationRequest?.startTime || null,
              requestedEndTime: mutationRequest?.endTime || computedEndTime(mutationRequest?.startTime, mutationRequest?.durationMinutes),
              assignedUserId: resolvedAppointmentAssignedUserId(runtimeContext, mutationRequest)
            }),
            skipIf: () => !explicitAppointmentAvailabilityCheck
          },
          {
            name: 'get_free_slots',
            kind: 'adapter_call',
            adapter: 'CalendarsAdapter',
            method: 'getFreeSlots',
            pathHint: '/calendars/:calendarId/free-slots',
            args: (runtimeContext) => [
              runtimeContext.credentialRef || defaultLocationCredential(runtimeContext),
              resolvedAppointmentCalendarId(runtimeContext, mutationRequest),
              appointmentFreeSlotsQuery(runtimeContext, mutationRequest)
            ],
            safe: true,
            mutation: false,
            requiresCredential: true,
            resumeData: appointmentFreeSlotsResumeData,
            details: (runtimeContext) => ({
              action: 'get_free_slots',
              appointmentId: mutationRequest?.appointmentId || null,
              calendarId: resolvedAppointmentCalendarId(runtimeContext, mutationRequest),
              query: appointmentFreeSlotsQuery(runtimeContext, mutationRequest)
            }),
            skipIf: () => !explicitGetFreeSlots
          },
          {
            name: 'refresh_calendar_event_window',
            kind: 'adapter_call',
            adapter: 'CalendarsAdapter',
            method: 'listEvents',
            pathHint: '/calendars/events',
            args: () => [context.credentialRef || defaultLocationCredential(context), {}],
            safe: true,
            mutation: false,
            requiresCredential: true,
            skipIf: () => explicitListAppointments || explicitGetAppointment || explicitAppointmentCreate || explicitGetFreeSlots || explicitAppointmentReschedule || explicitAppointmentUpdate || explicitAppointmentCancel || explicitAppointmentDelete
          },
          {
            name: 'create_appointment',
            kind: 'adapter_call',
            adapter: 'CalendarsAdapter',
            method: 'createAppointment',
            httpMethod: 'POST',
            pathHint: '/calendars/events/appointments',
            args: (runtimeContext) => [runtimeContext.credentialRef || defaultLocationCredential(runtimeContext), appointmentBody(runtimeContext, mutationRequest)],
            safe: false,
            mutation: true,
            requiresApproval: true,
            requiresCredential: true,
            details: (runtimeContext) => resolvedAppointmentDetails(runtimeContext, mutationRequest),
            skipIf: () => !explicitAppointmentCreate
          },
          {
            name: 'reschedule_appointment',
            kind: 'adapter_call',
            adapter: 'CalendarsAdapter',
            method: 'updateAppointment',
            httpMethod: 'PUT',
            pathHint: '/calendars/events/appointments/:eventId',
            args: (runtimeContext) => {
              ensureRequestedAppointmentSlotAvailable(runtimeContext, mutationRequest);
              return [runtimeContext.credentialRef || defaultLocationCredential(runtimeContext), mutationRequest.appointmentId, appointmentUpdateBody(runtimeContext, mutationRequest)];
            },
            safe: false,
            mutation: true,
            requiresApproval: true,
            requiresCredential: true,
            details: (runtimeContext) => resolvedAppointmentMutationDetails(runtimeContext, mutationRequest, 'reschedule_appointment', {
              availabilityCheck: ensureRequestedAppointmentSlotAvailable(runtimeContext, mutationRequest)
            }),
            skipIf: () => !explicitAppointmentReschedule
          },
          {
            name: 'update_appointment',
            kind: 'adapter_call',
            adapter: 'CalendarsAdapter',
            method: 'updateAppointment',
            httpMethod: 'PUT',
            pathHint: '/calendars/events/appointments/:eventId',
            args: (runtimeContext) => {
              ensureRequestedAppointmentSlotAvailable(runtimeContext, mutationRequest);
              return [runtimeContext.credentialRef || defaultLocationCredential(runtimeContext), mutationRequest.appointmentId, appointmentUpdateBody(runtimeContext, mutationRequest)];
            },
            safe: false,
            mutation: true,
            requiresApproval: true,
            requiresCredential: true,
            details: (runtimeContext) => resolvedAppointmentMutationDetails(runtimeContext, mutationRequest, 'update_appointment', {
              availabilityCheck: ensureRequestedAppointmentSlotAvailable(runtimeContext, mutationRequest)
            }),
            skipIf: () => !explicitAppointmentUpdate
          },
          {
            name: 'cancel_appointment',
            kind: 'adapter_call',
            adapter: 'CalendarsAdapter',
            method: 'updateAppointment',
            httpMethod: 'PUT',
            pathHint: '/calendars/events/appointments/:eventId',
            args: (runtimeContext) => [runtimeContext.credentialRef || defaultLocationCredential(runtimeContext), mutationRequest.appointmentId, appointmentUpdateBody(runtimeContext, mutationRequest, { forceCancelled: true })],
            safe: false,
            mutation: true,
            requiresApproval: true,
            requiresCredential: true,
            details: (runtimeContext) => resolvedAppointmentMutationDetails(runtimeContext, mutationRequest, 'cancel_appointment', { appointmentStatus: 'cancelled' }),
            skipIf: () => !explicitAppointmentCancel
          },
          {
            name: 'delete_appointment',
            kind: 'adapter_call',
            adapter: 'CalendarsAdapter',
            method: 'deleteEvent',
            httpMethod: 'DELETE',
            pathHint: '/calendars/events/:eventId',
            args: (runtimeContext) => [runtimeContext.credentialRef || defaultLocationCredential(runtimeContext), mutationRequest.appointmentId],
            safe: false,
            mutation: true,
            requiresApproval: true,
            requiresCredential: true,
            details: (runtimeContext) => resolvedAppointmentMutationDetails(runtimeContext, mutationRequest, 'delete_appointment'),
            skipIf: () => !explicitAppointmentDelete
          },
          {
            name: 'evaluate_booking_state',
            kind: 'intent',
            safe: true,
            mutation: false,
            details: { action: 'evaluate_booking_followups', locationId: context.event.locationId, mutationRequest },
            skipIf: () => explicitListAppointments || explicitGetAppointment || explicitAppointmentCreate || explicitGetFreeSlots || explicitAppointmentReschedule || explicitAppointmentUpdate || explicitAppointmentCancel || explicitAppointmentDelete
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
