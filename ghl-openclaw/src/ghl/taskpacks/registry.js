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

function normalizePlainObject(value) {
  return value && typeof value === 'object' && !Array.isArray(value) ? { ...value } : null;
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

function socialPostMutationRequestFrom(event) {
  const request = event?.payload?.mutationRequest || event?.payload?.actionRequest || null;
  if (!request) return null;

  const rawPost = request.post && typeof request.post === 'object' && !Array.isArray(request.post)
    ? { ...request.post }
    : {};
  const accountIds = normalizeStringArray(
    request.accountIds
    || rawPost.accountIds
    || [request.accountId, rawPost.accountId].filter(Boolean)
  );
  const status = normalizeString(request.status) || normalizeString(rawPost.status);
  const scheduleDate = normalizeString(request.scheduleDate)
    || normalizeString(request.scheduledAt)
    || normalizeString(rawPost.scheduleDate)
    || normalizeString(rawPost.scheduledAt);
  const summary = normalizeString(request.summary)
    || normalizeString(request.text)
    || normalizeString(rawPost.summary)
    || normalizeString(rawPost.text);

  if (accountIds.length > 0 && !Array.isArray(rawPost.accountIds)) rawPost.accountIds = accountIds;
  if (status && !rawPost.status) rawPost.status = status;
  if (scheduleDate && !rawPost.scheduleDate && !rawPost.scheduledAt) rawPost.scheduleDate = scheduleDate;
  if (summary && !rawPost.summary && !rawPost.text) rawPost.summary = summary;

  return {
    action: request.action || null,
    locationId: request.locationId || event?.locationId || event?.payload?.locationId || null,
    accountIds,
    status,
    scheduleDate,
    summary,
    post: rawPost
  };
}

function facebookAdMutationRequestFrom(event) {
  const request = event?.payload?.mutationRequest || event?.payload?.actionRequest || null;
  if (!request) return null;

  const campaign = normalizePlainObject(request.campaign || request.campaignBody);
  const adset = normalizePlainObject(request.adset || request.adsetBody);
  const ad = normalizePlainObject(request.ad || request.adBody);
  const query = normalizePlainObject(request.query);
  const sourcePost = normalizePlainObject(request.sourcePost || request.socialPost || request.post);
  const promotion = normalizePlainObject(request.promotion || request.bundle || request.adBundle);
  const publish = normalizeBoolean(request.publishAfterCreate ?? request.publish ?? request.pushLive);

  return {
    action: request.action || null,
    locationId: request.locationId || event?.locationId || event?.payload?.locationId || null,
    campaignId: normalizeString(request.campaignId) || normalizeString(campaign?.id),
    adsetId: normalizeString(request.adsetId) || normalizeString(adset?.id),
    adId: normalizeString(request.adId) || normalizeString(ad?.id),
    entityType: normalizeString(request.entityType) || normalizeString(query?.entityType),
    campaign,
    adset,
    ad,
    sourcePost,
    promotion,
    websiteUrl: normalizeString(request.websiteUrl) || normalizeString(request.link) || normalizeString(promotion?.websiteUrl) || normalizeString(promotion?.link),
    mediaUrls: normalizeStringArray(request.mediaUrls || request.imageUrls || promotion?.mediaUrls || promotion?.imageUrls),
    pageId: normalizeString(request.pageId) || normalizeString(promotion?.pageId),
    instagramAccountId: normalizeString(request.instagramAccountId) || normalizeString(promotion?.instagramAccountId),
    cta: normalizeString(request.cta) || normalizeString(request.callToAction) || normalizeString(promotion?.cta) || normalizeString(promotion?.callToAction),
    headline: normalizeString(request.headline) || normalizeString(promotion?.headline),
    description: normalizeString(request.description) || normalizeString(promotion?.description),
    query: query || {},
    publish: publish === true
  };
}

function googleAdMutationRequestFrom(event) {
  const request = event?.payload?.mutationRequest || event?.payload?.actionRequest || null;
  if (!request) return null;

  const campaign = normalizePlainObject(request.campaign || request.campaignBody || request.googleCampaign);
  const query = normalizePlainObject(request.query);
  const sourcePost = normalizePlainObject(request.sourcePost || request.socialPost || request.post);
  const promotion = normalizePlainObject(request.promotion || request.bundle || request.adBundle);
  const publish = normalizeBoolean(request.publishAfterCreate ?? request.publish ?? request.pushLive);

  return {
    action: request.action || null,
    locationId: request.locationId || event?.locationId || event?.payload?.locationId || null,
    adId: normalizeString(request.adId) || normalizeString(request.googleAdId) || normalizeString(request.campaignId) || normalizeString(campaign?.id),
    entityType: normalizeString(request.entityType) || normalizeString(query?.entityType),
    campaign,
    sourcePost,
    promotion,
    websiteUrl: normalizeString(request.websiteUrl) || normalizeString(request.link) || normalizeString(promotion?.websiteUrl) || normalizeString(promotion?.link),
    mediaUrls: normalizeStringArray(request.mediaUrls || request.imageUrls || promotion?.mediaUrls || promotion?.imageUrls),
    headline: normalizeString(request.headline) || normalizeString(promotion?.headline),
    description: normalizeString(request.description) || normalizeString(promotion?.description),
    cta: normalizeString(request.cta) || normalizeString(request.callToAction) || normalizeString(promotion?.cta) || normalizeString(promotion?.callToAction),
    query: query || {},
    publish: publish === true
  };
}

function socialAccountResumeData(liveResult) {
  const accounts = Array.isArray(liveResult?.data?.results?.accounts)
    ? liveResult.data.results.accounts
    : Array.isArray(liveResult?.data?.accounts)
      ? liveResult.data.accounts
      : Array.isArray(liveResult?.data)
        ? liveResult.data
        : [];
  return {
    accounts: accounts.slice(0, 20).map((account) => ({
      id: account?.id || null,
      name: account?.name || account?.accountName || null,
      platform: account?.platform || account?.type || null,
      status: account?.status || null
    })),
    accountCount: accounts.length,
    raw: liveResult?.data || null
  };
}

function socialPostCreateResumeData(liveResult, context) {
  const data = liveResult?.data || null;
  const mutationRequest = socialPostMutationRequestFrom(context?.event);
  return {
    post: data && typeof data === 'object'
      ? {
          id: data.id || data.postId || null,
          status: data.status || mutationRequest?.status || null,
          scheduleDate: data.scheduleDate || data.scheduledAt || mutationRequest?.scheduleDate || null,
          summary: data.summary || data.text || mutationRequest?.summary || null
        }
      : null,
    requested: {
      locationId: mutationRequest?.locationId || null,
      accountIds: mutationRequest?.accountIds || [],
      status: mutationRequest?.status || null,
      scheduleDate: mutationRequest?.scheduleDate || null,
      summary: mutationRequest?.summary || null
    },
    raw: data
  };
}

function socialAccountsFromContext(context) {
  const data = context?.runtime?.stepOutputs?.refresh_social_accounts?.data;
  if (Array.isArray(data?.results?.accounts)) return data.results.accounts;
  if (Array.isArray(data?.accounts)) return data.accounts;
  if (Array.isArray(data)) return data;
  return [];
}

function socialPostPreviewPayload(context, mutationRequest) {
  const createdPost = context?.runtime?.stepOutputs?.create_social_post?.data;
  const requestedPost = mutationRequest?.post && typeof mutationRequest.post === 'object' && !Array.isArray(mutationRequest.post)
    ? mutationRequest.post
    : {};

  return {
    ...requestedPost,
    ...(mutationRequest?.accountIds?.length > 0 ? { accountIds: mutationRequest.accountIds } : {}),
    ...(mutationRequest?.status ? { status: mutationRequest.status } : {}),
    ...(mutationRequest?.scheduleDate ? { scheduleDate: mutationRequest.scheduleDate } : {}),
    ...(mutationRequest?.summary ? { summary: mutationRequest.summary } : {}),
    ...(createdPost && typeof createdPost === 'object' && !Array.isArray(createdPost) ? createdPost : {})
  };
}

function resolvedSocialPostId(context) {
  const createdPost = context?.runtime?.stepOutputs?.create_social_post?.data;
  return createdPost?.id || createdPost?.postId || null;
}

function socialPreviewResultResumeData(liveResult) {
  const data = liveResult?.data || liveResult || null;
  return {
    preview: data && typeof data === 'object'
      ? {
          htmlPath: data.htmlPath || null,
          jsonPath: data.jsonPath || null,
          pngPath: data.pngPath || null,
          postId: data.postId || null,
          locationId: data.locationId || null
        }
      : null,
    raw: data
  };
}

function resolvedSocialPostLocationId(context, mutationRequest) {
  return mutationRequest?.locationId || context?.event?.locationId || context?.event?.payload?.locationId || null;
}

function socialPostCreateBody(mutationRequest) {
  const post = mutationRequest?.post;
  if (!post || typeof post !== 'object' || Array.isArray(post) || Object.keys(post).length === 0) {
    throw new Error('create_social_post requires a post object or top-level social post fields.');
  }

  const status = normalizeString(post.status) || mutationRequest?.status;
  const scheduleDate = normalizeString(post.scheduleDate) || normalizeString(post.scheduledAt) || mutationRequest?.scheduleDate;
  const accountIds = Array.isArray(post.accountIds) ? normalizeStringArray(post.accountIds) : mutationRequest?.accountIds || [];

  if (['scheduled', 'in_review'].includes((status || '').toLowerCase()) && !scheduleDate) {
    throw new Error('scheduled or in_review social posts require scheduleDate.');
  }
  if ((status || '').toLowerCase() !== 'draft' && accountIds.length === 0) {
    throw new Error('Non-draft social posts require at least one accountId.');
  }

  return {
    ...post,
    ...(accountIds.length > 0 ? { accountIds } : {}),
    ...(status ? { status } : {}),
    ...(scheduleDate && !post.scheduleDate && !post.scheduledAt ? { scheduleDate } : {})
  };
}

function collectFacebookEntityRecords(data) {
  const candidateArrays = [
    data?.results?.entities,
    data?.results?.items,
    data?.entities,
    data?.items,
    Array.isArray(data?.results) ? data.results : null,
    Array.isArray(data) ? data : null
  ];
  const records = candidateArrays.find((candidate) => Array.isArray(candidate)) || [];
  return records.filter((record) => record && typeof record === 'object');
}

function extractFacebookEntityId(value, keys = []) {
  const queue = [value];
  const seen = new Set();
  const preferredKeys = [...keys, 'id', 'campaignId', 'adsetId', 'adSetId', 'adId'];

  while (queue.length > 0) {
    const current = queue.shift();
    if (!current || typeof current !== 'object') continue;
    if (seen.has(current)) continue;
    seen.add(current);

    for (const key of preferredKeys) {
      const candidate = current[key];
      if (typeof candidate === 'string' && candidate.trim()) return candidate.trim();
    }

    if (Array.isArray(current)) {
      queue.push(...current.slice(0, 10));
      continue;
    }

    queue.push(...Object.values(current).filter((item) => item && typeof item === 'object').slice(0, 10));
  }

  return null;
}

function facebookEntityListResumeData(liveResult, context) {
  const mutationRequest = facebookAdMutationRequestFrom(context?.event);
  const entities = collectFacebookEntityRecords(liveResult?.data);
  return {
    entityType: mutationRequest?.entityType || null,
    entityCount: entities.length,
    entities: entities.slice(0, 20).map((entity) => ({
      id: extractFacebookEntityId(entity),
      name: entity?.name || entity?.campaignName || entity?.adsetName || entity?.adName || null,
      status: entity?.status || entity?.effectiveStatus || null,
      objective: entity?.objective || entity?.goal || null
    })),
    raw: liveResult?.data || null
  };
}

function facebookCampaignResumeData(liveResult) {
  const campaign = liveResult?.data?.campaign || liveResult?.data?.results?.campaign || liveResult?.data || null;
  return {
    campaign: campaign && typeof campaign === 'object'
      ? {
          id: extractFacebookEntityId(campaign, ['campaignId']),
          name: campaign.name || campaign.campaignName || null,
          status: campaign.status || campaign.effectiveStatus || null,
          objective: campaign.objective || campaign.goal || null
        }
      : null,
    raw: liveResult?.data || null
  };
}

function facebookUpsertResumeData(kind, liveResult, requestedBody) {
  const entity = liveResult?.data?.[kind] || liveResult?.data?.results?.[kind] || liveResult?.data || null;
  const idKey = kind === 'campaign' ? ['campaignId'] : kind === 'adset' ? ['adsetId', 'adSetId'] : ['adId'];
  return {
    kind,
    entity: entity && typeof entity === 'object'
      ? {
          id: extractFacebookEntityId(entity, idKey),
          name: entity.name || entity.campaignName || entity.adsetName || entity.adName || requestedBody?.name || null,
          status: entity.status || entity.effectiveStatus || requestedBody?.status || null
        }
      : {
          id: extractFacebookEntityId(liveResult?.data, idKey),
          name: requestedBody?.name || null,
          status: requestedBody?.status || null
        },
    requested: requestedBody || null,
    raw: liveResult?.data || null
  };
}

function facebookCampaignUpsertResumeData(liveResult, context) {
  return facebookUpsertResumeData('campaign', liveResult, facebookAdMutationRequestFrom(context?.event)?.campaign || null);
}

function facebookAdsetUpsertResumeData(liveResult, context) {
  return facebookUpsertResumeData('adset', liveResult, facebookAdMutationRequestFrom(context?.event)?.adset || null);
}

function facebookAdUpsertResumeData(liveResult, context) {
  return facebookUpsertResumeData('ad', liveResult, facebookAdMutationRequestFrom(context?.event)?.ad || null);
}

function resolvedFacebookCampaignId(context, mutationRequest) {
  return mutationRequest?.campaignId
    || normalizeString(mutationRequest?.campaign?.campaignId)
    || extractFacebookEntityId(context?.runtime?.stepOutputs?.upsert_facebook_campaign?.data, ['campaignId'])
    || null;
}

function resolvedFacebookAdsetId(context, mutationRequest) {
  return mutationRequest?.adsetId
    || normalizeString(mutationRequest?.adset?.adsetId)
    || normalizeString(mutationRequest?.adset?.adSetId)
    || extractFacebookEntityId(context?.runtime?.stepOutputs?.upsert_facebook_adset?.data, ['adsetId', 'adSetId'])
    || null;
}

function facebookCampaignDependencyReady(context, mutationRequest) {
  return Boolean(resolvedFacebookCampaignId(context, mutationRequest));
}

function facebookAdsetDependencyReady(context, mutationRequest) {
  return Boolean(resolvedFacebookAdsetId(context, mutationRequest));
}

function facebookCampaignBody(mutationRequest) {
  if (facebookPromotionRequested(mutationRequest)) {
    return facebookPromotionCampaignBody(mutationRequest);
  }
  const campaign = mutationRequest?.campaign;
  if (!campaign || Object.keys(campaign).length === 0) {
    throw new Error('Facebook campaign upsert requires a campaign object.');
  }
  return { ...campaign };
}

function facebookAdsetBody(context, mutationRequest) {
  if (facebookPromotionRequested(mutationRequest)) {
    return facebookPromotionAdsetBody(context, mutationRequest);
  }
  const adset = mutationRequest?.adset;
  if (!adset || Object.keys(adset).length === 0) {
    throw new Error('Facebook adset upsert requires an adset object.');
  }
  const campaignId = normalizeString(adset.campaignId) || resolvedFacebookCampaignId(context, mutationRequest);
  if (!campaignId) {
    throw new Error('Facebook adset upsert requires campaignId, either explicitly or from the prior campaign step.');
  }
  return {
    ...adset,
    ...(adset.campaignId ? {} : { campaignId })
  };
}

function facebookAdBody(context, mutationRequest) {
  if (facebookPromotionRequested(mutationRequest)) {
    return facebookPromotionAdBody(context, mutationRequest);
  }
  const ad = mutationRequest?.ad;
  if (!ad || Object.keys(ad).length === 0) {
    throw new Error('Facebook ad upsert requires an ad object.');
  }
  const campaignId = normalizeString(ad.campaignId) || resolvedFacebookCampaignId(context, mutationRequest);
  const adsetId = normalizeString(ad.adsetId) || resolvedFacebookAdsetId(context, mutationRequest);
  if (!adsetId) {
    throw new Error('Facebook ad upsert requires adsetId, either explicitly or from the prior adset step.');
  }
  return {
    ...ad,
    ...(campaignId && !ad.campaignId ? { campaignId } : {}),
    ...(ad.adsetId ? {} : { adsetId })
  };
}

function facebookEntityQuery(mutationRequest) {
  const query = { ...(mutationRequest?.query || {}) };
  const entityType = normalizeString(query.entityType) || mutationRequest?.entityType;
  if (!entityType) {
    throw new Error('Facebook entity lookup requires entityType.');
  }
  return {
    ...query,
    entityType
  };
}

function sourcePostSummary(sourcePost) {
  return normalizeString(sourcePost?.summary)
    || normalizeString(sourcePost?.text)
    || normalizeString(sourcePost?.caption)
    || null;
}

function sourcePostTheme(sourcePost) {
  return normalizeString(sourcePost?.theme) || normalizeString(sourcePost?.topic) || null;
}

function sourcePostMediaUrls(sourcePost) {
  const urls = [];
  const push = (value) => {
    const normalized = normalizeString(value);
    if (normalized) urls.push(normalized);
  };

  if (!sourcePost || typeof sourcePost !== 'object') return [];

  push(sourcePost.mediaUrl);
  push(sourcePost.imageUrl);
  push(sourcePost.link);

  if (Array.isArray(sourcePost.mediaUrls)) {
    sourcePost.mediaUrls.forEach(push);
  }
  if (Array.isArray(sourcePost.imageUrls)) {
    sourcePost.imageUrls.forEach(push);
  }
  if (Array.isArray(sourcePost.images)) {
    sourcePost.images.forEach((image) => {
      if (typeof image === 'string') push(image);
      else if (image && typeof image === 'object') {
        push(image.url);
        push(image.mediaUrl);
        push(image.fileUrl);
      }
    });
  }
  if (Array.isArray(sourcePost.media)) {
    sourcePost.media.forEach((item) => {
      if (typeof item === 'string') push(item);
      else if (item && typeof item === 'object') {
        push(item.url);
        push(item.mediaUrl);
        push(item.fileUrl);
        push(item.sourceUrl);
        push(item.secureUrl);
      }
    });
  }

  return [...new Set(urls)];
}

function facebookPromotionMediaUrls(mutationRequest) {
  return [...new Set([...(mutationRequest?.mediaUrls || []), ...sourcePostMediaUrls(mutationRequest?.sourcePost)])];
}

function firstParagraph(text) {
  const normalized = normalizeString(text);
  if (!normalized) return null;
  return normalized.split(/\n\s*\n/).map((part) => part.trim()).find(Boolean) || normalized;
}

function firstNonHashtagLine(text) {
  const normalized = normalizeString(text);
  if (!normalized) return null;
  const line = normalized
    .split('\n')
    .map((part) => part.trim())
    .find((part) => part && !part.startsWith('#'));
  return line || normalized;
}

function clampText(value, maxLength) {
  const normalized = normalizeString(value);
  if (!normalized || normalized.length <= maxLength) return normalized;
  return `${normalized.slice(0, Math.max(0, maxLength - 1)).trim()}…`;
}

function inferredPromotionHeadline(mutationRequest) {
  return mutationRequest?.headline
    || clampText(firstNonHashtagLine(sourcePostSummary(mutationRequest?.sourcePost)), 60)
    || clampText(sourcePostTheme(mutationRequest?.sourcePost), 60)
    || 'Learn more';
}

function inferredPromotionDescription(mutationRequest) {
  return mutationRequest?.description
    || clampText(firstParagraph(sourcePostSummary(mutationRequest?.sourcePost)), 140)
    || clampText(sourcePostTheme(mutationRequest?.sourcePost), 140)
    || null;
}

function inferredPromotionCampaignName(mutationRequest) {
  const explicit = normalizeString(mutationRequest?.promotion?.campaignName) || normalizeString(mutationRequest?.campaign?.name);
  if (explicit) return explicit;
  const theme = sourcePostTheme(mutationRequest?.sourcePost);
  if (theme) return `Promote ${theme}`;
  const headline = inferredPromotionHeadline(mutationRequest);
  return headline ? `Promote ${headline}` : 'Promote social post';
}

function inferredPromotionAdsetName(mutationRequest) {
  return normalizeString(mutationRequest?.promotion?.adsetName) || normalizeString(mutationRequest?.adset?.name) || `${inferredPromotionCampaignName(mutationRequest)} ad set`;
}

function inferredPromotionAdName(mutationRequest) {
  return normalizeString(mutationRequest?.promotion?.adName) || normalizeString(mutationRequest?.ad?.name) || `${inferredPromotionCampaignName(mutationRequest)} ad`;
}

function inferredPromotionStatus(mutationRequest) {
  return normalizeString(mutationRequest?.promotion?.status)
    || normalizeString(mutationRequest?.campaign?.status)
    || normalizeString(mutationRequest?.adset?.status)
    || normalizeString(mutationRequest?.ad?.status)
    || 'PAUSED';
}

function inferredPromotionObjective(mutationRequest) {
  return normalizeString(mutationRequest?.promotion?.objective)
    || normalizeString(mutationRequest?.campaign?.objective)
    || 'OUTCOME_TRAFFIC';
}

function facebookPromotionRequested(mutationRequest) {
  return ['promote_social_post_to_facebook_ad', 'promote_social_post', 'build_facebook_ad_from_social_post'].includes(mutationRequest?.action);
}

function facebookPromotionCampaignBody(mutationRequest) {
  const base = {
    ...(mutationRequest?.locationId ? { locationId: mutationRequest.locationId } : {}),
    name: inferredPromotionCampaignName(mutationRequest),
    objective: inferredPromotionObjective(mutationRequest),
    status: inferredPromotionStatus(mutationRequest)
  };
  return {
    ...base,
    ...(mutationRequest?.promotion?.campaign || {}),
    ...(mutationRequest?.campaign || {})
  };
}

function facebookPromotionAdsetBody(context, mutationRequest) {
  const campaignId = resolvedFacebookCampaignId(context, mutationRequest);
  const base = {
    ...(mutationRequest?.locationId ? { locationId: mutationRequest.locationId } : {}),
    name: inferredPromotionAdsetName(mutationRequest),
    status: inferredPromotionStatus(mutationRequest),
    ...(normalizeNumber(mutationRequest?.promotion?.dailyBudget) === null ? {} : { dailyBudget: normalizeNumber(mutationRequest?.promotion?.dailyBudget) }),
    ...(normalizeString(mutationRequest?.promotion?.optimizationGoal) ? { optimizationGoal: normalizeString(mutationRequest?.promotion?.optimizationGoal) } : {}),
    ...(normalizeString(mutationRequest?.promotion?.billingEvent) ? { billingEvent: normalizeString(mutationRequest?.promotion?.billingEvent) } : {}),
    ...(normalizePlainObject(mutationRequest?.promotion?.targeting) ? { targeting: normalizePlainObject(mutationRequest?.promotion?.targeting) } : {}),
    ...(normalizeString(mutationRequest?.promotion?.startTime) ? { startTime: normalizeString(mutationRequest?.promotion?.startTime) } : {}),
    ...(normalizeString(mutationRequest?.promotion?.endTime) ? { endTime: normalizeString(mutationRequest?.promotion?.endTime) } : {})
  };
  return {
    ...base,
    ...(mutationRequest?.promotion?.adset || {}),
    ...(mutationRequest?.adset || {}),
    ...(campaignId ? { campaignId } : {})
  };
}

function facebookPromotionAdBody(context, mutationRequest) {
  const mediaUrls = facebookPromotionMediaUrls(mutationRequest);
  const campaignId = resolvedFacebookCampaignId(context, mutationRequest);
  const adsetId = resolvedFacebookAdsetId(context, mutationRequest);
  const websiteUrl = mutationRequest?.websiteUrl || normalizeString(mutationRequest?.sourcePost?.websiteUrl) || normalizeString(mutationRequest?.sourcePost?.link) || null;
  const creative = {
    primaryText: sourcePostSummary(mutationRequest?.sourcePost),
    headline: inferredPromotionHeadline(mutationRequest),
    ...(inferredPromotionDescription(mutationRequest) ? { description: inferredPromotionDescription(mutationRequest) } : {}),
    ...(normalizeString(mutationRequest?.cta) ? { callToAction: normalizeString(mutationRequest?.cta) } : {}),
    ...(websiteUrl ? { link: websiteUrl } : {}),
    ...(mediaUrls[0] ? { mediaUrl: mediaUrls[0] } : {}),
    ...(mediaUrls.length > 0 ? { mediaUrls } : {})
  };

  return {
    ...(mutationRequest?.locationId ? { locationId: mutationRequest.locationId } : {}),
    name: inferredPromotionAdName(mutationRequest),
    status: inferredPromotionStatus(mutationRequest),
    ...(campaignId ? { campaignId } : {}),
    ...(adsetId ? { adsetId } : {}),
    ...(mutationRequest?.pageId ? { pageId: mutationRequest.pageId } : {}),
    ...(mutationRequest?.instagramAccountId ? { instagramAccountId: mutationRequest.instagramAccountId } : {}),
    ...(mutationRequest?.promotion?.ad || {}),
    ...(mutationRequest?.ad || {}),
    creative: {
      ...creative,
      ...(normalizePlainObject(mutationRequest?.promotion?.creative) || {}),
      ...(normalizePlainObject(mutationRequest?.ad?.creative) || {})
    }
  };
}

function collectGoogleEntityRecords(data) {
  const candidateArrays = [
    data?.results?.entities,
    data?.results?.items,
    data?.entities,
    data?.items,
    Array.isArray(data?.results) ? data.results : null,
    Array.isArray(data) ? data : null
  ];
  const records = candidateArrays.find((candidate) => Array.isArray(candidate)) || [];
  return records.filter((record) => record && typeof record === 'object');
}

function extractGoogleEntityId(value, keys = []) {
  const queue = [value];
  const seen = new Set();
  const preferredKeys = [...keys, 'id', 'adId', 'campaignId', 'resourceName'];

  while (queue.length > 0) {
    const current = queue.shift();
    if (!current || typeof current !== 'object') continue;
    if (seen.has(current)) continue;
    seen.add(current);

    for (const key of preferredKeys) {
      const candidate = current[key];
      if (typeof candidate === 'string' && candidate.trim()) return candidate.trim();
    }

    if (Array.isArray(current)) {
      queue.push(...current.slice(0, 10));
      continue;
    }

    queue.push(...Object.values(current).filter((item) => item && typeof item === 'object').slice(0, 10));
  }

  return null;
}

function googleEntityListResumeData(liveResult, context) {
  const mutationRequest = googleAdMutationRequestFrom(context?.event);
  const entities = collectGoogleEntityRecords(liveResult?.data);
  return {
    entityType: mutationRequest?.entityType || null,
    entityCount: entities.length,
    entities: entities.slice(0, 20).map((entity) => ({
      id: extractGoogleEntityId(entity),
      name: entity?.name || entity?.campaignName || entity?.adName || null,
      status: entity?.status || entity?.primaryStatus || null,
      type: entity?.entityType || entity?.type || mutationRequest?.entityType || null
    })),
    raw: liveResult?.data || null
  };
}

function googleIntegrationResumeData(liveResult) {
  const data = liveResult?.data || null;
  return {
    integration: data && typeof data === 'object'
      ? {
          locationId: data.locationId || null,
          status: data.status || null,
          connected: data.status === 'connected',
          adAccountId: data.adAccountId || null,
          createdAt: data.createdAt || null,
          updatedAt: data.updatedAt || null
        }
      : null
  };
}

function googleCampaignResumeData(liveResult) {
  const campaign = liveResult?.data?.campaign || liveResult?.data?.results?.campaign || liveResult?.data || null;
  return {
    campaign: campaign && typeof campaign === 'object'
      ? {
          id: extractGoogleEntityId(campaign),
          name: campaign.name || campaign.campaignName || null,
          status: campaign.status || campaign.primaryStatus || null,
          advertisingChannelType: campaign.advertisingChannelType || campaign.channelType || null
        }
      : null,
    raw: liveResult?.data || null
  };
}

function googlePublishPreflightResumeData(liveResult) {
  const campaign = liveResult?.data?.campaign || null;
  const readiness = liveResult?.data?.publishReadiness || null;
  return {
    campaign: campaign && typeof campaign === 'object'
      ? {
          id: extractGoogleEntityId(campaign),
          name: campaign.name || campaign.campaignName || null,
          status: campaign.status || campaign.primaryStatus || campaign.publishingStatus || null,
          advertisingChannelType: campaign.advertisingChannelType || campaign.channelType || null
        }
      : null,
    publishReadiness: readiness && typeof readiness === 'object'
      ? {
          ready: readiness.ready === true,
          missing: Array.isArray(readiness.missing) ? readiness.missing : [],
          counts: readiness.counts || null
        }
      : null,
    raw: liveResult?.data || null
  };
}

function googleCampaignUpsertResumeData(liveResult, context) {
  const mutationRequest = googleAdMutationRequestFrom(context?.event);
  const entity = liveResult?.data?.campaign || liveResult?.data?.results?.campaign || liveResult?.data || null;
  return {
    campaign: entity && typeof entity === 'object'
      ? {
          id: extractGoogleEntityId(entity),
          name: entity.name || entity.campaignName || mutationRequest?.campaign?.name || null,
          status: entity.status || entity.primaryStatus || mutationRequest?.campaign?.status || null,
          advertisingChannelType: entity.advertisingChannelType || entity.channelType || mutationRequest?.campaign?.advertisingChannelType || null
        }
      : {
          id: extractGoogleEntityId(liveResult?.data),
          name: mutationRequest?.campaign?.name || null,
          status: mutationRequest?.campaign?.status || null,
          advertisingChannelType: mutationRequest?.campaign?.advertisingChannelType || null
        },
    requested: mutationRequest?.campaign || null,
    raw: liveResult?.data || null
  };
}

function resolvedGoogleAdId(context, mutationRequest) {
  return mutationRequest?.adId
    || normalizeString(mutationRequest?.campaign?.adId)
    || normalizeString(mutationRequest?.campaign?.campaignId)
    || extractGoogleEntityId(context?.runtime?.stepOutputs?.upsert_google_campaign?.data)
    || null;
}

function googlePublishReadinessFrom(context) {
  return context?.runtime?.stepOutputs?.preflight_google_publish?.data?.publishReadiness || null;
}

function googlePreviewResultResumeData(liveResult) {
  const data = liveResult?.data || liveResult || null;
  return {
    preview: data && typeof data === 'object'
      ? {
          htmlPath: data.htmlPath || null,
          jsonPath: data.jsonPath || null,
          pngPath: data.pngPath || null,
          adId: data.adId || null,
          locationId: data.locationId || null
        }
      : null,
    raw: data
  };
}

function googleCampaignBody(context, mutationRequest) {
  if (googlePromotionRequested(mutationRequest)) {
    return googlePromotionCampaignBody(context, mutationRequest);
  }
  const campaign = mutationRequest?.campaign;
  if (!campaign || Object.keys(campaign).length === 0) {
    throw new Error('Google campaign upsert requires a campaign object.');
  }
  return {
    ...(mutationRequest?.locationId ? { locationId: mutationRequest.locationId } : {}),
    ...campaign
  };
}

function googleEntityQuery(mutationRequest) {
  const entityType = normalizeString(mutationRequest?.entityType)
    || normalizeString(mutationRequest?.query?.entityType)
    || normalizeString(mutationRequest?.query?.type);
  return {
    ...(mutationRequest?.locationId ? { locationId: mutationRequest.locationId } : {}),
    ...(entityType ? { entityType, type: entityType } : {}),
    ...(normalizePlainObject(mutationRequest?.query) || {})
  };
}

function googleCampaignQuery(mutationRequest, fallbackLocationId = null) {
  return {
    ...(mutationRequest?.locationId || fallbackLocationId ? { locationId: mutationRequest?.locationId || fallbackLocationId } : {}),
    ...(normalizePlainObject(mutationRequest?.query) || {})
  };
}

function googlePromotionRequested(mutationRequest) {
  return ['promote_social_post_to_google_ad', 'promote_social_post', 'build_google_ad_from_social_post'].includes(mutationRequest?.action);
}

function googlePromotionWebsiteUrl(mutationRequest) {
  return mutationRequest?.websiteUrl || normalizeString(mutationRequest?.sourcePost?.websiteUrl) || normalizeString(mutationRequest?.sourcePost?.link) || null;
}

function googlePromotionMediaUrls(mutationRequest) {
  return [...new Set([...(mutationRequest?.mediaUrls || []), ...sourcePostMediaUrls(mutationRequest?.sourcePost)])];
}

function inferredGoogleAdvertisingChannelType(mutationRequest) {
  return normalizeString(mutationRequest?.promotion?.advertisingChannelType)
    || normalizeString(mutationRequest?.campaign?.advertisingChannelType)
    || normalizeString(mutationRequest?.promotion?.channelType)
    || normalizeString(mutationRequest?.campaign?.channelType)
    || 'SEARCH';
}

function sanitizedPromotionTextTokens(mutationRequest) {
  const collected = [
    sourcePostTheme(mutationRequest?.sourcePost),
    sourcePostSummary(mutationRequest?.sourcePost),
    normalizeString(mutationRequest?.headline),
    normalizeString(mutationRequest?.description)
  ].filter(Boolean).join(' ');

  return [...new Set(
    collected
      .toLowerCase()
      .replace(/https?:\/\/\S+/g, ' ')
      .replace(/[#@][\p{L}\p{N}_-]+/gu, ' ')
      .replace(/[^\p{L}\p{N}\s-]/gu, ' ')
      .split(/\s+/)
      .map((token) => token.trim())
      .filter((token) => token.length >= 3)
      .filter((token) => !['and', 'the', 'for', 'with', 'your', 'from', 'into', 'that', 'this', 'have', 'help', 'more'].includes(token))
  )];
}

function normalizeKeywordMatchType(value, fallback = 'PHRASE') {
  const normalized = normalizeString(value)?.toUpperCase();
  return ['BROAD', 'PHRASE', 'EXACT'].includes(normalized) ? normalized : fallback;
}

function normalizeKeywordEntry(entry, fallbackMatchType = 'PHRASE') {
  if (typeof entry === 'string') {
    const keyword = normalizeString(entry);
    return keyword ? { keyword, matchType: fallbackMatchType } : null;
  }
  if (!entry || typeof entry !== 'object') return null;
  const keyword = normalizeString(entry.keyword) || normalizeString(entry.text) || normalizeString(entry.value);
  if (!keyword) return null;
  return {
    keyword,
    matchType: normalizeKeywordMatchType(entry.matchType, fallbackMatchType)
  };
}

function normalizeKeywordEntries(entries, fallbackMatchType = 'PHRASE') {
  return Array.isArray(entries)
    ? entries.map((entry) => normalizeKeywordEntry(entry, fallbackMatchType)).filter(Boolean)
    : [];
}

function googlePromotionCopySource(mutationRequest) {
  return normalizePlainObject(mutationRequest?.promotion?.copy)
    || normalizePlainObject(mutationRequest?.campaign?.copy)
    || {};
}

function googlePromotionKeywords(mutationRequest) {
  const explicit = normalizeKeywordEntries(
    mutationRequest?.promotion?.keywords?.positives
    || mutationRequest?.promotion?.positiveKeywords
    || mutationRequest?.campaign?.keywords?.positives,
    'PHRASE'
  );
  if (explicit && explicit.length > 0) {
    return explicit;
  }

  const theme = sourcePostTheme(mutationRequest?.sourcePost);
  const tokens = sanitizedPromotionTextTokens(mutationRequest);
  const copy = googlePromotionCopySource(mutationRequest);
  const candidates = [
    theme,
    normalizeString(copy.primaryKeyword),
    normalizeString(copy.secondaryKeyword),
    normalizeString(copy.keywordSeed),
    tokens.slice(0, 2).join(' '),
    tokens.slice(2, 4).join(' '),
    tokens.slice(0, 1).join(' '),
    tokens.slice(1, 2).join(' ')
  ].map((value) => normalizeString(value)).filter(Boolean);

  return [...new Set(candidates)].slice(0, 5).map((keyword) => ({
    keyword,
    matchType: 'PHRASE'
  }));
}

function googlePromotionNegativeKeywords(mutationRequest) {
  return normalizeKeywordEntries(
    mutationRequest?.promotion?.keywords?.negatives
    || mutationRequest?.promotion?.negativeKeywords
    || mutationRequest?.campaign?.keywords?.negatives,
    'PHRASE'
  );
}

function googlePromotionHeadlines(mutationRequest) {
  const copy = googlePromotionCopySource(mutationRequest);
  const explicit = normalizeStringArray(
    mutationRequest?.promotion?.headlines
    || mutationRequest?.campaign?.headlines
    || copy.headlines
  );
  if (explicit.length > 0) return explicit;

  const theme = sourcePostTheme(mutationRequest?.sourcePost);
  const headline = inferredPromotionHeadline(mutationRequest);
  const candidates = [
    headline,
    normalizeString(copy.primaryHeadline),
    normalizeString(copy.secondaryHeadline),
    theme ? `Get clarity on ${theme}` : null,
    'Turn clicks into clients',
    'Clearer marketing decisions',
    'Better tracking, better growth'
  ].map((value) => clampText(value, 30)).filter(Boolean);

  return [...new Set(candidates)].slice(0, 5);
}

function googlePromotionLongHeadlines(mutationRequest) {
  const copy = googlePromotionCopySource(mutationRequest);
  const explicit = normalizeStringArray(
    mutationRequest?.promotion?.longHeadlines
    || mutationRequest?.campaign?.longHeadlines
    || copy.longHeadlines
  );
  if (explicit.length > 0) return explicit;

  const candidates = [
    normalizeString(copy.longHeadline),
    sourcePostTheme(mutationRequest?.sourcePost) ? `Get clearer growth decisions for ${sourcePostTheme(mutationRequest?.sourcePost)}` : null,
    firstParagraph(sourcePostSummary(mutationRequest?.sourcePost)),
    inferredPromotionDescription(mutationRequest)
  ].map((value) => clampText(value, 90)).filter(Boolean);

  return [...new Set(candidates)].slice(0, 3);
}

function googlePromotionDescriptions(mutationRequest) {
  const copy = googlePromotionCopySource(mutationRequest);
  const explicit = normalizeStringArray(
    mutationRequest?.promotion?.descriptions
    || mutationRequest?.campaign?.descriptions
    || copy.descriptions
  );
  if (explicit.length > 0) return explicit;

  const candidates = [
    normalizeString(copy.primaryDescription),
    normalizeString(copy.secondaryDescription),
    inferredPromotionDescription(mutationRequest),
    'Improve tracking, offers, and conversion paths.',
    'Build a cleaner path from click to client.'
  ].map((value) => clampText(value, 90)).filter(Boolean);

  return [...new Set(candidates)].slice(0, 4);
}

function googlePromotionPathParts(websiteUrl) {
  const normalized = normalizeString(websiteUrl);
  if (!normalized) return [];
  try {
    const parsed = new URL(normalized);
    return parsed.pathname
      .split('/')
      .map((part) => part.trim())
      .filter(Boolean)
      .map((part) => part.replace(/[^a-zA-Z0-9-]/g, '').slice(0, 15))
      .filter(Boolean)
      .slice(0, 2);
  } catch {
    return [];
  }
}

function googlePromotionResolvedPathParts(mutationRequest, websiteUrl) {
  const copy = googlePromotionCopySource(mutationRequest);
  const path1 = clampText(normalizeString(mutationRequest?.promotion?.path1) || normalizeString(copy.path1), 15);
  const path2 = clampText(normalizeString(mutationRequest?.promotion?.path2) || normalizeString(copy.path2), 15);
  if (path1 || path2) return [path1 || null, path2 || null];
  return googlePromotionPathParts(websiteUrl);
}

function normalizeGoogleTargetingEntries(values) {
  if (!Array.isArray(values)) return [];
  return values.map((entry) => {
    if (typeof entry === 'string') return normalizeString(entry);
    if (!entry || typeof entry !== 'object') return null;
    return { ...entry };
  }).filter(Boolean);
}

function normalizeGoogleLocaleEntries(values) {
  if (!Array.isArray(values)) return [];
  return values.map((entry) => {
    if (typeof entry === 'string') {
      const code = normalizeString(entry);
      return code ? { code } : null;
    }
    if (!entry || typeof entry !== 'object') return null;
    return { ...entry };
  }).filter(Boolean);
}

function googlePromotionTargeting(mutationRequest) {
  const targeting = normalizePlainObject(mutationRequest?.promotion?.targeting)
    || normalizePlainObject(mutationRequest?.campaign?.targeting)
    || {};
  const locales = normalizeGoogleLocaleEntries(targeting.locales || targeting.languages);
  const geoLocations = normalizeGoogleTargetingEntries(targeting.geoLocations || targeting.locations);
  const segments = normalizeGoogleTargetingEntries(targeting.segments || targeting.audienceSegments);
  const targetInterests = normalizeGoogleTargetingEntries(targeting.targetInterests || targeting.interests);
  const audiences = normalizeGoogleTargetingEntries(targeting.audiences || targeting.userLists);
  const selectedChannels = normalizeStringArray(targeting.selectedChannels || targeting.channels).map((value) => value.toUpperCase());
  const customChannels = normalizeBoolean(targeting.customChannels);

  return {
    audience: {
      ...(locales.length > 0 ? { locales } : {}),
      ...(geoLocations.length > 0 ? { geoLocations } : {}),
      ...(segments.length > 0 ? { segments } : {}),
      ...(targetInterests.length > 0 ? { targetInterests } : {}),
      ...(audiences.length > 0 ? { audiences } : {})
    },
    selectedChannels,
    customChannels
  };
}

function googlePromotionSitelinkSpecs(mutationRequest) {
  const explicit = Array.isArray(mutationRequest?.promotion?.sitelinks) ? mutationRequest.promotion.sitelinks : null;
  if (explicit && explicit.length > 0) {
    return explicit.map((entry, index) => {
      if (typeof entry === 'string') {
        return {
          linkText: clampText(entry, 25),
          finalUrls: googlePromotionWebsiteUrl(mutationRequest)
        };
      }
      return {
        resourceName: normalizeString(entry?.resourceName),
        linkText: clampText(normalizeString(entry?.linkText) || normalizeString(entry?.text) || `Link ${index + 1}`, 25),
        finalUrls: normalizeString(entry?.finalUrls) || normalizeString(entry?.finalUrl) || googlePromotionWebsiteUrl(mutationRequest),
        description1: clampText(normalizeString(entry?.description1), 35),
        description2: clampText(normalizeString(entry?.description2), 35)
      };
    }).filter((entry) => entry.resourceName || (entry.linkText && entry.finalUrls));
  }

  if (mutationRequest?.promotion?.autoSitelinks === false) return [];

  const websiteUrl = googlePromotionWebsiteUrl(mutationRequest);
  if (!websiteUrl) return [];
  try {
    const parsed = new URL(websiteUrl);
    const origin = parsed.origin;
    return [
      {
        linkText: 'Learn More',
        finalUrls: websiteUrl,
        description1: 'See how it works',
        description2: 'Get the overview'
      },
      {
        linkText: 'Contact Us',
        finalUrls: `${origin}/contact`,
        description1: 'Talk with our team',
        description2: 'Get in touch fast'
      },
      {
        linkText: 'Our Services',
        finalUrls: `${origin}/services`,
        description1: 'See service options',
        description2: 'Find the right fit'
      }
    ];
  } catch {
    return [];
  }
}

function googlePromotionCallPayload(mutationRequest) {
  const call = normalizePlainObject(mutationRequest?.promotion?.call || mutationRequest?.promotion?.callAsset);
  if (!call) return null;
  const phoneNumber = normalizeString(call.phoneNumber);
  if (!phoneNumber) return null;
  return {
    phoneNumber,
    ...(normalizeString(call.countryCode) ? { countryCode: normalizeString(call.countryCode) } : {})
  };
}

function googleAssetsListResumeData(type, liveResult) {
  const assets = Array.isArray(liveResult?.data) ? liveResult.data : [];
  return {
    type,
    assets: assets.map((asset) => ({
      resourceName: asset.resourceName || null,
      type: asset.type || type || null,
      linkText: asset.linkText || null,
      phoneNumber: asset.phoneNumber || null,
      finalUrls: asset.finalUrls || asset.finalUrl || null
    }))
  };
}

function googleAssetsUpsertResumeData(liveResult, requestedAssets = []) {
  const items = Array.isArray(liveResult?.data) ? liveResult.data : [liveResult?.data].filter(Boolean);
  return {
    assets: items.filter((item) => item && typeof item === 'object').map((item, index) => ({
      resourceName: item.resourceName || item.results?.resourceName || null,
      type: item.type || item.results?.type || requestedAssets[index]?.type || null,
      linkText: item.linkText || null,
      phoneNumber: item.phoneNumber || null,
      finalUrls: item.finalUrls || item.finalUrl || null,
      raw: item
    }))
  };
}

function resolvedGoogleCallAssetResourceNames(context) {
  const existing = context?.runtime?.stepOutputs?.get_google_call_assets?.data?.assets;
  const created = context?.runtime?.stepOutputs?.upsert_google_extension_assets?.data?.assets;
  return [...new Set([
    ...(Array.isArray(existing) ? existing : []),
    ...(Array.isArray(created) ? created : [])
  ].filter((asset) => asset?.type === 'CALL' && normalizeString(asset?.resourceName)).map((asset) => asset.resourceName))];
}

function resolvedGoogleSitelinkResourceNames(context, mutationRequest) {
  const created = context?.runtime?.stepOutputs?.upsert_google_extension_assets?.data?.assets;
  const explicit = googlePromotionSitelinkSpecs(mutationRequest)
    .map((entry) => normalizeString(entry?.resourceName))
    .filter(Boolean);
  return [...new Set([
    ...explicit,
    ...(Array.isArray(created) ? created : []).filter((asset) => asset?.type === 'SITELINK' && normalizeString(asset?.resourceName)).map((asset) => asset.resourceName)
  ])];
}

function googleExtensionAssetBodies(context, mutationRequest) {
  const locationId = mutationRequest?.locationId || context?.event?.locationId || null;
  const sitelinks = googlePromotionSitelinkSpecs(mutationRequest)
    .filter((entry) => !normalizeString(entry?.resourceName))
    .map((entry) => ({
      locationId,
      type: 'SITELINK',
      payload: {
        linkText: entry.linkText,
        finalUrls: entry.finalUrls,
        ...(entry.description1 ? { description1: entry.description1 } : {}),
        ...(entry.description2 ? { description2: entry.description2 } : {})
      }
    }));

  const callPayload = googlePromotionCallPayload(mutationRequest);
  const shouldCreateCall = Boolean(callPayload) && resolvedGoogleCallAssetResourceNames(context).length === 0;
  const calls = shouldCreateCall ? [{
    locationId,
    type: 'CALL',
    payload: callPayload
  }] : [];

  return [...sitelinks, ...calls];
}

function googlePromotionMediaItems(mutationRequest) {
  return googlePromotionMediaUrls(mutationRequest).slice(0, 5).map((src) => ({ type: 'IMAGE', src }));
}

function googlePromotionAssetImages(mutationRequest) {
  return googlePromotionMediaUrls(mutationRequest).slice(0, 5).map((url) => ({ type: 'IMAGE', url }));
}

function googlePromotionBusinessName(mutationRequest) {
  return normalizeString(mutationRequest?.promotion?.businessName)
    || normalizeString(mutationRequest?.campaign?.businessName)
    || normalizeString(mutationRequest?.sourcePost?.businessName)
    || null;
}

function googlePromotionAdContentBody(mutationRequest, websiteUrl) {
  const pathParts = googlePromotionResolvedPathParts(mutationRequest, websiteUrl);
  const media = googlePromotionMediaItems(mutationRequest);
  return {
    name: inferredPromotionAdName(mutationRequest),
    status: inferredPromotionStatus(mutationRequest),
    headlines: googlePromotionHeadlines(mutationRequest),
    longHeadlines: googlePromotionLongHeadlines(mutationRequest),
    descriptions: googlePromotionDescriptions(mutationRequest),
    ...(websiteUrl ? { finalUrl: websiteUrl } : {}),
    ...(pathParts[0] ? { path1: pathParts[0] } : {}),
    ...(pathParts[1] ? { path2: pathParts[1] } : {}),
    ...(media.length > 0 ? { media } : {}),
    ...(googlePromotionBusinessName(mutationRequest) ? { businessName: googlePromotionBusinessName(mutationRequest) } : {})
  };
}

function googleFetchedCampaignFrom(context) {
  return context?.runtime?.stepOutputs?.get_google_campaign?.data?.campaign
    || context?.runtime?.stepOutputs?.get_google_campaign?.data
    || null;
}

function normalizeGooglePromotionAdContentEntry(entry, mutationRequest, options = {}) {
  const item = normalizePlainObject(entry) || {};
  const websiteUrl = options.websiteUrl || googlePromotionWebsiteUrl(mutationRequest);
  const media = Array.isArray(item.media) && item.media.length > 0 ? item.media : googlePromotionMediaItems(mutationRequest);
  const pathParts = [
    clampText(normalizeString(item.path1), 15),
    clampText(normalizeString(item.path2), 15)
  ];
  const fallbackPathParts = googlePromotionResolvedPathParts(mutationRequest, websiteUrl);
  const googleAdGroupId = normalizeString(options.googleAdGroupId) || normalizeString(item.googleAdGroupId) || normalizeString(item.adGroupResourceName);
  const draftAdGroupId = normalizeString(options.draftAdGroupId) || normalizeString(item.adGroupId);
  const googleAdId = normalizeString(item.googleAdId);
  const draftAdId = normalizeString(item.id);

  return {
    ...(googleAdId || draftAdId ? { id: googleAdId || draftAdId } : {}),
    ...(googleAdId ? { googleAdId } : {}),
    ...(draftAdGroupId ? { adGroupId: draftAdGroupId } : (googleAdGroupId ? { adGroupId: googleAdGroupId } : {})),
    ...(googleAdGroupId ? { googleAdGroupId, adGroup: googleAdGroupId } : {}),
    name: normalizeString(item.name) || inferredPromotionAdName(mutationRequest),
    status: normalizeString(item.status) || inferredPromotionStatus(mutationRequest),
    headlines: normalizeStringArray(item.headlines).length > 0 ? normalizeStringArray(item.headlines) : googlePromotionHeadlines(mutationRequest),
    longHeadlines: normalizeStringArray(item.longHeadlines).length > 0 ? normalizeStringArray(item.longHeadlines) : googlePromotionLongHeadlines(mutationRequest),
    descriptions: normalizeStringArray(item.descriptions).length > 0 ? normalizeStringArray(item.descriptions) : googlePromotionDescriptions(mutationRequest),
    ...(websiteUrl ? { finalUrl: normalizeString(item.finalUrl) || websiteUrl } : {}),
    ...((pathParts[0] || fallbackPathParts[0]) ? { path1: pathParts[0] || fallbackPathParts[0] } : {}),
    ...((pathParts[1] || fallbackPathParts[1]) ? { path2: pathParts[1] || fallbackPathParts[1] } : {}),
    ...(media.length > 0 ? { media } : {}),
    ...(normalizeString(item.businessName) || googlePromotionBusinessName(mutationRequest)
      ? { businessName: normalizeString(item.businessName) || googlePromotionBusinessName(mutationRequest) }
      : {})
  };
}

function normalizeGooglePromotionAdGroupEntry(entry, mutationRequest, websiteUrl) {
  const item = normalizePlainObject(entry) || {};
  const pathParts = [
    clampText(normalizeString(item.path1), 15),
    clampText(normalizeString(item.path2), 15)
  ];
  const fallbackPathParts = googlePromotionResolvedPathParts(mutationRequest, websiteUrl);
  const media = Array.isArray(item.media) && item.media.length > 0 ? item.media : googlePromotionMediaItems(mutationRequest);
  const fallbackTargeting = googlePromotionTargeting(mutationRequest);
  const audience = normalizePlainObject(item.audience) || fallbackTargeting.audience;
  const selectedChannels = normalizeStringArray(item.selectedChannels).length > 0
    ? normalizeStringArray(item.selectedChannels).map((value) => value.toUpperCase())
    : fallbackTargeting.selectedChannels;
  const customChannels = normalizeBoolean(item.customChannels);
  const googleAdGroupId = normalizeString(item.googleAdGroupId) || normalizeString(item.resourceName);
  const draftAdGroupId = normalizeString(item.id);
  const explicitAdContent = Array.isArray(item.adContent) && item.adContent.length > 0
    ? item.adContent
    : Array.isArray(mutationRequest?.promotion?.adContent) && mutationRequest.promotion.adContent.length > 0
      ? mutationRequest.promotion.adContent
      : [googlePromotionAdContentBody(mutationRequest, websiteUrl)];

  return {
    ...(draftAdGroupId ? { id: draftAdGroupId } : {}),
    ...(googleAdGroupId ? { googleAdGroupId } : {}),
    name: normalizeString(item.name) || normalizeString(mutationRequest?.promotion?.adGroupName) || `${inferredPromotionCampaignName(mutationRequest)} ad group`,
    status: normalizeString(item.status) || inferredPromotionStatus(mutationRequest),
    keywords: {
      positives: normalizeKeywordEntries(item?.keywords?.positives, 'PHRASE').length > 0
        ? normalizeKeywordEntries(item?.keywords?.positives, 'PHRASE')
        : googlePromotionKeywords(mutationRequest),
      negatives: normalizeKeywordEntries(item?.keywords?.negatives, 'PHRASE').length > 0
        ? normalizeKeywordEntries(item?.keywords?.negatives, 'PHRASE')
        : googlePromotionNegativeKeywords(mutationRequest)
    },
    headlines: normalizeStringArray(item.headlines).length > 0 ? normalizeStringArray(item.headlines) : googlePromotionHeadlines(mutationRequest),
    longHeadlines: normalizeStringArray(item.longHeadlines).length > 0 ? normalizeStringArray(item.longHeadlines) : googlePromotionLongHeadlines(mutationRequest),
    descriptions: normalizeStringArray(item.descriptions).length > 0 ? normalizeStringArray(item.descriptions) : googlePromotionDescriptions(mutationRequest),
    ...(websiteUrl ? { finalUrl: normalizeString(item.finalUrl) || websiteUrl } : {}),
    ...((pathParts[0] || fallbackPathParts[0]) ? { path1: pathParts[0] || fallbackPathParts[0] } : {}),
    ...((pathParts[1] || fallbackPathParts[1]) ? { path2: pathParts[1] || fallbackPathParts[1] } : {}),
    ...(media.length > 0 ? { media } : {}),
    ...(customChannels === null ? (fallbackTargeting.customChannels === null ? {} : { customChannels: fallbackTargeting.customChannels }) : { customChannels }),
    ...(selectedChannels.length > 0 ? { selectedChannels } : {}),
    ...(Object.keys(audience).length > 0 ? { audience } : {}),
    adContent: explicitAdContent.map((adContentEntry) => normalizeGooglePromotionAdContentEntry(adContentEntry, mutationRequest, {
      websiteUrl,
      googleAdGroupId,
      draftAdGroupId
    }))
  };
}

function googlePromotionAdGroupBody(mutationRequest, websiteUrl) {
  return normalizeGooglePromotionAdGroupEntry({}, mutationRequest, websiteUrl);
}

function googlePromotionCampaignBody(context, mutationRequest) {
  const mediaUrls = googlePromotionMediaUrls(mutationRequest);
  const websiteUrl = googlePromotionWebsiteUrl(mutationRequest);
  const headline = inferredPromotionHeadline(mutationRequest);
  const description = inferredPromotionDescription(mutationRequest);
  const dailyBudget = normalizeNumber(mutationRequest?.promotion?.dailyBudget);
  const existingCampaignId = normalizeString(mutationRequest?.adId)
    || normalizeString(mutationRequest?.campaign?.id)
    || normalizeString(mutationRequest?.campaign?.adId)
    || normalizeString(mutationRequest?.campaign?.campaignId)
    || null;
  const fetchedCampaign = googleFetchedCampaignFrom(context);
  const explicitAdGroups = Array.isArray(mutationRequest?.promotion?.adGroups) && mutationRequest.promotion.adGroups.length > 0
    ? mutationRequest.promotion.adGroups
    : Array.isArray(mutationRequest?.campaign?.adGroups) && mutationRequest.campaign.adGroups.length > 0
      ? mutationRequest.campaign.adGroups
      : null;
  const adGroups = explicitAdGroups && explicitAdGroups.length > 0
    ? explicitAdGroups.map((entry) => normalizeGooglePromotionAdGroupEntry(entry, mutationRequest, websiteUrl))
    : existingCampaignId && Array.isArray(fetchedCampaign?.adGroups) && fetchedCampaign.adGroups.length > 0
      ? fetchedCampaign.adGroups.map((entry) => normalizeGooglePromotionAdGroupEntry(entry, mutationRequest, websiteUrl))
      : [googlePromotionAdGroupBody(mutationRequest, websiteUrl)];
  const callAssetResourceNames = resolvedGoogleCallAssetResourceNames(context);
  const sitelinkResourceNames = resolvedGoogleSitelinkResourceNames(context, mutationRequest);

  return {
    ...(mutationRequest?.locationId ? { locationId: mutationRequest.locationId } : {}),
    ...(existingCampaignId ? { id: existingCampaignId, adId: existingCampaignId } : {}),
    name: inferredPromotionCampaignName(mutationRequest),
    status: inferredPromotionStatus(mutationRequest),
    advertisingChannelType: inferredGoogleAdvertisingChannelType(mutationRequest),
    ...(websiteUrl ? { finalUrl: websiteUrl } : {}),
    ...(headline ? { headline } : {}),
    ...(description ? { description } : {}),
    ...(normalizeString(mutationRequest?.cta) ? { callToAction: normalizeString(mutationRequest.cta) } : {}),
    ...(dailyBudget === null ? {} : {
      dailyBudget,
      budget: {
        budgetType: 'DAILY',
        amount: dailyBudget
      }
    }),
    ...(mediaUrls.length > 0 ? { mediaUrls } : {}),
    ...((googlePromotionAssetImages(mutationRequest).length > 0 || callAssetResourceNames.length > 0 || sitelinkResourceNames.length > 0)
      ? {
          assets: {
            ...(callAssetResourceNames.length > 0 ? { calls: callAssetResourceNames } : {}),
            ...(sitelinkResourceNames.length > 0 ? { sitelinks: sitelinkResourceNames } : {}),
            ...(googlePromotionAssetImages(mutationRequest).length > 0 ? { images: googlePromotionAssetImages(mutationRequest) } : {})
          }
        }
      : {}),
    adGroups,
    ...(mutationRequest?.promotion?.campaign || {}),
    ...(mutationRequest?.campaign || {})
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

function appointmentComparableDateTime(value) {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return new Date(value).toISOString().slice(0, 19).replace('T', ' ');
  }
  if (typeof value !== 'string' || !value.trim()) return null;
  const trimmed = value.trim();
  const directMatch = trimmed.match(/^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})/);
  if (directMatch) {
    return `${directMatch[1]} ${directMatch[2]}`;
  }
  const parsed = new Date(trimmed);
  if (Number.isNaN(parsed.getTime())) return null;
  return parsed.toISOString().slice(0, 19).replace('T', ' ');
}

function filterAppointmentRecords(records, mutationRequest) {
  const requestedStart = appointmentComparableDateTime(mutationRequest?.startTime)
    || appointmentComparableDateTime(mutationRequest?.startDate);
  const requestedEnd = appointmentComparableDateTime(
    mutationRequest?.endTime
    || computedEndTime(mutationRequest?.startTime, mutationRequest?.durationMinutes)
    || mutationRequest?.endDate
  );

  return records.filter((appointment) => {
    if (mutationRequest?.contactId && appointment.contactId !== mutationRequest.contactId) return false;
    if (mutationRequest?.calendarId && appointment.calendarId !== mutationRequest.calendarId) return false;
    if (mutationRequest?.assignedUserId && appointment.assignedUserId !== mutationRequest.assignedUserId) return false;
    if (mutationRequest?.appointmentStatus && appointment.appointmentStatus !== mutationRequest.appointmentStatus) return false;

    const appointmentStart = appointmentComparableDateTime(appointment.startTime);
    const appointmentEnd = appointmentComparableDateTime(appointment.endTime) || appointmentStart;

    if (requestedStart && appointmentEnd && appointmentEnd < requestedStart) return false;
    if (requestedEnd && appointmentStart && appointmentStart > requestedEnd) return false;
    return true;
  });
}

function appointmentListResumeData(liveResult, context) {
  const mutationRequest = appointmentMutationRequestFrom(context?.event);
  const appointments = collectAppointmentRecords(liveResult?.data).slice(0, 100);
  const filteredAppointments = filterAppointmentRecords(appointments, mutationRequest).slice(0, 100);
  return {
    appointments: filteredAppointments,
    raw: liveResult?.data || null,
    filtersApplied: {
      contactId: mutationRequest?.contactId || null,
      contactName: mutationRequest?.contactName || null,
      calendarId: mutationRequest?.calendarId || null,
      assignedUserId: mutationRequest?.assignedUserId || null,
      appointmentStatus: mutationRequest?.appointmentStatus || null,
      startTime: mutationRequest?.startTime || null,
      endTime: mutationRequest?.endTime || null,
      startDate: mutationRequest?.startDate ?? null,
      endDate: mutationRequest?.endDate ?? null
    },
    unfilteredCount: appointments.length,
    filteredCount: filteredAppointments.length
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
      trigger_events: ['SocialPostCreate', 'ManualRun'],
      buildExecutionPlan(context) {
        const mutationRequest = socialPostMutationRequestFrom(context.event);
        const locationId = resolvedSocialPostLocationId(context, mutationRequest);
        const explicitCreateSocialPost = mutationRequest?.action === 'create_social_post';
        return [
          {
            name: 'refresh_social_accounts',
            kind: 'adapter_call',
            adapter: 'SocialPlannerAdapter',
            method: 'listAccounts',
            pathHint: '/social-media-posting/:locationId/accounts',
            args: () => [context.credentialRef || defaultLocationCredential(context), locationId, {}],
            safe: true,
            mutation: false,
            requiresCredential: true,
            resumeData: socialAccountResumeData,
            details: {
              action: 'list_social_accounts',
              locationId
            },
            skipIf: () => !locationId
          },
          {
            name: 'create_social_post',
            kind: 'adapter_call',
            adapter: 'SocialPlannerAdapter',
            method: 'createPost',
            httpMethod: 'POST',
            pathHint: '/social-media-posting/:locationId/posts',
            args: () => [context.credentialRef || defaultLocationCredential(context), locationId, socialPostCreateBody(mutationRequest)],
            safe: false,
            mutation: true,
            requiresApproval: true,
            requiresCredential: true,
            resumeData: socialPostCreateResumeData,
            details: {
              action: 'create_social_post',
              locationId,
              accountIds: mutationRequest?.accountIds || [],
              status: mutationRequest?.status || null,
              scheduleDate: mutationRequest?.scheduleDate || null,
              summaryPreview: mutationRequest?.summary ? mutationRequest.summary.slice(0, 160) : null
            },
            skipIf: () => !explicitCreateSocialPost
          },
          {
            name: 'generate_social_post_preview',
            kind: 'adapter_call',
            adapter: 'PreviewArtifactsAdapter',
            method: 'generateSocialPostPreview',
            pathHint: 'local://generated/social-post-preview',
            args: (runtimeContext) => [
              null,
              {
                locationId,
                postId: resolvedSocialPostId(runtimeContext),
                post: socialPostPreviewPayload(runtimeContext, mutationRequest),
                socialAccounts: socialAccountsFromContext(runtimeContext)
              }
            ],
            safe: true,
            mutation: false,
            requiresCredential: false,
            resumeData: socialPreviewResultResumeData,
            details: (runtimeContext) => ({
              action: 'generate_social_post_preview',
              locationId,
              postId: resolvedSocialPostId(runtimeContext),
              accountCount: socialAccountsFromContext(runtimeContext).length,
              targetAccountIds: mutationRequest?.accountIds || []
            }),
            skipIf: () => !explicitCreateSocialPost
          }
        ];
      }
    },
    facebook_ad_pack: {
      trigger_events: ['ManualRun'],
      buildExecutionPlan(context) {
        const mutationRequest = facebookAdMutationRequestFrom(context.event);
        const explicitPromoteSocialPost = facebookPromotionRequested(mutationRequest)
          && Boolean(sourcePostSummary(mutationRequest?.sourcePost))
          && (facebookPromotionMediaUrls(mutationRequest).length > 0 || Boolean(mutationRequest?.websiteUrl));
        const explicitEntityList = mutationRequest?.action === 'list_facebook_ad_entities'
          && Boolean(mutationRequest?.entityType || mutationRequest?.query?.entityType);
        const explicitCampaignFetch = mutationRequest?.action === 'get_facebook_campaign'
          && Boolean(mutationRequest?.campaignId);
        const explicitCampaignUpsert = explicitPromoteSocialPost || (
          ['upsert_facebook_campaign', 'build_facebook_ad_campaign'].includes(mutationRequest?.action)
          && Boolean(mutationRequest?.campaign)
          && Object.keys(mutationRequest.campaign).length > 0
        );
        const explicitAdsetUpsert = explicitPromoteSocialPost || (
          ['upsert_facebook_adset', 'build_facebook_ad_campaign'].includes(mutationRequest?.action)
          && Boolean(mutationRequest?.adset)
          && Object.keys(mutationRequest.adset).length > 0
        );
        const explicitAdUpsert = explicitPromoteSocialPost || (
          ['upsert_facebook_ad', 'build_facebook_ad_campaign'].includes(mutationRequest?.action)
          && Boolean(mutationRequest?.ad)
          && Object.keys(mutationRequest.ad).length > 0
        );
        const explicitPublish = mutationRequest?.action === 'publish_facebook_campaign'
          || ((mutationRequest?.action === 'build_facebook_ad_campaign' || explicitPromoteSocialPost) && mutationRequest?.publish === true);

        return [
          {
            name: 'list_facebook_ad_entities',
            kind: 'adapter_call',
            adapter: 'FacebookAdsAdapter',
            method: 'listEntities',
            pathHint: '/ad-publishing/facebook/entity',
            args: () => [context.credentialRef || defaultLocationCredential(context), facebookEntityQuery(mutationRequest)],
            safe: true,
            mutation: false,
            requiresCredential: true,
            resumeData: facebookEntityListResumeData,
            details: {
              action: 'list_facebook_ad_entities',
              entityType: mutationRequest?.entityType || mutationRequest?.query?.entityType || null,
              locationId: mutationRequest?.locationId || context.event.locationId || null
            },
            skipIf: () => !explicitEntityList
          },
          {
            name: 'get_facebook_campaign',
            kind: 'adapter_call',
            adapter: 'FacebookAdsAdapter',
            method: 'getCampaign',
            pathHint: '/ad-publishing/facebook/campaigns/:campaignId',
            args: () => [context.credentialRef || defaultLocationCredential(context), mutationRequest.campaignId, mutationRequest.query || {}],
            safe: true,
            mutation: false,
            requiresCredential: true,
            resumeData: facebookCampaignResumeData,
            details: {
              action: 'get_facebook_campaign',
              campaignId: mutationRequest?.campaignId || null
            },
            skipIf: () => !explicitCampaignFetch
          },
          {
            name: 'upsert_facebook_campaign',
            kind: 'adapter_call',
            adapter: 'FacebookAdsAdapter',
            method: 'upsertCampaign',
            httpMethod: 'PUT',
            pathHint: '/ad-publishing/facebook/campaigns',
            args: () => [context.credentialRef || defaultLocationCredential(context), facebookCampaignBody(mutationRequest)],
            safe: false,
            mutation: true,
            requiresApproval: true,
            requiresCredential: true,
            resumeData: facebookCampaignUpsertResumeData,
            details: {
              action: 'upsert_facebook_campaign',
              locationId: mutationRequest?.locationId || context.event.locationId || null,
              campaignId: mutationRequest?.campaignId || null,
              name: mutationRequest?.campaign?.name || inferredPromotionCampaignName(mutationRequest),
              objective: mutationRequest?.campaign?.objective || mutationRequest?.campaign?.goal || inferredPromotionObjective(mutationRequest),
              status: mutationRequest?.campaign?.status || inferredPromotionStatus(mutationRequest),
              sourceSummaryPreview: sourcePostSummary(mutationRequest?.sourcePost)?.slice(0, 160) || null
            },
            skipIf: () => !explicitCampaignUpsert
          },
          {
            name: 'upsert_facebook_adset',
            kind: 'adapter_call',
            adapter: 'FacebookAdsAdapter',
            method: 'upsertAdset',
            httpMethod: 'PUT',
            pathHint: '/ad-publishing/facebook/adsets',
            args: (runtimeContext) => [runtimeContext.credentialRef || defaultLocationCredential(runtimeContext), facebookAdsetBody(runtimeContext, mutationRequest)],
            safe: false,
            mutation: true,
            requiresApproval: true,
            requiresCredential: true,
            resumeData: facebookAdsetUpsertResumeData,
            details: (runtimeContext) => ({
              action: 'upsert_facebook_adset',
              locationId: mutationRequest?.locationId || runtimeContext.event.locationId || null,
              campaignId: resolvedFacebookCampaignId(runtimeContext, mutationRequest),
              adsetId: mutationRequest?.adsetId || null,
              name: mutationRequest?.adset?.name || inferredPromotionAdsetName(mutationRequest),
              status: mutationRequest?.adset?.status || inferredPromotionStatus(mutationRequest),
              sourceMediaCount: facebookPromotionMediaUrls(mutationRequest).length
            }),
            skipIf: (runtimeContext) => !explicitAdsetUpsert || !facebookCampaignDependencyReady(runtimeContext, mutationRequest)
          },
          {
            name: 'upsert_facebook_ad',
            kind: 'adapter_call',
            adapter: 'FacebookAdsAdapter',
            method: 'upsertAd',
            httpMethod: 'PUT',
            pathHint: '/ad-publishing/facebook/ads',
            args: (runtimeContext) => [runtimeContext.credentialRef || defaultLocationCredential(runtimeContext), facebookAdBody(runtimeContext, mutationRequest)],
            safe: false,
            mutation: true,
            requiresApproval: true,
            requiresCredential: true,
            resumeData: facebookAdUpsertResumeData,
            details: (runtimeContext) => ({
              action: 'upsert_facebook_ad',
              locationId: mutationRequest?.locationId || runtimeContext.event.locationId || null,
              campaignId: resolvedFacebookCampaignId(runtimeContext, mutationRequest),
              adsetId: resolvedFacebookAdsetId(runtimeContext, mutationRequest),
              adId: mutationRequest?.adId || null,
              name: mutationRequest?.ad?.name || inferredPromotionAdName(mutationRequest),
              status: mutationRequest?.ad?.status || inferredPromotionStatus(mutationRequest),
              websiteUrl: mutationRequest?.websiteUrl || null,
              mediaUrls: facebookPromotionMediaUrls(mutationRequest)
            }),
            skipIf: (runtimeContext) => !explicitAdUpsert || !facebookCampaignDependencyReady(runtimeContext, mutationRequest) || !facebookAdsetDependencyReady(runtimeContext, mutationRequest)
          },
          {
            name: 'publish_facebook_campaign',
            kind: 'adapter_call',
            adapter: 'FacebookAdsAdapter',
            method: 'publishCampaign',
            httpMethod: 'POST',
            pathHint: '/ad-publishing/facebook/campaigns/:campaignId/publish',
            args: (runtimeContext) => [runtimeContext.credentialRef || defaultLocationCredential(runtimeContext), resolvedFacebookCampaignId(runtimeContext, mutationRequest), {}],
            safe: false,
            mutation: true,
            requiresApproval: true,
            requiresCredential: true,
            details: (runtimeContext) => ({
              action: 'publish_facebook_campaign',
              campaignId: resolvedFacebookCampaignId(runtimeContext, mutationRequest),
              publishRequested: true
            }),
            skipIf: (runtimeContext) => !explicitPublish || !resolvedFacebookCampaignId(runtimeContext, mutationRequest)
          },
          {
            name: 'plan_facebook_ad_actions',
            kind: 'intent',
            safe: false,
            mutation: true,
            requiresApproval: true,
            details: {
              action: 'possible_facebook_ad_campaign_bundle',
              locationId: mutationRequest?.locationId || context.event.locationId || null,
              requestedAction: mutationRequest?.action || null
            },
            skipIf: () => explicitEntityList || explicitCampaignFetch || explicitCampaignUpsert || explicitAdsetUpsert || explicitAdUpsert || explicitPublish || explicitPromoteSocialPost
          }
        ];
      }
    },
    google_ad_pack: {
      trigger_events: ['ManualRun'],
      buildExecutionPlan(context) {
        const mutationRequest = googleAdMutationRequestFrom(context.event);
        const explicitIntegrationStatus = mutationRequest?.action === 'get_google_integration_status';
        const explicitPromoteSocialPost = googlePromotionRequested(mutationRequest)
          && Boolean(sourcePostSummary(mutationRequest?.sourcePost))
          && (googlePromotionMediaUrls(mutationRequest).length > 0 || Boolean(mutationRequest?.websiteUrl));
        const explicitEntityList = mutationRequest?.action === 'list_google_ad_entities'
          && Boolean(mutationRequest?.entityType || mutationRequest?.query?.entityType);
        const explicitCampaignFetch = (
          mutationRequest?.action === 'get_google_campaign'
          || (explicitPromoteSocialPost && Boolean(mutationRequest?.adId))
        ) && Boolean(mutationRequest?.adId);
        const explicitCampaignUpsert = explicitPromoteSocialPost || (
          ['upsert_google_campaign', 'build_google_ad_campaign'].includes(mutationRequest?.action)
          && Boolean(mutationRequest?.campaign)
          && Object.keys(mutationRequest.campaign).length > 0
        );
        const explicitPublish = mutationRequest?.action === 'publish_google_ad'
          || ((mutationRequest?.action === 'build_google_ad_campaign' || explicitPromoteSocialPost) && mutationRequest?.publish === true);
        const wantsGoogleExtensions = explicitPromoteSocialPost
          || Array.isArray(mutationRequest?.promotion?.sitelinks)
          || Boolean(mutationRequest?.promotion?.call)
          || Boolean(mutationRequest?.promotion?.callAsset);

        return [
          {
            name: 'get_google_integration_status',
            kind: 'adapter_call',
            adapter: 'GoogleAdsAdapter',
            method: 'getIntegration',
            pathHint: '/ad-publishing/google/integration',
            args: () => [context.credentialRef || defaultLocationCredential(context), { locationId: mutationRequest?.locationId || context.event.locationId || null }],
            safe: true,
            mutation: false,
            requiresCredential: true,
            resumeData: googleIntegrationResumeData,
            details: {
              action: 'get_google_integration_status',
              locationId: mutationRequest?.locationId || context.event.locationId || null
            },
            skipIf: () => !explicitIntegrationStatus
          },
          {
            name: 'list_google_ad_entities',
            kind: 'adapter_call',
            adapter: 'GoogleAdsAdapter',
            method: 'listEntities',
            pathHint: '/ad-publishing/google/entity',
            args: () => [context.credentialRef || defaultLocationCredential(context), googleEntityQuery(mutationRequest)],
            safe: true,
            mutation: false,
            requiresCredential: true,
            resumeData: googleEntityListResumeData,
            details: {
              action: 'list_google_ad_entities',
              entityType: mutationRequest?.entityType || mutationRequest?.query?.entityType || null,
              locationId: mutationRequest?.locationId || context.event.locationId || null
            },
            skipIf: () => !explicitEntityList
          },
          {
            name: 'get_google_campaign',
            kind: 'adapter_call',
            adapter: 'GoogleAdsAdapter',
            method: 'getCampaign',
            pathHint: '/ad-publishing/google/ads/:adId',
            args: () => [context.credentialRef || defaultLocationCredential(context), mutationRequest.adId, googleCampaignQuery(mutationRequest, context.event.locationId || null)],
            safe: true,
            mutation: false,
            requiresCredential: true,
            resumeData: googleCampaignResumeData,
            details: {
              action: 'get_google_campaign',
              adId: mutationRequest?.adId || null
            },
            skipIf: () => !explicitCampaignFetch
          },
          {
            name: 'get_google_call_assets',
            kind: 'adapter_call',
            adapter: 'GoogleAdsAdapter',
            method: 'getAssets',
            pathHint: '/ad-publishing/google/assets',
            args: () => [context.credentialRef || defaultLocationCredential(context), { locationId: mutationRequest?.locationId || context.event.locationId || null, type: 'CALL' }],
            safe: true,
            mutation: false,
            requiresCredential: true,
            resumeData: (liveResult) => googleAssetsListResumeData('CALL', liveResult),
            details: {
              action: 'get_google_call_assets',
              locationId: mutationRequest?.locationId || context.event.locationId || null
            },
            skipIf: () => !wantsGoogleExtensions
          },
          {
            name: 'upsert_google_extension_assets',
            kind: 'adapter_call',
            adapter: 'GoogleAdsAdapter',
            method: 'upsertAssets',
            httpMethod: 'POST',
            pathHint: '/ad-publishing/google/assets',
            args: (runtimeContext) => [runtimeContext.credentialRef || defaultLocationCredential(runtimeContext), googleExtensionAssetBodies(runtimeContext, mutationRequest)],
            safe: false,
            mutation: true,
            requiresApproval: true,
            requiresCredential: true,
            resumeData: (liveResult, runtimeContext) => googleAssetsUpsertResumeData(liveResult, googleExtensionAssetBodies(runtimeContext, mutationRequest)),
            details: (runtimeContext) => ({
              action: 'upsert_google_extension_assets',
              locationId: mutationRequest?.locationId || runtimeContext.event.locationId || null,
              assetCount: googleExtensionAssetBodies(runtimeContext, mutationRequest).length,
              sitelinks: googlePromotionSitelinkSpecs(mutationRequest).map((entry) => entry.linkText || entry.resourceName).filter(Boolean),
              createCallAsset: Boolean(googlePromotionCallPayload(mutationRequest)) && resolvedGoogleCallAssetResourceNames(runtimeContext).length === 0
            }),
            skipIf: (runtimeContext) => googleExtensionAssetBodies(runtimeContext, mutationRequest).length === 0
          },
          {
            name: 'upsert_google_campaign',
            kind: 'adapter_call',
            adapter: 'GoogleAdsAdapter',
            method: 'upsertCampaign',
            httpMethod: 'PUT',
            pathHint: '/ad-publishing/google/ads',
            args: (runtimeContext) => [runtimeContext.credentialRef || defaultLocationCredential(runtimeContext), googleCampaignBody(runtimeContext, mutationRequest)],
            safe: false,
            mutation: true,
            requiresApproval: true,
            requiresCredential: true,
            resumeData: googleCampaignUpsertResumeData,
            details: (runtimeContext) => ({
              action: 'upsert_google_campaign',
              locationId: mutationRequest?.locationId || runtimeContext.event.locationId || null,
              adId: mutationRequest?.adId || null,
              name: mutationRequest?.campaign?.name || inferredPromotionCampaignName(mutationRequest),
              status: mutationRequest?.campaign?.status || inferredPromotionStatus(mutationRequest),
              advertisingChannelType: mutationRequest?.campaign?.advertisingChannelType || mutationRequest?.campaign?.channelType || inferredGoogleAdvertisingChannelType(mutationRequest),
              websiteUrl: mutationRequest?.websiteUrl || null,
              mediaUrls: googlePromotionMediaUrls(mutationRequest),
              callAssetCount: resolvedGoogleCallAssetResourceNames(runtimeContext).length,
              sitelinkAssetCount: resolvedGoogleSitelinkResourceNames(runtimeContext, mutationRequest).length,
              keywordCounts: {
                positives: googlePromotionKeywords(mutationRequest).length,
                negatives: googlePromotionNegativeKeywords(mutationRequest).length
              },
              copyCounts: {
                headlines: googlePromotionHeadlines(mutationRequest).length,
                longHeadlines: googlePromotionLongHeadlines(mutationRequest).length,
                descriptions: googlePromotionDescriptions(mutationRequest).length
              },
              targeting: {
                selectedChannels: googlePromotionTargeting(mutationRequest).selectedChannels,
                customChannels: googlePromotionTargeting(mutationRequest).customChannels,
                audienceCounts: {
                  locales: googlePromotionTargeting(mutationRequest).audience.locales?.length || 0,
                  geoLocations: googlePromotionTargeting(mutationRequest).audience.geoLocations?.length || 0,
                  segments: googlePromotionTargeting(mutationRequest).audience.segments?.length || 0,
                  targetInterests: googlePromotionTargeting(mutationRequest).audience.targetInterests?.length || 0,
                  audiences: googlePromotionTargeting(mutationRequest).audience.audiences?.length || 0
                }
              }
            }),
            skipIf: () => !explicitCampaignUpsert
          },
          {
            name: 'generate_google_ad_preview',
            kind: 'adapter_call',
            adapter: 'PreviewArtifactsAdapter',
            method: 'generateGoogleAdPreview',
            pathHint: 'local://generated/google-ad-preview',
            args: (runtimeContext) => [
              runtimeContext.credentialRef || defaultLocationCredential(runtimeContext),
              {
                locationId: mutationRequest?.locationId || runtimeContext.event.locationId || null,
                adId: resolvedGoogleAdId(runtimeContext, mutationRequest)
              }
            ],
            safe: true,
            mutation: false,
            requiresCredential: true,
            resumeData: googlePreviewResultResumeData,
            details: (runtimeContext) => ({
              action: 'generate_google_ad_preview',
              locationId: mutationRequest?.locationId || runtimeContext.event.locationId || null,
              adId: resolvedGoogleAdId(runtimeContext, mutationRequest)
            }),
            skipIf: (runtimeContext) => {
              const campaignWrite = runtimeContext?.runtime?.stepOutputs?.upsert_google_campaign;
              return !explicitCampaignUpsert || !resolvedGoogleAdId(runtimeContext, mutationRequest) || campaignWrite?.ok !== true;
            }
          },
          {
            name: 'preflight_google_publish',
            kind: 'adapter_call',
            adapter: 'GoogleAdsAdapter',
            method: 'preflightPublish',
            pathHint: '/ad-publishing/google/ads/:adId',
            args: (runtimeContext) => [runtimeContext.credentialRef || defaultLocationCredential(runtimeContext), resolvedGoogleAdId(runtimeContext, mutationRequest), googleCampaignQuery(mutationRequest, runtimeContext.event.locationId || null)],
            safe: true,
            mutation: false,
            requiresCredential: true,
            resumeData: googlePublishPreflightResumeData,
            details: (runtimeContext) => ({
              action: 'preflight_google_publish',
              adId: resolvedGoogleAdId(runtimeContext, mutationRequest),
              publishRequested: true,
              readiness: googlePublishReadinessFrom(runtimeContext)
            }),
            skipIf: (runtimeContext) => !explicitPublish || !resolvedGoogleAdId(runtimeContext, mutationRequest)
          },
          {
            name: 'publish_google_ad',
            kind: 'adapter_call',
            adapter: 'GoogleAdsAdapter',
            method: 'publishAd',
            httpMethod: 'POST',
            pathHint: '/ad-publishing/google/ads/:adId/publish',
            args: (runtimeContext) => [runtimeContext.credentialRef || defaultLocationCredential(runtimeContext), resolvedGoogleAdId(runtimeContext, mutationRequest), {}],
            safe: false,
            mutation: true,
            requiresApproval: true,
            requiresCredential: true,
            details: (runtimeContext) => ({
              action: 'publish_google_ad',
              adId: resolvedGoogleAdId(runtimeContext, mutationRequest),
              publishRequested: true,
              readiness: googlePublishReadinessFrom(runtimeContext)
            }),
            skipIf: (runtimeContext) => !explicitPublish || !resolvedGoogleAdId(runtimeContext, mutationRequest)
          },
          {
            name: 'plan_google_ad_actions',
            kind: 'intent',
            safe: false,
            mutation: true,
            requiresApproval: true,
            details: {
              action: 'possible_google_ad_campaign_bundle',
              locationId: mutationRequest?.locationId || context.event.locationId || null,
              requestedAction: mutationRequest?.action || null
            },
            skipIf: () => explicitIntegrationStatus || explicitEntityList || explicitCampaignFetch || explicitCampaignUpsert || explicitPublish || explicitPromoteSocialPost
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
