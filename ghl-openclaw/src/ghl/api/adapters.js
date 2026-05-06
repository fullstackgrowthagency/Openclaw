import { GhlApiClient } from './client.js';

export class LocationsAdapter {
  constructor({ client = new GhlApiClient() } = {}) {
    this.client = client;
  }

  getLocation(credentialRef, locationId) {
    return this.client.request({ credentialRef, method: 'GET', path: `/locations/${locationId}` });
  }

  searchLocations(credentialRef, query = {}) {
    return this.client.request({ credentialRef, method: 'GET', path: '/locations/search', query });
  }

  createLocation(credentialRef, body) {
    return this.client.request({ credentialRef, method: 'POST', path: '/locations/', body });
  }

  updateLocation(credentialRef, locationId, body) {
    return this.client.request({ credentialRef, method: 'PUT', path: `/locations/${locationId}`, body });
  }

  deleteLocation(credentialRef, locationId) {
    return this.client.request({ credentialRef, method: 'DELETE', path: `/locations/${locationId}` });
  }
}

export class ContactsAdapter {
  constructor({ client = new GhlApiClient() } = {}) {
    this.client = client;
  }

  listContacts(credentialRef, query = {}) {
    return this.client.request({ credentialRef, method: 'GET', path: '/contacts/', query });
  }

  getContact(credentialRef, contactId) {
    return this.client.request({ credentialRef, method: 'GET', path: `/contacts/${contactId}` });
  }

  createContact(credentialRef, body) {
    return this.client.request({ credentialRef, method: 'POST', path: '/contacts/', body });
  }

  updateContact(credentialRef, contactId, body) {
    return this.client.request({ credentialRef, method: 'PUT', path: `/contacts/${contactId}`, body });
  }

  deleteContact(credentialRef, contactId) {
    return this.client.request({ credentialRef, method: 'DELETE', path: `/contacts/${contactId}` });
  }

  listTasks(credentialRef, contactId) {
    return this.client.request({ credentialRef, method: 'GET', path: `/contacts/${contactId}/tasks` });
  }

  createTask(credentialRef, contactId, body) {
    return this.client.request({ credentialRef, method: 'POST', path: `/contacts/${contactId}/tasks`, body });
  }

  addNote(credentialRef, contactId, body) {
    return this.client.request({ credentialRef, method: 'POST', path: `/contacts/${contactId}/notes`, body });
  }

  addTags(credentialRef, contactId, body) {
    return this.client.request({ credentialRef, method: 'POST', path: `/contacts/${contactId}/tags`, body });
  }
}

export class ConversationsAdapter {
  constructor({ client = new GhlApiClient() } = {}) {
    this.client = client;
  }

  getConversation(credentialRef, conversationId) {
    return this.client.request({ credentialRef, method: 'GET', path: `/conversations/${conversationId}` });
  }

  createConversation(credentialRef, body) {
    return this.client.request({ credentialRef, method: 'POST', path: '/conversations/', body });
  }

  updateConversation(credentialRef, conversationId, body) {
    return this.client.request({ credentialRef, method: 'PUT', path: `/conversations/${conversationId}`, body });
  }

  deleteConversation(credentialRef, conversationId) {
    return this.client.request({ credentialRef, method: 'DELETE', path: `/conversations/${conversationId}` });
  }

  sendMessage(credentialRef, body) {
    return this.client.request({ credentialRef, method: 'POST', path: '/conversations/messages', body });
  }
}

export class OpportunitiesAdapter {
  constructor({ client = new GhlApiClient() } = {}) {
    this.client = client;
  }

  getOpportunity(credentialRef, id) {
    return this.client.request({ credentialRef, method: 'GET', path: `/opportunities/${id}` });
  }

  createOpportunity(credentialRef, body) {
    return this.client.request({ credentialRef, method: 'POST', path: '/opportunities', body });
  }

  updateOpportunity(credentialRef, id, body) {
    return this.client.request({ credentialRef, method: 'PUT', path: `/opportunities/${id}`, body });
  }

  deleteOpportunity(credentialRef, id) {
    return this.client.request({ credentialRef, method: 'DELETE', path: `/opportunities/${id}` });
  }
}

export class UsersAdapter {
  constructor({ client = new GhlApiClient() } = {}) {
    this.client = client;
  }

  listUsers(credentialRef, query = {}) {
    return this.client.request({ credentialRef, method: 'GET', path: '/users/', query });
  }

  createUser(credentialRef, body) {
    return this.client.request({ credentialRef, method: 'POST', path: '/users/', body });
  }

