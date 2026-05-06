import {
  AssociationsAdapter,
  BusinessesAdapter,
  CalendarsAdapter,
  ContactsAdapter,
  ConversationsAdapter,
  CustomMenusAdapter,
  FormsAdapter,
  InvoicesAdapter,
  LocationCustomFieldsAdapter,
  LocationCustomValuesAdapter,
  LocationsAdapter,
  MediaStorageAdapter,
  OpportunitiesAdapter,
  PaymentsAdapter,
  PhoneSystemAdapter,
  ProductsAdapter,
  SaasAdapter,
  SnapshotsAdapter,
  SurveysAdapter,
  TriggerLinksAdapter,
  UsersAdapter,
  VoiceAiAdapter,
  WorkflowsAdapter,
  SocialPlannerAdapter
} from './index.js';

export function buildAdapterExamples() {
  return {
    agency: {
      locations: new LocationsAdapter(),
      users: new UsersAdapter(),
      customMenus: new CustomMenusAdapter(),
      saas: new SaasAdapter(),
      snapshots: new SnapshotsAdapter()
    },
    location: {
      contacts: new ContactsAdapter(),
      conversations: new ConversationsAdapter(),
      opportunities: new OpportunitiesAdapter(),
      calendars: new CalendarsAdapter(),
      invoices: new InvoicesAdapter(),
      payments: new PaymentsAdapter(),
      products: new ProductsAdapter(),
      businesses: new BusinessesAdapter(),
      forms: new FormsAdapter(),
      surveys: new SurveysAdapter(),
      customFields: new LocationCustomFieldsAdapter(),
      customValues: new LocationCustomValuesAdapter(),
      associations: new AssociationsAdapter(),
      triggerLinks: new TriggerLinksAdapter(),
      mediaStorage: new MediaStorageAdapter(),
      phoneSystem: new PhoneSystemAdapter(),
      socialPlanner: new SocialPlannerAdapter(),
      voiceAi: new VoiceAiAdapter(),
      workflows: new WorkflowsAdapter()
    }
  };
}

