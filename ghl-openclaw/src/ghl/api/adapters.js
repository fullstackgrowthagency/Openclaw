import path from 'node:path';
import { readFile } from 'node:fs/promises';
import { GhlApiClient } from './client.js';
import { generateSocialPostCreative } from '../previews/social-post-creative.js';

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

  listAppointments(credentialRef, contactId) {
    return this.client.request({ credentialRef, method: 'GET', path: `/contacts/${contactId}/appointments` });
  }

  createTask(credentialRef, contactId, body) {
    return this.client.request({ credentialRef, method: 'POST', path: `/contacts/${contactId}/tasks`, body });
  }

  deleteTask(credentialRef, contactId, taskId) {
    return this.client.request({ credentialRef, method: 'DELETE', path: `/contacts/${contactId}/tasks/${taskId}` });
  }

  updateTask(credentialRef, contactId, taskId, body) {
    return this.client.request({ credentialRef, method: 'PUT', path: `/contacts/${contactId}/tasks/${taskId}`, body });
  }

  updateTaskCompleted(credentialRef, contactId, taskId, body) {
    return this.client.request({ credentialRef, method: 'PUT', path: `/contacts/${contactId}/tasks/${taskId}/completed`, body });
  }

  listNotes(credentialRef, contactId) {
    return this.client.request({ credentialRef, method: 'GET', path: `/contacts/${contactId}/notes` });
  }

  addNote(credentialRef, contactId, body) {
    return this.client.request({ credentialRef, method: 'POST', path: `/contacts/${contactId}/notes`, body });
  }

  deleteNote(credentialRef, contactId, noteId) {
    return this.client.request({ credentialRef, method: 'DELETE', path: `/contacts/${contactId}/notes/${noteId}` });
  }

  addTags(credentialRef, contactId, body) {
    return this.client.request({ credentialRef, method: 'POST', path: `/contacts/${contactId}/tags`, body });
  }

  removeTags(credentialRef, contactId, body) {
    return this.client.request({ credentialRef, method: 'DELETE', path: `/contacts/${contactId}/tags`, body });
  }

  addFollowers(credentialRef, contactId, body) {
    return this.client.request({ credentialRef, method: 'POST', path: `/contacts/${contactId}/followers`, body });
  }

  removeFollowers(credentialRef, contactId, body) {
    return this.client.request({ credentialRef, method: 'DELETE', path: `/contacts/${contactId}/followers`, body });
  }

  addToWorkflow(credentialRef, contactId, workflowId) {
    return this.client.request({ credentialRef, method: 'POST', path: `/contacts/${contactId}/workflow/${workflowId}` });
  }

  removeFromWorkflow(credentialRef, contactId, workflowId) {
    return this.client.request({ credentialRef, method: 'DELETE', path: `/contacts/${contactId}/workflow/${workflowId}` });
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
    return this.client.request({ credentialRef, method: 'POST', path: '/opportunities/', body });
  }

  updateOpportunity(credentialRef, id, body) {
    return this.client.request({ credentialRef, method: 'PUT', path: `/opportunities/${id}`, body });
  }

  updateOpportunityStatus(credentialRef, id, body) {
    return this.client.request({ credentialRef, method: 'PUT', path: `/opportunities/${id}/status`, body });
  }

  addFollowers(credentialRef, id, body) {
    return this.client.request({ credentialRef, method: 'POST', path: `/opportunities/${id}/followers`, body });
  }

  removeFollowers(credentialRef, id, body) {
    return this.client.request({ credentialRef, method: 'DELETE', path: `/opportunities/${id}/followers`, body });
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

  getFreeSlots(credentialRef, calendarId, query = {}) {
    return this.client.request({
      credentialRef,
      method: 'GET',
      path: `/calendars/${calendarId}/free-slots`,
      query,
      version: '2023-02-21'
    });
  }

  createCalendar(credentialRef, body) {
    return this.client.request({ credentialRef, method: 'POST', path: '/calendars/', body });
  }

  listEvents(credentialRef, query = {}) {
    return this.client.request({ credentialRef, method: 'GET', path: '/calendars/events', query });
  }

  getAppointment(credentialRef, eventId) {
    return this.client.request({ credentialRef, method: 'GET', path: `/calendars/events/appointments/${eventId}` });
  }

  createAppointment(credentialRef, body) {
    return this.client.request({ credentialRef, method: 'POST', path: '/calendars/events/appointments', body });
  }

  updateAppointment(credentialRef, eventId, body) {
    return this.client.request({ credentialRef, method: 'PUT', path: `/calendars/events/appointments/${eventId}`, body });
  }

  deleteEvent(credentialRef, eventId) {
    return this.client.request({ credentialRef, method: 'DELETE', path: `/calendars/events/${eventId}` });
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

export class BusinessesAdapter {
  constructor({ client = new GhlApiClient() } = {}) {
    this.client = client;
  }

  listBusinesses(credentialRef, query = {}) {
    return this.client.request({ credentialRef, method: 'GET', path: '/businesses/', query });
  }

  getBusiness(credentialRef, businessId) {
    return this.client.request({ credentialRef, method: 'GET', path: `/businesses/${businessId}` });
  }

  createBusiness(credentialRef, body) {
    return this.client.request({ credentialRef, method: 'POST', path: '/businesses/', body });
  }

  updateBusiness(credentialRef, businessId, body) {
    return this.client.request({ credentialRef, method: 'PUT', path: `/businesses/${businessId}`, body });
  }

  deleteBusiness(credentialRef, businessId) {
    return this.client.request({ credentialRef, method: 'DELETE', path: `/businesses/${businessId}` });
  }
}

export class FormsAdapter {
  constructor({ client = new GhlApiClient() } = {}) {
    this.client = client;
  }

  listForms(credentialRef, query = {}) {
    return this.client.request({ credentialRef, method: 'GET', path: '/forms/', query });
  }

  listFormSubmissions(credentialRef, query = {}) {
    return this.client.request({ credentialRef, method: 'GET', path: '/forms/submissions', query });
  }
}

export class SurveysAdapter {
  constructor({ client = new GhlApiClient() } = {}) {
    this.client = client;
  }

  listSurveys(credentialRef, query = {}) {
    return this.client.request({ credentialRef, method: 'GET', path: '/surveys/', query });
  }

  listSurveySubmissions(credentialRef, query = {}) {
    return this.client.request({ credentialRef, method: 'GET', path: '/surveys/submissions', query });
  }
}

export class CustomMenusAdapter {
  constructor({ client = new GhlApiClient() } = {}) {
    this.client = client;
  }

  listCustomMenus(credentialRef, query = {}) {
    return this.client.request({ credentialRef, method: 'GET', path: '/custom-menus/', query });
  }

  getCustomMenu(credentialRef, customMenuId) {
    return this.client.request({ credentialRef, method: 'GET', path: `/custom-menus/${customMenuId}` });
  }

  createCustomMenu(credentialRef, body) {
    return this.client.request({ credentialRef, method: 'POST', path: '/custom-menus/', body });
  }

  updateCustomMenu(credentialRef, customMenuId, body) {
    return this.client.request({ credentialRef, method: 'PUT', path: `/custom-menus/${customMenuId}`, body });
  }

  deleteCustomMenu(credentialRef, customMenuId) {
    return this.client.request({ credentialRef, method: 'DELETE', path: `/custom-menus/${customMenuId}` });
  }
}

export class LocationCustomFieldsAdapter {
  constructor({ client = new GhlApiClient() } = {}) {
    this.client = client;
  }

  listCustomFields(credentialRef, locationId, query = {}) {
    return this.client.request({ credentialRef, method: 'GET', path: `/locations/${locationId}/customFields`, query });
  }

  getCustomField(credentialRef, locationId, id) {
    return this.client.request({ credentialRef, method: 'GET', path: `/locations/${locationId}/customFields/${id}` });
  }

  createCustomField(credentialRef, locationId, body) {
    return this.client.request({ credentialRef, method: 'POST', path: `/locations/${locationId}/customFields`, body });
  }

  updateCustomField(credentialRef, locationId, id, body) {
    return this.client.request({ credentialRef, method: 'PUT', path: `/locations/${locationId}/customFields/${id}`, body });
  }

  deleteCustomField(credentialRef, locationId, id) {
    return this.client.request({ credentialRef, method: 'DELETE', path: `/locations/${locationId}/customFields/${id}` });
  }
}

export class LocationCustomValuesAdapter {
  constructor({ client = new GhlApiClient() } = {}) {
    this.client = client;
  }

  listCustomValues(credentialRef, locationId, query = {}) {
    return this.client.request({ credentialRef, method: 'GET', path: `/locations/${locationId}/customValues`, query });
  }

  getCustomValue(credentialRef, locationId, id) {
    return this.client.request({ credentialRef, method: 'GET', path: `/locations/${locationId}/customValues/${id}` });
  }

  createCustomValue(credentialRef, locationId, body) {
    return this.client.request({ credentialRef, method: 'POST', path: `/locations/${locationId}/customValues`, body });
  }

  updateCustomValue(credentialRef, locationId, id, body) {
    return this.client.request({ credentialRef, method: 'PUT', path: `/locations/${locationId}/customValues/${id}`, body });
  }

  deleteCustomValue(credentialRef, locationId, id) {
    return this.client.request({ credentialRef, method: 'DELETE', path: `/locations/${locationId}/customValues/${id}` });
  }
}

export class AssociationsAdapter {
  constructor({ client = new GhlApiClient() } = {}) {
    this.client = client;
  }

  createRelation(credentialRef, body) {
    return this.client.request({ credentialRef, method: 'POST', path: '/associations/relations', body });
  }

  listRelationsByRecord(credentialRef, recordId, query = {}) {
    return this.client.request({ credentialRef, method: 'GET', path: `/associations/relations/${recordId}`, query });
  }

  deleteRelation(credentialRef, relationId) {
    return this.client.request({ credentialRef, method: 'DELETE', path: `/associations/relations/${relationId}` });
  }
}

export class TriggerLinksAdapter {
  constructor({ client = new GhlApiClient() } = {}) {
    this.client = client;
  }

  searchTriggerLinks(credentialRef, query = {}) {
    return this.client.request({ credentialRef, method: 'GET', path: '/links/search', query });
  }
}

export class MediaStorageAdapter {
  constructor({ client = new GhlApiClient() } = {}) {
    this.client = client;
  }

  listMedia(credentialRef, query = {}) {
    return this.client.request({ credentialRef, method: 'GET', path: '/medias/files', query });
  }

  uploadFile(credentialRef, body) {
    return this.client.request({ credentialRef, method: 'POST', path: '/medias/upload-file', body });
  }

  deleteMediaObject(credentialRef, id) {
    return this.client.request({ credentialRef, method: 'DELETE', path: `/medias/${id}` });
  }

  updateMediaObject(credentialRef, id, body) {
    return this.client.request({ credentialRef, method: 'POST', path: `/medias/${id}`, body });
  }

  createFolder(credentialRef, body) {
    return this.client.request({ credentialRef, method: 'POST', path: '/medias/folder', body });
  }
}

export class PhoneSystemAdapter {
  constructor({ client = new GhlApiClient() } = {}) {
    this.client = client;
  }

  listNumberPools(credentialRef, query = {}) {
    return this.client.request({ credentialRef, method: 'GET', path: '/phone-system/number-pools', query });
  }
}

export class SaasAdapter {
  constructor({ client = new GhlApiClient() } = {}) {
    this.client = client;
  }

  enableLocation(credentialRef, body) {
    return this.client.request({ credentialRef, method: 'POST', path: '/enable-saas/', body });
  }

  bulkDisable(credentialRef, body) {
    return this.client.request({ credentialRef, method: 'POST', path: '/bulk-disable-saas/', body });
  }

  updateSubscription(credentialRef, body) {
    return this.client.request({ credentialRef, method: 'PUT', path: '/update-saas-subscription/', body });
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

  getPost(credentialRef, locationId, postId, query = {}) {
    return this.client.request({ credentialRef, method: 'GET', path: `/social-media-posting/${locationId}/posts/${postId}`, query });
  }

  async uploadLocalMediaFile(credentialRef, filePath, { mimeType = 'image/png', filename } = {}) {
    const token = await this.client.credentialBroker.getResolvedAccessToken(credentialRef);
    const bytes = await readFile(filePath);
    const form = new FormData();
    form.set('file', new Blob([bytes], { type: mimeType }), filename || path.basename(filePath));
    form.set('hosted', 'false');

    const response = await fetch(`${this.client.env.apiBaseUrl}/medias/upload-file`, {
      method: 'POST',
      headers: {
        Accept: 'application/json',
        Authorization: `Bearer ${token}`,
        Version: '2023-02-21'
      },
      body: form
    });

    const text = await response.text();
    let data;
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }

    return {
      ok: response.ok,
      status: response.status,
      headers: Object.fromEntries(response.headers.entries()),
      data
    };
  }

  async createPostWithCreative(credentialRef, locationId, body, options = {}) {
    const payload = JSON.parse(JSON.stringify(body || {}));
    delete payload.businessName;
    const existingMedia = Array.isArray(payload.media) ? payload.media.filter(Boolean) : [];
    let creative = null;

    if (existingMedia.length === 0 && options.generateCreative !== false) {
      const generated = await generateSocialPostCreative({
        locationId,
        post: payload,
        businessName: options.businessName,
        outputDir: options.outputDir
      });
      const uploaded = await this.uploadLocalMediaFile(credentialRef, generated.pngPath, {
        mimeType: 'image/png',
        filename: path.basename(generated.pngPath)
      });

      const hostedUrl = uploaded?.data?.url || uploaded?.data?.fileUrl || null;
      if (!uploaded?.ok || !hostedUrl) {
        throw new Error(`Failed to upload generated social creative${uploaded?.status ? ` (${uploaded.status})` : ''}.`);
      }

      payload.media = [{ url: hostedUrl, type: 'image' }];
      creative = {
        generated,
        uploaded: uploaded.data,
        media: payload.media
      };
    }

    const created = await this.createPost(credentialRef, locationId, payload);
    return {
      ...created,
      data: {
        ...(created?.data && typeof created.data === 'object' ? created.data : {}),
        openClaw: {
          creative,
          requestBody: payload
        }
      }
    };
  }

  createPost(credentialRef, locationId, body) {
    return this.client.request({ credentialRef, method: 'POST', path: `/social-media-posting/${locationId}/posts`, body });
  }
}

export class FacebookAdsAdapter {
  constructor({ client = new GhlApiClient() } = {}) {
    this.client = client;
  }

  listEntities(credentialRef, query = {}) {
    return this.client.request({ credentialRef, method: 'GET', path: '/ad-publishing/facebook/entity', query });
  }

  getCampaign(credentialRef, campaignId, query = {}) {
    return this.client.request({ credentialRef, method: 'GET', path: `/ad-publishing/facebook/campaigns/${campaignId}`, query });
  }

  upsertCampaign(credentialRef, body) {
    return this.client.request({ credentialRef, method: 'PUT', path: '/ad-publishing/facebook/campaigns', body });
  }

  upsertAdset(credentialRef, body) {
    return this.client.request({ credentialRef, method: 'PUT', path: '/ad-publishing/facebook/adsets', body });
  }

  upsertAd(credentialRef, body) {
    return this.client.request({ credentialRef, method: 'PUT', path: '/ad-publishing/facebook/ads', body });
  }

  publishCampaign(credentialRef, campaignId, body = {}) {
    return this.client.request({ credentialRef, method: 'POST', path: `/ad-publishing/facebook/campaigns/${campaignId}/publish`, body });
  }
}

export class GoogleAdsAdapter {
  constructor({ client = new GhlApiClient() } = {}) {
    this.client = client;
  }

  static publishReadiness(data) {
    const campaign = data && typeof data === 'object' ? data : null;
    const adGroups = Array.isArray(campaign?.adGroups) ? campaign.adGroups : [];
    const adContent = adGroups.flatMap((group) => Array.isArray(group?.adContent) ? group.adContent : []);
    const images = Array.isArray(campaign?.assets?.images) ? campaign.assets.images : [];
    const missing = [];

    if (!campaign?.id) missing.push('campaign.id');
    if (adGroups.length === 0) missing.push('adGroups');
    if (adContent.length === 0) missing.push('adContent');
    if (images.length === 0) missing.push('assets.images');
    if (!adContent.some((entry) => Array.isArray(entry?.headlines) && entry.headlines.length > 0)) missing.push('adContent.headlines');
    if (!adContent.some((entry) => Array.isArray(entry?.descriptions) && entry.descriptions.length > 0)) missing.push('adContent.descriptions');
    if (!adContent.some((entry) => typeof entry?.finalUrl === 'string' && entry.finalUrl.trim())) missing.push('adContent.finalUrl');

    return {
      ready: missing.length === 0,
      missing,
      counts: {
        adGroups: adGroups.length,
        adContent: adContent.length,
        images: images.length
      }
    };
  }

  getIntegration(credentialRef, query = {}) {
    return this.client.request({ credentialRef, method: 'GET', path: '/ad-publishing/google/integration', query });
  }

  listEntities(credentialRef, query = {}) {
    return this.client.request({ credentialRef, method: 'GET', path: '/ad-publishing/google/entity', query });
  }

  getCampaign(credentialRef, adId, query = {}) {
    return this.client.request({ credentialRef, method: 'GET', path: `/ad-publishing/google/ads/${adId}`, query });
  }

  getAssets(credentialRef, query = {}) {
    return this.client.request({ credentialRef, method: 'GET', path: '/ad-publishing/google/assets', query });
  }

  searchTargeting(credentialRef, query = {}) {
    return this.client.request({ credentialRef, method: 'GET', path: '/ad-publishing/google/targeting/search', query });
  }

  getTargetInterests(credentialRef, query = {}) {
    return this.client.request({ credentialRef, method: 'GET', path: '/ad-publishing/google/target-interests', query });
  }

  getSegments(credentialRef, query = {}) {
    return this.client.request({ credentialRef, method: 'GET', path: '/ad-publishing/google/segments', query });
  }

  async upsertAssets(credentialRef, body) {
    const items = Array.isArray(body) ? body : [body];
    const results = [];
    for (const item of items) {
      results.push(await this.client.request({ credentialRef, method: 'POST', path: '/ad-publishing/google/assets', body: item }));
    }
    return Array.isArray(body)
      ? { ok: results.every((entry) => entry?.ok !== false), status: results.at(-1)?.status ?? null, headers: results.at(-1)?.headers ?? {}, data: results.map((entry) => entry?.data ?? null) }
      : results[0];
  }

  upsertCampaign(credentialRef, body) {
    return this.client.request({ credentialRef, method: 'PUT', path: '/ad-publishing/google/ads', body });
  }

  async preflightPublish(credentialRef, adId, query = {}) {
    const result = await this.getCampaign(credentialRef, adId, query);
    const publishReadiness = GoogleAdsAdapter.publishReadiness(result?.data);
    if (!publishReadiness.ready) {
      throw new Error(`Google ad ${adId} is not ready to publish. Missing: ${publishReadiness.missing.join(', ')}`);
    }
    return {
      ...result,
      data: {
        campaign: result?.data || null,
        publishReadiness
      }
    };
  }

  publishAd(credentialRef, adId, body = {}) {
    return this.client.request({ credentialRef, method: 'POST', path: `/ad-publishing/google/ads/${adId}/publish`, body });
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
