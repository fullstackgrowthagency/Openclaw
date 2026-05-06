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
  removeContactTag: {
    adapter: 'ContactsAdapter',
    method: 'removeTags',
    args: ['location-oauth', 'CONTACT_ID', { tags: ['openclaw-validation'] }]
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
  addContactNote: {
    adapter: 'ContactsAdapter',
    method: 'addNote',
    args: ['location-oauth', 'CONTACT_ID', { body: 'OpenClaw validation note', title: 'Validation', pinned: false }]
  },
  deleteContactNote: {
    adapter: 'ContactsAdapter',
    method: 'deleteNote',
    args: ['location-oauth', 'CONTACT_ID', 'NOTE_ID']
  },
  createOpportunity: {
    adapter: 'OpportunitiesAdapter',
    method: 'createOpportunity',
    args: ['location-oauth', { name: 'New Deal', pipelineId: 'PIPELINE_ID' }]
  },
  createAppointment: {
    adapter: 'CalendarsAdapter',
    method: 'createAppointment',
    args: ['location-oauth', { calendarId: 'CALENDAR_ID' }]
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
  uploadMediaFile: {
    adapter: 'MediaStorageAdapter',
    method: 'uploadFile',
    args: ['location-oauth', { hosted: true, fileUrl: 'https://example.com/logo.png' }]
  }
};
