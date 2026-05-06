import {
  CalendarsAdapter,
  ContactsAdapter,
  ConversationsAdapter,
  InvoicesAdapter,
  LocationsAdapter,
  OpportunitiesAdapter,
  PaymentsAdapter,
  ProductsAdapter,
  SnapshotsAdapter,
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
  }
};
