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
}