  updateUser(credentialRef, userId, body) {
    return this.client.request({ credentialRef, method: 'PUT', path: `/users/${userId}`, body });
  }

  deleteUser(credentialRef, userId) {
    return this.client.request({ credentialRef, method: 'DELETE', path: `/users/${userId}` });
  }
}

export class CalendarsAdapter {
  constructor({ client = new GhlApiClient() } = {}) {
    this.client = client;
  }

  listCalendars(credentialRef, query = {}) {
    return this.client.request({ credentialRef, method: 'GET', path: '/calendars/', query });
  }

  createCalendar(credentialRef, body) {
    return this.client.request({ credentialRef, method: 'POST', path: '/calendars/', body });
  }

  listEvents(credentialRef, query = {}) {
    return this.client.request({ credentialRef, method: 'GET', path: '/calendars/events', query });
  }

  createAppointment(credentialRef, body) {
    return this.client.request({ credentialRef, method: 'POST', path: '/calendars/events/appointments', body });
  }
}

export class InvoicesAdapter {
  constructor({ client = new GhlApiClient() } = {}) {
    this.client = client;
  }

  listInvoices(credentialRef, query = {}) {
    return this.client.request({ credentialRef, method: 'GET', path: '/invoices/', query });
  }

  createInvoice(credentialRef, body) {
    return this.client.request({ credentialRef, method: 'POST', path: '/invoices', body });
  }

  sendInvoice(credentialRef, invoiceId, body = {}) {
    return this.client.request({ credentialRef, method: 'POST', path: `/invoices/${invoiceId}/send`, body });
  }

  voidInvoice(credentialRef, invoiceId, body = {}) {
    return this.client.request({ credentialRef, method: 'POST', path: `/invoices/${invoiceId}/void`, body });
  }
}

export class PaymentsAdapter {
  constructor({ client = new GhlApiClient() } = {}) {
    this.client = client;
  }

  listOrders(credentialRef, query = {}) {
    return this.client.request({ credentialRef, method: 'GET', path: '/payments/orders/', query });
  }

  listTransactions(credentialRef, query = {}) {
    return this.client.request({ credentialRef, method: 'GET', path: '/payments/transactions/', query });
  }

  listSubscriptions(credentialRef, query = {}) {
    return this.client.request({ credentialRef, method: 'GET', path: '/payments/subscriptions/', query });
  }
}

export class ProductsAdapter {
  constructor({ client = new GhlApiClient() } = {}) {
    this.client = client;
  }

  listProducts(credentialRef, query = {}) {
    return this.client.request({ credentialRef, method: 'GET', path: '/products/', query });
  }

  createProduct(credentialRef, body) {
    return this.client.request({ credentialRef, method: 'POST', path: '/products/', body });
  }

  createPrice(credentialRef, productId, body) {
    return this.client.request({ credentialRef, method: 'POST', path: `/products/${productId}/price/`, body });
  }
}

export class SnapshotsAdapter {
  constructor({ client = new GhlApiClient() } = {}) {
    this.client = client;
  }

  listSnapshots(credentialRef, query = {}) {
    return this.client.request({ credentialRef, method: 'GET', path: '/snapshots', query });
  }

  createShareLink(credentialRef, body) {
    return this.client.request({ credentialRef, method: 'POST', path: '/snapshots/share/link', body });
  }
}

export class SocialPlannerAdapter {
  constructor({ client = new GhlApiClient() } = {}) {
    this.client = client;
  }

  listAccounts(credentialRef, locationId, query = {}) {
    return this.client.request({ credentialRef, method: 'GET', path: `/social-media-posting/${locationId}/accounts`, query });
  }

  createPost(credentialRef, locationId, body) {
    return this.client.request({ credentialRef, method: 'POST', path: `/social-media-posting/${locationId}/posts`, body });
  }
}

export class VoiceAiAdapter {
  constructor({ client = new GhlApiClient() } = {}) {
    this.client = client;
  }

  listCallLogs(credentialRef, query = {}) {
    return this.client.request({ credentialRef, method: 'GET', path: '/voice-ai/dashboard/call-logs', query });
  }

  listAgents(credentialRef, query = {}) {
    return this.client.request({ credentialRef, method: 'GET', path: '/voice-ai/agents', query });
  }

  createAgent(credentialRef, body) {
    return this.client.request({ credentialRef, method: 'POST', path: '/voice-ai/agents', body });
  }
}

export class WorkflowsAdapter {
  constructor({ client = new GhlApiClient() } = {}) {
    this.client = client;
  }

  listWorkflows(credentialRef, query = {}) {
    return this.client.request({ credentialRef, method: 'GET', path: '/workflows/', query });
  }
}