export const EXAMPLE_CALLS = {
  getLocation: {
    adapter: 'LocationsAdapter',
    method: 'getLocation',
    args: ['agency-oauth', 'LOCATION_ID']
  },
  createContact: {
    adapter: 'ContactsAdapter',
    method: 'createContact',
    args: ['location-oauth', { firstName: 'Ada', lastName: 'Lovelace' }]
  },
  addContactTag: {
    adapter: 'ContactsAdapter',
    method: 'addTags',
    args: ['location-oauth', 'CONTACT_ID', { tags: ['openclaw-validation'] }]
  },
  updateContact: {
    adapter: 'ContactsAdapter',
    method: 'updateContact',
    args: ['location-oauth', 'CONTACT_ID', { website: 'https://openclaw-validation.invalid' }]
  },
  updateContactCustomFields: {
    adapter: 'ContactsAdapter',
    method: 'updateContact',
    args: ['location-oauth', 'CONTACT_ID', { customFields: [{ id: 'CUSTOM_FIELD_ID', value: 'OpenClaw validation value' }] }]
  },
  removeContactTag: {
    adapter: 'ContactsAdapter',
    method: 'removeTags',
    args: ['location-oauth', 'CONTACT_ID', { tags: ['openclaw-validation'] }]
  },
  addContactFollowers: {
    adapter: 'ContactsAdapter',
    method: 'addFollowers',
    args: ['location-oauth', 'CONTACT_ID', { followers: ['USER_ID'] }]
  },
  removeContactFollowers: {
    adapter: 'ContactsAdapter',
    method: 'removeFollowers',
    args: ['location-oauth', 'CONTACT_ID', { followers: ['USER_ID'] }]
  },
  createContactTask: {
    adapter: 'ContactsAdapter',
    method: 'createTask',
    args: ['location-oauth', 'CONTACT_ID', { title: 'OpenClaw validation task', body: 'Safe to delete', dueDate: '2026-05-07T00:00:00Z', completed: false }]
  },
  deleteContactTask: {
    adapter: 'ContactsAdapter',
    method: 'deleteTask',
    args: ['location-oauth', 'CONTACT_ID', 'TASK_ID']
  },
  updateContactTask: {
    adapter: 'ContactsAdapter',
    method: 'updateTask',
    args: ['location-oauth', 'CONTACT_ID', 'TASK_ID', { title: 'Updated task', body: 'Updated body', dueDate: '2026-05-07T12:00:00Z', completed: false }]
  },
  updateContactTaskCompleted: {
    adapter: 'ContactsAdapter',
    method: 'updateTaskCompleted',
    args: ['location-oauth', 'CONTACT_ID', 'TASK_ID', { completed: true }]
  },
  addContactNote: {
    adapter: 'ContactsAdapter',
    method: 'addNote',
    args: ['location-oauth', 'CONTACT_ID', { body: 'OpenClaw validation note', title: 'Validation', pinned: false }]
  },
  addContactToWorkflow: {
    adapter: 'ContactsAdapter',
    method: 'addToWorkflow',
    args: ['location-oauth', 'CONTACT_ID', 'WORKFLOW_ID']
  },
  removeContactFromWorkflow: {
    adapter: 'ContactsAdapter',
    method: 'removeFromWorkflow',
    args: ['location-oauth', 'CONTACT_ID', 'WORKFLOW_ID']
  },
  deleteContactNote: {
    adapter: 'ContactsAdapter',
    method: 'deleteNote',
    args: ['location-oauth', 'CONTACT_ID', 'NOTE_ID']
  },
  createOpportunity: {
    adapter: 'OpportunitiesAdapter',
    method: 'createOpportunity',
    args: ['location-oauth', { locationId: 'LOCATION_ID', contactId: 'CONTACT_ID', name: 'New Deal', pipelineId: 'PIPELINE_ID', pipelineStageId: 'STAGE_ID', status: 'open' }]
  },
  updateOpportunity: {
    adapter: 'OpportunitiesAdapter',
    method: 'updateOpportunity',
    args: ['location-oauth', 'OPPORTUNITY_ID', { name: 'Updated Deal', monetaryValue: 2500, status: 'open' }]
  },
  updateOpportunityStatus: {
    adapter: 'OpportunitiesAdapter',
    method: 'updateOpportunityStatus',
    args: ['location-oauth', 'OPPORTUNITY_ID', { status: 'won' }]
  },
  addOpportunityFollowers: {
    adapter: 'OpportunitiesAdapter',
    method: 'addFollowers',
    args: ['location-oauth', 'OPPORTUNITY_ID', { followers: ['USER_ID'] }]
  },
  removeOpportunityFollowers: {
    adapter: 'OpportunitiesAdapter',
    method: 'removeFollowers',
    args: ['location-oauth', 'OPPORTUNITY_ID', { followers: ['USER_ID'] }]
  },
  deleteOpportunity: {
    adapter: 'OpportunitiesAdapter',
    method: 'deleteOpportunity',
    args: ['location-oauth', 'OPPORTUNITY_ID']
  },
  sendConversationMessage: {
    adapter: 'ConversationsAdapter',
    method: 'sendMessage',
    args: ['location-oauth', { contactId: 'CONTACT_ID', message: 'OpenClaw validation message', type: 'SMS' }]
  },
  createAppointment: {
    adapter: 'CalendarsAdapter',
    method: 'createAppointment',
    args: ['location-oauth', {
      calendarId: 'CALENDAR_ID',
      locationId: 'LOCATION_ID',
      contactId: 'CONTACT_ID',
      startTime: '2026-05-07T15:00:00Z',
      endTime: '2026-05-07T15:30:00Z',
      title: 'OpenClaw validation appointment'
    }]
  },
  getAppointment: {
    adapter: 'CalendarsAdapter',
    method: 'getAppointment',
    args: ['location-oauth', 'APPOINTMENT_ID']
  },
  listAppointments: {
    adapter: 'CalendarsAdapter',
    method: 'listEvents',
    args: ['location-oauth', { locationId: 'LOCATION_ID', calendarId: 'CALENDAR_ID', startTime: '2026-05-14T00:00:00-05:00', endTime: '2026-05-15T00:00:00-05:00' }]
  },
  listContactAppointments: {
    adapter: 'ContactsAdapter',
    method: 'listAppointments',
    args: ['location-oauth', 'CONTACT_ID']
  },
  getFreeSlots: {
    adapter: 'CalendarsAdapter',
    method: 'getFreeSlots',
    args: ['location-oauth', 'CALENDAR_ID', { startDate: 1778716800000, endDate: 1778803200000, userId: 'USER_ID', timezone: 'America/Chicago' }]
  },
  updateAppointment: {
    adapter: 'CalendarsAdapter',
    method: 'updateAppointment',
    args: ['location-oauth', 'APPOINTMENT_ID', {
      startTime: '2026-05-07T16:00:00Z',
      endTime: '2026-05-07T16:30:00Z',
      title: 'Updated OpenClaw validation appointment',
      appointmentStatus: 'confirmed'
    }]
  },
  rescheduleAppointment: {
    adapter: 'CalendarsAdapter',
    method: 'updateAppointment',
    args: ['location-oauth', 'APPOINTMENT_ID', {
      startTime: '2026-05-07T17:00:00Z',
      endTime: '2026-05-07T17:30:00Z',
      title: 'Rescheduled OpenClaw validation appointment'
    }]
  },
  deleteEvent: {
    adapter: 'CalendarsAdapter',
    method: 'deleteEvent',
    args: ['location-oauth', 'APPOINTMENT_ID']
  },
  createInvoice: {
    adapter: 'InvoicesAdapter',
    method: 'createInvoice',
    args: ['location-oauth', { contactId: 'CONTACT_ID' }]
  },
  listForms: {
    adapter: 'FormsAdapter',
    method: 'listForms',
    args: ['location-oauth', { locationId: 'LOCATION_ID' }]
  },
  getCustomField: {
    adapter: 'LocationCustomFieldsAdapter',
    method: 'getCustomField',
    args: ['location-oauth', 'LOCATION_ID', 'CUSTOM_FIELD_ID']
  },
  searchTriggerLinks: {
    adapter: 'TriggerLinksAdapter',
    method: 'searchTriggerLinks',
    args: ['location-oauth', { locationId: 'LOCATION_ID', query: 'promo' }]
  },
  listSocialAccounts: {
    adapter: 'SocialPlannerAdapter',
    method: 'listAccounts',
    args: ['location-oauth', 'LOCATION_ID', {}]
  },
  createSocialPost: {
    adapter: 'SocialPlannerAdapter',
    method: 'createPost',
    args: ['location-oauth', 'LOCATION_ID', {
      accountIds: ['ACCOUNT_ID'],
      summary: 'OpenClaw validation draft social post',
      status: 'draft'
    }]
  },
  uploadMediaFile: {
    adapter: 'MediaStorageAdapter',
    method: 'uploadFile',
    args: ['location-oauth', { hosted: true, fileUrl: 'https://example.com/logo.png' }]
  }
};
