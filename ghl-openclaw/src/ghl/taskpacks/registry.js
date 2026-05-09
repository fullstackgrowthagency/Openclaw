import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
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

function normalizeIntegerArray(values) {
  return Array.isArray(values)
    ? values
      .map((value) => normalizeNumber(value))
      .filter((value) => Number.isInteger(value) && value > 0)
    : [];
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

function normalizeBusinessNameKey(value) {
  return normalizeString(value)?.toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim() || null;
}

function resolveWorkspaceRoot(fromDir = process.cwd()) {
  let current = path.resolve(fromDir);
  for (let depth = 0; depth < 4; depth += 1) {
    if (existsSync(path.join(current, 'START_PROTOCOL.md'))) return current;
    if (existsSync(path.join(current, 'business', 'ACTIVE_BUSINESS.json'))) return current;
    if (existsSync(path.join(current, 'business', 'ACTIVE_BUSINESS.template.json'))) return current;
    const parent = path.dirname(current);
    if (parent === current) break;
    current = parent;
  }
  return path.resolve(fromDir, '..');
}

function readActiveBusinessRecord(fromDir = process.cwd()) {
  const activeBusinessPath = path.join(resolveWorkspaceRoot(fromDir), 'business', 'ACTIVE_BUSINESS.json');
  if (!existsSync(activeBusinessPath)) return null;
  try {
    return JSON.parse(readFileSync(activeBusinessPath, 'utf8'));
  } catch {
    return null;
  }
}

function activeBusinessDefaultsFromRecord(record) {
  const activeBusiness = normalizePlainObject(record) || {};
  const profile = normalizePlainObject(activeBusiness.business || activeBusiness.businessProfile || activeBusiness.profile || activeBusiness.details) || {};
  const knowledgeBase = normalizePlainObject(activeBusiness.knowledgeBase || profile.knowledgeBase) || {};
  const brand = normalizePlainObject(activeBusiness.brand || profile.brand) || {};
  const ghlAccess = normalizePlainObject(activeBusiness.access?.ghl || activeBusiness.ghl || profile.ghl) || {};

  return {
    name: normalizeString(activeBusiness.businessName)
      || normalizeString(activeBusiness.name)
      || normalizeString(profile.name)
      || normalizeString(profile.businessName)
      || null,
    industry: normalizeString(activeBusiness.industry)
      || normalizeString(activeBusiness.category)
      || normalizeString(profile.industry)
      || normalizeString(profile.category)
      || null,
    audience: normalizeStringArray(activeBusiness.audience || activeBusiness.audiences || profile.audience || profile.audiences),
    offers: normalizeStringArray(activeBusiness.offers || activeBusiness.services || activeBusiness.solutions || profile.offers || profile.services || profile.solutions),
    differentiators: normalizeStringArray(activeBusiness.differentiators || activeBusiness.strengths || activeBusiness.advantages || profile.differentiators || profile.strengths || profile.advantages),
    goals: normalizeStringArray(activeBusiness.goals || activeBusiness.outcomes || profile.goals || profile.outcomes),
    ctas: normalizeStringArray(activeBusiness.ctas || activeBusiness.callsToAction || profile.ctas || profile.callsToAction),
    hashtags: normalizeStringArray(activeBusiness.hashtags || profile.hashtags),
    knowledgeBaseRef: normalizeString(activeBusiness.knowledgeBaseRef)
      || normalizeString(activeBusiness.knowledgeBaseId)
      || normalizeString(knowledgeBase.ref)
      || normalizeString(knowledgeBase.id)
      || null,
    websiteUrl: normalizeString(activeBusiness.website)
      || normalizeString(activeBusiness.websiteUrl)
      || normalizeString(brand.website)
      || null,
    logoUrl: normalizeString(activeBusiness.logo)
      || normalizeString(activeBusiness.logoUrl)
      || normalizeString(brand.logo)
      || null,
    brandVoice: normalizeString(activeBusiness.brandVoice)
      || normalizeString(activeBusiness.voice)
      || normalizeString(brand.voice)
      || null,
    locationId: normalizeString(activeBusiness.locationId)
      || normalizeString(ghlAccess.locationId)
      || null
  };
}

function shouldUseActiveBusinessDefaults(requestedBusinessName, activeBusiness) {
  if (!activeBusiness) return false;
  const activeBusinessName = normalizeString(activeBusiness.name);
  if (!normalizeString(requestedBusinessName)) return Boolean(activeBusinessName);
  if (!activeBusinessName) return false;
  return normalizeBusinessNameKey(requestedBusinessName) === normalizeBusinessNameKey(activeBusinessName);
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
  const plainTextRequest = normalizeTextBlock(
    request.requestText
    || request.brief
    || request.description
    || request.instructions
    || request.prompt
    || ((!request.action && !request.contactName && !request.name && !request.type && !request.messageType) ? request.text : null)
  );
  const parsedRequest = parseConversationRequestText(plainTextRequest);
  const action = normalizeConversationAction(request.action)
    || parsedRequest.action
    || (!request.action && looksLikeConversationSendRequestText(plainTextRequest) && (normalizeString(request.contactName) || normalizeString(request.name) || parsedRequest.contactName) && (normalizeString(request.message) || parsedRequest.message) && (normalizeConversationMessageType(request.messageType) || normalizeConversationMessageType(request.type) || parsedRequest.messageType)
      ? 'send_conversation_message'
      : null);
  const normalizedMessageType = normalizeConversationMessageType(request.messageType)
    || normalizeConversationMessageType(request.type)
    || parsedRequest.messageType;
  const normalizedContactName = normalizeString(request.contactName)
    || normalizeString(request.name)
    || parsedRequest.contactName;
  const normalizedMessage = normalizeString(request.message)
    || parsedRequest.message;
  return {
    action,
    conversationId: request.conversationId || parsedRequest.conversationId || objectIdFrom(event),
    locationId: request.locationId || event?.locationId || event?.payload?.locationId || null,
    contactId: request.contactId || null,
    contactName: normalizedContactName,
    message: normalizedMessage,
    messageType: normalizedMessageType,
    channel: normalizeString(request.channel) || parsedRequest.channel,
    searchQuery: normalizeString(request.searchQuery) || normalizeString(request.query) || normalizeString(request.search) || parsedRequest.searchQuery,
    limit: normalizeNumber(request.limit ?? request.count) || parsedRequest.limit,
    unreadOnly: normalizeBoolean(request.unreadOnly) ?? parsedRequest.unreadOnly,
    openOnly: normalizeBoolean(request.openOnly) ?? parsedRequest.openOnly,
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

  const plainTextRequest = normalizeTextBlock(
    request.requestText
    || request.brief
    || request.description
    || request.text
    || request.instructions
    || request.prompt
    || request.message
  );
  const parsedRequest = parseSocialPostRequestText(plainTextRequest);

  const rawPostDefaults = request.postDefaults && typeof request.postDefaults === 'object' && !Array.isArray(request.postDefaults)
    ? { ...request.postDefaults }
    : {};
  const rawPost = request.post && typeof request.post === 'object' && !Array.isArray(request.post)
    ? { ...request.post }
    : {};
  const rawPosts = Array.isArray(request.posts)
    ? request.posts.filter((post) => post && typeof post === 'object' && !Array.isArray(post)).map((post) => ({ ...post }))
    : [];
  const accountIds = normalizeStringArray(
    request.accountIds
    || parsedRequest.accountIds
    || rawPost.accountIds
    || rawPostDefaults.accountIds
    || [request.accountId, rawPost.accountId].filter(Boolean)
  );
  const status = normalizeString(request.status) || normalizeString(parsedRequest.status) || normalizeString(rawPost.status) || 'draft';
  const scheduleDate = normalizeString(request.scheduleDate)
    || normalizeString(request.scheduledAt)
    || normalizeString(parsedRequest.scheduleDate)
    || normalizeString(rawPost.scheduleDate)
    || normalizeString(rawPost.scheduledAt);
  const summary = normalizeString(request.summary)
    || normalizeString(parsedRequest.summary)
    || (request.action ? normalizeString(request.text) : null)
    || normalizeString(rawPost.summary)
    || normalizeString(rawPost.text);
  const requestedBusinessName = normalizeString(request.businessName)
    || normalizeString(parsedRequest.businessName)
    || normalizeString(rawPost.businessName)
    || normalizeString(rawPostDefaults.businessName)
    || null;
  const activeBusiness = activeBusinessDefaultsFromRecord(readActiveBusinessRecord(process.cwd()));
  const useActiveBusiness = shouldUseActiveBusinessDefaults(requestedBusinessName, activeBusiness);
  const activeBusinessDefaults = useActiveBusiness ? activeBusiness : null;
  const eventLocationId = normalizeString(event?.locationId) || normalizeString(event?.payload?.locationId) || null;
  const preferredEventLocationId = eventLocationId && eventLocationId !== 'demo-location' ? eventLocationId : null;
  const businessName = requestedBusinessName || normalizeString(activeBusinessDefaults?.name) || null;
  const creativeStyle = normalizeString(request.creativeStyle)
    || normalizeString(request.visualStyle)
    || normalizeString(request.style)
    || normalizeString(rawPost.creativeStyle)
    || normalizeString(rawPost.visualStyle)
    || normalizeString(rawPost.style)
    || normalizeString(rawPostDefaults.creativeStyle)
    || normalizeString(rawPostDefaults.visualStyle)
    || normalizeString(rawPostDefaults.style);
  const creativeStyles = normalizeStringArray(
    request.creativeStyles
    || parsedRequest.creativeStyles
    || request?.creative?.styles
    || rawPost.creativeStyles
    || rawPostDefaults.creativeStyles
  );
  const variantCount = normalizeVariantCount(
    request.variantCount
    || request.variants
    || request.versionCount
    || request.versions
    || request?.creative?.variantCount
    || request?.creative?.variants
    || parsedRequest.variantCount
    || rawPost.variantCount
    || rawPost.variants
    || rawPostDefaults.variantCount
    || rawPostDefaults.variants,
    4
  );
  const brandVoice = normalizeString(request.brandVoice)
    || normalizeString(request.voice)
    || normalizeString(rawPost.brandVoice)
    || normalizeString(rawPost.voice)
    || normalizeString(rawPostDefaults.brandVoice)
    || normalizeString(rawPostDefaults.voice)
    || normalizeString(activeBusinessDefaults?.brandVoice)
    || null;
  const websiteUrl = normalizeString(request.websiteUrl)
    || normalizeString(request.website)
    || normalizeString(rawPost.websiteUrl)
    || normalizeString(rawPost.website)
    || normalizeString(rawPostDefaults.websiteUrl)
    || normalizeString(rawPostDefaults.website)
    || normalizeString(activeBusinessDefaults?.websiteUrl)
    || null;
  const logoUrl = normalizeString(request.logoUrl)
    || normalizeString(request.logo)
    || normalizeString(rawPost.logoUrl)
    || normalizeString(rawPost.logo)
    || normalizeString(rawPostDefaults.logoUrl)
    || normalizeString(rawPostDefaults.logo)
    || normalizeString(activeBusinessDefaults?.logoUrl)
    || null;

  if (accountIds.length > 0 && !Array.isArray(rawPost.accountIds)) rawPost.accountIds = accountIds;
  if (status && !rawPost.status) rawPost.status = status;
  if (scheduleDate && !rawPost.scheduleDate && !rawPost.scheduledAt) rawPost.scheduleDate = scheduleDate;
  if (summary && !rawPost.summary && !rawPost.text) rawPost.summary = summary;
  if (businessName && !rawPost.businessName) rawPost.businessName = businessName;
  if (creativeStyle && !rawPost.creativeStyle && !rawPost.visualStyle && !rawPost.style) rawPost.creativeStyle = creativeStyle;
  if (creativeStyles.length > 0 && !Array.isArray(rawPost.creativeStyles)) rawPost.creativeStyles = creativeStyles;
  if (variantCount && !rawPost.variantCount && !rawPost.variants) rawPost.variantCount = variantCount;
  if (brandVoice && !rawPost.brandVoice && !rawPost.voice) rawPost.brandVoice = brandVoice;
  if (websiteUrl && !rawPost.websiteUrl && !rawPost.website) rawPost.websiteUrl = websiteUrl;
  if (logoUrl && !rawPost.logoUrl && !rawPost.logo) rawPost.logoUrl = logoUrl;

  return {
    action: request.action
      || (!request.action && plainTextRequest && looksLikeSocialPostRequestText(plainTextRequest) ? 'create_social_post' : null),
    locationId: request.locationId || preferredEventLocationId || normalizeString(activeBusinessDefaults?.locationId) || eventLocationId || null,
    accountIds,
    status,
    scheduleDate,
    summary,
    businessName,
    creativeStyle,
    creativeStyles,
    variantCount,
    brandVoice,
    websiteUrl,
    logoUrl,
    continueOnError: normalizeBoolean(request.continueOnError),
    postDefaults: rawPostDefaults,
    post: rawPost,
    posts: rawPosts
  };
}

function normalizeTextBlock(value) {
  if (typeof value === 'string' && value.trim()) return value.trim();
  if (Array.isArray(value)) {
    const joined = value
      .map((entry) => (typeof entry === 'string' && entry.trim() ? entry.trim() : null))
      .filter(Boolean)
      .join('\n');
    return joined || null;
  }
  return null;
}

function uniqueIntegers(values = []) {
  const seen = new Set();
  const output = [];
  for (const value of values) {
    const normalized = normalizeNumber(value);
    if (!Number.isInteger(normalized) || normalized <= 0 || seen.has(normalized)) continue;
    seen.add(normalized);
    output.push(normalized);
  }
  return output;
}

function parseInstructionDayNumbers(text) {
  if (!text) return [];
  return uniqueIntegers(Array.from(text.matchAll(/\b(\d{1,3})\b/g)).map((match) => Number(match[1])));
}

function splitInstructionBlocks(text) {
  if (!text) return [];
  return text
    .replace(/\r/g, '\n')
    .split(/\n+/)
    .flatMap((line) => line.split(/\s*;\s*/g))
    .map((line) => line.replace(/^(?:[-*•]\s+|\d+[.)]\s+)/, '').trim())
    .filter(Boolean);
}

function parseCalendarPostsFromText(text) {
  return splitInstructionBlocks(text)
    .map((line) => {
      const match = line.match(/^(?:day|post)?\s*(\d+)\s*[:.-]\s*(.+)$/i);
      if (!match) return null;
      const day = normalizeNumber(match[1]);
      const summary = normalizeString(match[2]);
      return day && summary ? { day, summary } : null;
    })
    .filter(Boolean);
}

function mergePostInstructions(primary = [], secondary = []) {
  return [
    ...primary.filter((item) => item && typeof item === 'object' && !Array.isArray(item)).map((item) => ({ ...item })),
    ...secondary.filter((item) => item && typeof item === 'object' && !Array.isArray(item)).map((item) => ({ ...item }))
  ];
}

function parseSocialCalendarReviewInstructions(value) {
  const text = normalizeTextBlock(value);
  const parsed = {
    sourceText: text,
    removeDays: [],
    replacePosts: [],
    appendPosts: [],
    postEdits: []
  };
  if (!text) return parsed;

  const replaceCalendarMatch = text.match(/(?:^|\n)(?:replace|swap(?:\s+out)?)\s+(?:the\s+)?(?:whole\s+)?(?:calendar|all\s+posts|all\s+days)\s*:?\s*([\s\S]+)$/i);
  if (replaceCalendarMatch) {
    parsed.replacePosts = parseCalendarPostsFromText(replaceCalendarMatch[1]);
  }

  for (const line of splitInstructionBlocks(text)) {
    if (/^(?:replace|swap(?:\s+out)?)\s+(?:the\s+)?(?:whole\s+)?(?:calendar|all\s+posts|all\s+days)\b/i.test(line)) {
      continue;
    }

    if (/\b(?:remove|drop|delete|skip)\b/i.test(line) && /\b(?:day|days|post|posts)\b/i.test(line)) {
      parsed.removeDays.push(...parseInstructionDayNumbers(line));
      continue;
    }

    const editMatch = line.match(/^(?:edit|rewrite|revise|update|change|replace)\s+(?:day|post)\s*(\d+)\s*[:=-]\s*(.+)$/i);
    if (editMatch) {
      const day = normalizeNumber(editMatch[1]);
      const summary = normalizeString(editMatch[2]);
      if (day && summary) parsed.postEdits.push({ day, summary });
      continue;
    }

    const appendMatch = line.match(/^(?:add|append|include)\s+(?:a\s+|an\s+)?post(?:\s+for\s+day\s*(\d+))?\s*[:=-]\s*(.+)$/i);
    if (appendMatch) {
      const day = normalizeNumber(appendMatch[1]);
      const summary = normalizeString(appendMatch[2]);
      if (summary) parsed.appendPosts.push(day ? { day, summary } : { summary });
      continue;
    }

    const looseAppendMatch = line.match(/^(?:new\s+post|extra\s+post)\s*[:=-]\s*(.+)$/i);
    if (looseAppendMatch) {
      const summary = normalizeString(looseAppendMatch[1]);
      if (summary) parsed.appendPosts.push({ summary });
    }
  }

  parsed.removeDays = uniqueIntegers(parsed.removeDays);
  return parsed;
}

function escapeRegex(value) {
  return String(value || '').replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function extractLabeledText(text, labels = []) {
  const source = normalizeTextBlock(text);
  if (!source) return null;
  for (const label of labels) {
    const match = source.match(new RegExp(`(?:^|\\n)\\s*${escapeRegex(label)}\\s*[:=-]\\s*(.+)$`, 'im'));
    const value = normalizeString(match?.[1]);
    if (value) return value;
  }
  return null;
}

function parseInlineList(value) {
  const text = normalizeTextBlock(value);
  if (!text) return [];
  return text
    .split(/\s*,\s*|\s+\band\b\s+/i)
    .map((item) => normalizeString(item?.replace(/^[-*•]\s*/, '')))
    .filter(Boolean);
}

function normalizeStyleToken(value) {
  const token = normalizeString(value)?.toLowerCase().replace(/[_\s]+/g, '-');
  if (!token) return null;
  if (['realistic', 'realistic-scene'].includes(token)) return 'realistic-scene';
  if (['hybrid', 'hybrid-system', 'system'].includes(token)) return 'hybrid-system';
  if (['infographic', 'info-graphic'].includes(token)) return 'infographic';
  if (['editorial', 'magazine'].includes(token)) return 'editorial';
  return token;
}

function detectStyleListFromText(value) {
  const text = normalizeTextBlock(value)?.toLowerCase() || '';
  const detected = [];
  if (/\brealistic(?:[-\s]+scene)?\b/.test(text)) detected.push('realistic-scene');
  if (/\bhybrid(?:[-\s]+system)?\b/.test(text)) detected.push('hybrid-system');
  if (/\binfographic\b/.test(text)) detected.push('infographic');
  if (/\beditorial\b/.test(text)) detected.push('editorial');
  return [...new Set(detected)];
}

function parseStyleList(value) {
  const explicit = parseInlineList(value).map((item) => normalizeStyleToken(item)).filter(Boolean);
  if (explicit.length > 0) return [...new Set(explicit)];
  return detectStyleListFromText(value);
}

function normalizeVariantCount(value, max = 4) {
  const normalized = normalizeNumber(value);
  if (!Number.isFinite(normalized) || normalized < 1) return null;
  return Math.min(max, Math.max(1, Math.round(normalized)));
}

function normalizeConversationMessageType(value) {
  const normalized = normalizeString(value)?.toLowerCase().replace(/[_\s-]+/g, ' ');
  if (!normalized) return null;
  if (['sms', 'text', 'text message'].includes(normalized)) return 'SMS';
  if (['email', 'e mail'].includes(normalized)) return 'Email';
  if (['whatsapp', 'wa'].includes(normalized)) return 'WhatsApp';
  if (['live chat', 'livechat', 'chat', 'web chat', 'webchat'].includes(normalized)) return 'Live_Chat';
  return normalizeString(value);
}

function normalizeConversationAction(value) {
  const normalized = normalizeString(value)?.toLowerCase().replace(/[_\s-]+/g, '_');
  if (!normalized) return null;
  if (['send', 'send_message', 'send_conversation', 'send_conversation_message'].includes(normalized)) return 'send_conversation_message';
  if (['reply', 'reply_latest', 'reply_to_latest', 'reply_latest_thread', 'reply_latest_conversation'].includes(normalized)) return 'reply_latest_conversation';
  if (['fetch', 'get_conversation', 'read_conversation', 'conversation_summary', 'fetch_conversation_summary'].includes(normalized)) return 'fetch_conversation_summary';
  if (['list_conversations', 'search', 'search_conversations', 'find_conversations'].includes(normalized)) return 'search_conversations';
  return normalizeString(value);
}

function detectConversationMessageTypeFromText(text) {
  const source = normalizeTextBlock(text)?.toLowerCase() || '';
  if (!source) return null;
  if (/\bwhats?app\b/.test(source)) return 'WhatsApp';
  if (/\bemail\b|\be-mail\b/.test(source)) return 'Email';
  if (/\blive\s*chat\b|\bweb\s*chat\b/.test(source)) return 'Live_Chat';
  if (/\btext\b|\bsms\b|\bmessage\b/.test(source)) return 'SMS';
  return null;
}

function extractConversationContactNameFromText(text) {
  const labeled = extractLabeledText(text, ['contact', 'contact name', 'lead', 'person', 'to']);
  if (labeled) return labeled;

  const natural = text?.match(/\b(?:send\s+)?(?:an?\s+)?(?:sms|text|message|email|whatsapp|whats?app\s+message)\s+to\s+([^\n:,-]+?)(?=(?:\s+(?:saying|that)\b|\s*[:,-]|$))/i)
    || text?.match(/\b(?:text|message|email|whatsapp|whats?app)\s+([^\n:,-]+?)(?=(?:\s+(?:saying|that)\b|\s*[:,-]|$))/i)
    || text?.match(/\b(?:reply|respond|follow\s*up)(?:\s+(?:to|with))?(?:\s+(?:the\s+)?latest\s+(?:conversation|thread)(?:\s+for)?)?\s+([^\n:,-]+?)(?=(?:\s+(?:saying|that)\b|\s*[:,-]|$))/i)
    || text?.match(/\b(?:show|read|fetch|get|summari[sz]e)(?:\s+(?:the\s+)?)?(?:latest\s+|recent\s+)?(?:conversation|thread|messages?)(?:\s+(?:with|for))\s+([^\n:,-]+?)(?=(?:\s*[?.!]|$))/i);
  return normalizeString(natural?.[1]);
}

function extractConversationMessageFromText(text) {
  const labeled = extractLabeledText(text, ['message', 'body', 'reply']);
  if (labeled) return labeled;

  const quoted = text?.match(/["“]([^"”]+)["”]\s*$/);
  if (normalizeString(quoted?.[1])) return normalizeString(quoted[1]);

  const natural = text?.match(/\b(?:saying|that)\s+([\s\S]+)$/i)
    || text?.match(/\b(?:send\s+)?(?:an?\s+)?(?:sms|text|message|email|whatsapp|whats?app\s+message)\s+to\s+[^\n:,-]+?\s*[:,-]\s*([\s\S]+)$/i)
    || text?.match(/\b(?:text|message|email|whatsapp|whats?app)\s+[^\n:,-]+?\s*[:,-]\s*([\s\S]+)$/i)
    || text?.match(/\b(?:reply|respond|follow\s*up)(?:\s+(?:to|with))?(?:\s+(?:the\s+)?latest\s+(?:conversation|thread)(?:\s+for)?)?\s+[^\n:,-]+?\s*(?:[:,-]|\b(?:saying|that)\b)\s*([\s\S]+)$/i);
  return normalizeString(natural?.[1]);
}

function detectConversationUnreadOnlyFromText(text) {
  const source = normalizeTextBlock(text)?.toLowerCase() || '';
  if (!source) return null;
  if (/\b(?:unread|needs?\s+reply|awaiting\s+reply)\b/.test(source)) return true;
  return null;
}

function detectConversationOpenOnlyFromText(text) {
  const source = normalizeTextBlock(text)?.toLowerCase() || '';
  if (!source) return null;
  if (/\bopen\s+(?:conversations|threads|inbox)\b|\bactive\s+(?:conversations|threads)\b/.test(source)) return true;
  return null;
}

function extractConversationLimitFromText(text) {
  const labeled = normalizeNumber(extractLabeledText(text, ['limit', 'count', 'max']));
  if (Number.isInteger(labeled) && labeled > 0) return labeled;
  const natural = text?.match(/\b(?:top|latest|last|recent)\s+(\d{1,2})\b/i)
    || text?.match(/\b(\d{1,2})\s+(?:open|unread|latest|recent)?\s*(?:conversations|threads)\b/i);
  const parsed = normalizeNumber(natural?.[1]);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : null;
}

function looksLikeConversationReplyRequestText(text) {
  const source = normalizeTextBlock(text)?.toLowerCase() || '';
  if (!source) return false;
  if (/\bsocial\s+post\b|\bsocial\s+calendar\b|\bappointment\b/.test(source)) return false;
  return /\b(?:reply|respond|follow\s*up)\b/.test(source);
}

function looksLikeConversationSearchRequestText(text) {
  const source = normalizeTextBlock(text)?.toLowerCase() || '';
  if (!source) return false;
  if (/\bsocial\s+post\b|\bsocial\s+calendar\b|\bappointment\b/.test(source)) return false;
  return /\b(?:list|search|find)\b.*\b(?:conversations|threads|inbox)\b/.test(source)
    || /\b(?:unread|open)\s+(?:conversations|threads|inbox)\b/.test(source)
    || /\b(?:show|get|fetch)\b.*\b(?:unread|open|recent|latest)\s+(?:conversations|threads)\b/.test(source);
}

function looksLikeConversationSummaryRequestText(text) {
  const source = normalizeTextBlock(text)?.toLowerCase() || '';
  if (!source) return false;
  if (/\bsocial\s+post\b|\bsocial\s+calendar\b|\bappointment\b/.test(source)) return false;
  if (looksLikeConversationSearchRequestText(source) || looksLikeConversationReplyRequestText(source) || looksLikeConversationSendRequestText(source)) return false;
  return /\b(?:summari[sz]e|summary|read)\b.*\b(?:conversation|thread|messages?)\b/.test(source)
    || /\b(?:show|get|fetch)\b.*\b(?:latest|recent)\s+(?:conversation|thread|messages?)\b/.test(source);
}

function detectConversationActionFromText(text, parsed = {}) {
  if (!text) return null;
  if (looksLikeConversationReplyRequestText(text) && (parsed.contactName || parsed.conversationId) && parsed.message) return 'reply_latest_conversation';
  if (looksLikeConversationSendRequestText(text) && parsed.contactName && parsed.message && parsed.messageType) return 'send_conversation_message';
  if (looksLikeConversationSearchRequestText(text)) return 'search_conversations';
  if (looksLikeConversationSummaryRequestText(text) && (parsed.contactName || parsed.conversationId || /\b(?:latest|recent)\s+(?:conversation|thread|messages?)\b/i.test(text))) return 'fetch_conversation_summary';
  return null;
}

function parseConversationRequestText(value) {
  const text = normalizeTextBlock(value);
  if (!text) {
    return {
      sourceText: null,
      action: null,
      conversationId: null,
      contactName: null,
      message: null,
      messageType: null,
      channel: null,
      searchQuery: null,
      limit: null,
      unreadOnly: null,
      openOnly: null
    };
  }

  const parsed = {
    sourceText: text,
    action: null,
    conversationId: normalizeString(extractLabeledText(text, ['conversation id', 'conversation', 'thread id'])),
    contactName: extractConversationContactNameFromText(text),
    message: extractConversationMessageFromText(text),
    messageType: normalizeConversationMessageType(extractLabeledText(text, ['message type', 'type'])) || detectConversationMessageTypeFromText(text),
    channel: normalizeString(extractLabeledText(text, ['channel'])),
    searchQuery: normalizeString(extractLabeledText(text, ['query', 'search', 'search query'])),
    limit: extractConversationLimitFromText(text),
    unreadOnly: detectConversationUnreadOnlyFromText(text),
    openOnly: detectConversationOpenOnlyFromText(text)
  };

  parsed.action = detectConversationActionFromText(text, parsed);
  return parsed;
}

function looksLikeConversationSendRequestText(text) {
  const source = normalizeTextBlock(text)?.toLowerCase() || '';
  if (!source) return false;
  if (/\bsocial\s+post\b|\bsocial\s+calendar\b|\bappointment\b/.test(source)) return false;
  return /\b(?:send\s+)?(?:an?\s+)?(?:sms|text|message|email|whatsapp|whats?app\s+message)\b/.test(source);
}

function parseDateToIso(value) {
  const text = normalizeString(value);
  if (!text) return null;
  const parsed = Date.parse(text);
  return Number.isNaN(parsed) ? null : new Date(parsed).toISOString();
}

function utcStartOfDay(value = new Date()) {
  const date = new Date(value);
  return new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate(), 0, 0, 0, 0));
}

function utcStartOfWeek(value = new Date()) {
  const date = utcStartOfDay(value);
  const offset = (date.getUTCDay() + 6) % 7;
  date.setUTCDate(date.getUTCDate() - offset);
  return date;
}

function utcStartOfMonth(value = new Date()) {
  const date = new Date(value);
  return new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), 1, 0, 0, 0, 0));
}

function utcAddDays(value, days = 0) {
  const date = new Date(value);
  date.setUTCDate(date.getUTCDate() + days);
  return date;
}

function inferCalendarWindowFromText(text, now = new Date()) {
  const source = normalizeTextBlock(text)?.toLowerCase() || '';
  if (!source) return null;

  if (/\bnext\s+month(?:['’]s?)?\s+posts?\b|\bposts?\s+for\s+next\s+month\b/.test(source)) {
    const start = utcStartOfMonth(new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + 1, 1)));
    const end = utcStartOfMonth(new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + 2, 1)));
    const postCount = Math.round((end.getTime() - start.getTime()) / 86400000);
    return { startDate: start.toISOString(), postCount, cadenceDays: 1, window: 'next_month' };
  }

  if (/\bthis\s+month(?:['’]s?)?\s+posts?\b|\bposts?\s+for\s+this\s+month\b/.test(source)) {
    const start = utcStartOfMonth(now);
    const end = utcStartOfMonth(new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + 1, 1)));
    const postCount = Math.round((end.getTime() - start.getTime()) / 86400000);
    return { startDate: start.toISOString(), postCount, cadenceDays: 1, window: 'this_month' };
  }

  if (/\bnext\s+week(?:['’]s?)?\s+posts?\b|\bposts?\s+for\s+next\s+week\b/.test(source)) {
    const start = utcAddDays(utcStartOfWeek(now), 7);
    return { startDate: start.toISOString(), postCount: 7, cadenceDays: 1, window: 'next_week' };
  }

  if (/\bthis\s+week(?:['’]s?)?\s+posts?\b|\bposts?\s+for\s+this\s+week\b/.test(source)) {
    const start = utcStartOfWeek(now);
    return { startDate: start.toISOString(), postCount: 7, cadenceDays: 1, window: 'this_week' };
  }

  const nextDays = source.match(/\bnext\s+(\d{1,3})\s+days'?\s+posts?\b|\bposts?\s+for\s+the\s+next\s+(\d{1,3})\s+days\b/);
  const dayCount = normalizeNumber(nextDays?.[1] || nextDays?.[2]);
  if (Number.isFinite(dayCount) && dayCount > 0) {
    const start = utcAddDays(utcStartOfDay(now), 1);
    return { startDate: start.toISOString(), postCount: dayCount, cadenceDays: 1, window: 'next_days' };
  }

  return null;
}

function extractPostCountFromText(text, inferredWindow = null) {
  const labeled = normalizeNumber(extractLabeledText(text, ['post count', 'posts', 'number of posts']));
  if (Number.isFinite(labeled) && labeled > 0) return labeled;
  const natural = text?.match(/\b(\d{1,3})\s*(?:-|\s)?post(?:s)?\b/i);
  const naturalCount = normalizeNumber(natural?.[1]);
  if (Number.isFinite(naturalCount) && naturalCount > 0) return naturalCount;
  return normalizeNumber(inferredWindow?.postCount);
}

function extractCadenceDaysFromText(text, inferredWindow = null) {
  const labeled = normalizeNumber(extractLabeledText(text, ['cadence days', 'cadence', 'every']));
  if (Number.isFinite(labeled) && labeled > 0) return labeled;

  const lines = splitInstructionBlocks(text);
  for (const line of lines) {
    const everyDays = line.match(/\bevery\s+(\d{1,3})\s+days?\b/i);
    if (everyDays) return normalizeNumber(everyDays[1]);
    if (/^(?:post|publish|schedule)\s+daily\b/i.test(line) || /^(?:daily)\b/i.test(line)) return 1;
    if (/^(?:post|publish|schedule)\s+weekly\b/i.test(line) || /^(?:weekly)\b/i.test(line)) return 7;
  }

  return normalizeNumber(inferredWindow?.cadenceDays);
}

function extractStartDateFromText(text, inferredWindow = null) {
  const labeled = parseDateToIso(extractLabeledText(text, ['start date', 'start', 'starting']));
  if (labeled) return labeled;
  const natural = text?.match(/\bstart(?:ing)?(?:\s+on)?\s*[:=-]?\s*([^\n.;]+)/i);
  const naturalDate = parseDateToIso(natural?.[1]);
  if (naturalDate) return naturalDate;
  return normalizeString(inferredWindow?.startDate);
}

function extractTimezoneFromText(text) {
  return extractLabeledText(text, ['timezone', 'time zone']);
}

function extractStatusFromText(text) {
  return extractLabeledText(text, ['status']);
}

function extractVariantCountFromText(text) {
  const labeled = normalizeVariantCount(extractLabeledText(text, ['variant count', 'variants', 'version count', 'versions', 'options', 'option count']));
  if (labeled) return labeled;

  const natural = text?.match(/\b(?:make|create|write|draft|generate|show|give)\s+(\d+)\s+(?:variants?|versions?|options?)\b/i)
    || text?.match(/\b(\d+)\s+(?:variants?|versions?|options?)\b/i);
  return normalizeVariantCount(natural?.[1]);
}

function extractSocialPostSummaryFromText(text) {
  const cleanCandidate = (value) => normalizeString(value)
    ?.replace(/^(?:about|on)\s+/i, '')
    .replace(/\s*(?:[.?!]\s*)?(?:with\s+)?(?:styles?|style|variants?|versions?|options?|status|schedule(?:d)?(?:\s+at)?|account ids|accounts|business|brand|website|logo)\b[\s\S]*$/i, '')
    .trim() || null;

  const labeled = extractLabeledText(text, ['summary', 'caption', 'post copy', 'copy', 'topic', 'idea']);
  if (labeled) return cleanCandidate(labeled);

  const quoted = text?.match(/\b(?:summary|caption|post copy|copy|topic|idea)\s*[:=-]\s*["“]([^"”]+)["”]/i);
  if (normalizeString(quoted?.[1])) return cleanCandidate(quoted[1]);

  const topical = text?.match(/\b(?:about|on)\s+([^\n.]+?)(?=(?:\s+(?:with|styles?|style|variants?|versions?|options?|status|schedule|scheduled|account ids|accounts|business|brand|website|logo)\b)|$)/i);
  if (normalizeString(topical?.[1])) return cleanCandidate(topical[1]);

  const direct = text?.match(/\b(?:create|make|write|draft|generate)\b(?:\s+\d+\s+(?:variants?|versions?|options?)\s+of)?\s+(?:a\s+)?(?:social\s+post|post|caption)\b\s*[:=-]?\s*(.+)$/i);
  if (normalizeString(direct?.[1])) return cleanCandidate(direct[1]);

  return null;
}

function parseSocialPostRequestText(value) {
  const text = normalizeTextBlock(value);
  if (!text) {
    return {
      sourceText: null,
      summary: null,
      businessName: null,
      accountIds: [],
      creativeStyles: [],
      status: null,
      scheduleDate: null,
      variantCount: null
    };
  }

  const stylesText = extractLabeledText(text, ['styles', 'style', 'creative styles', 'creative style', 'visual styles', 'visual style']);
  return {
    sourceText: text,
    summary: extractSocialPostSummaryFromText(text),
    businessName: extractLabeledText(text, ['business name', 'business', 'company', 'brand']),
    accountIds: parseInlineList(extractLabeledText(text, ['account ids', 'accounts', 'accountids'])),
    creativeStyles: stylesText ? parseStyleList(stylesText) : detectStyleListFromText(text),
    status: extractStatusFromText(text),
    scheduleDate: parseDateToIso(extractLabeledText(text, ['schedule date', 'scheduled at', 'schedule', 'date'])),
    variantCount: extractVariantCountFromText(text)
  };
}

function looksLikeSocialPostRequestText(text) {
  const source = normalizeTextBlock(text)?.toLowerCase() || '';
  if (!source) return false;
  if (looksLikeSocialCalendarRequestText(source)) return false;
  if (!extractSocialPostSummaryFromText(text) && !extractLabeledText(text, ['summary', 'caption', 'post copy', 'copy', 'topic', 'idea'])) return false;
  if (/\b(?:make|create|write|draft|generate)\b/.test(source) && /\b(?:social\s+post|facebook\s+post|instagram\s+post|linkedin\s+post|caption)\b/.test(source)) return true;
  if (/\b(?:variants?|versions?|options?)\b/.test(source) && /\b(?:social\s+post|post|caption)\b/.test(source)) return true;
  return false;
}

function extractKnowledgeBaseRefFromText(text) {
  return extractLabeledText(text, ['knowledge base', 'knowledgebase', 'kb']);
}

function extractBusinessNameFromText(text) {
  const labeled = extractLabeledText(text, ['business name', 'business', 'company', 'brand']);
  if (labeled) return labeled;
  const natural = text?.match(/\b(?:calendar|plan|posts?)\s+for\s+["“]?([^"”\n.,;]+?)["”]?(?=(?:,|\.|\n|\s+(?:industry|audience|offers?|services|goals?|knowledge|start|every|styles?|remove|edit|add)\b|$))/i);
  return normalizeString(natural?.[1]);
}

function extractIndustryFromText(text) {
  const labeled = extractLabeledText(text, ['industry', 'category', 'niche']);
  if (labeled) return labeled;
  const natural = text?.match(/\bfor\s+["“]?[^"”\n.,;]+["”]?,\s+(?:an?|the)\s+([^,\n.]+?)(?=(?:,|\.|\n|\s+(?:audience|offers?|services|goals?|knowledge|start|every|styles?)\b|$))/i);
  return normalizeString(natural?.[1]);
}

function extractScheduledTimeFromText(text) {
  const labeled = extractLabeledText(text, ['time', 'post time', 'schedule time']);
  const natural = labeled || text?.match(/\bat\s+(\d{1,2})(?::(\d{2}))?\s*(utc)?\b/i)?.[0] || null;
  const source = normalizeString(natural);
  if (!source) return { scheduledHourUtc: null, scheduledMinuteUtc: null };
  const match = source.match(/(\d{1,2})(?::(\d{2}))?/);
  const hour = normalizeNumber(match?.[1]);
  const minute = normalizeNumber(match?.[2] || 0);
  if (!Number.isInteger(hour) || hour < 0 || hour > 23) return { scheduledHourUtc: null, scheduledMinuteUtc: null };
  if (!Number.isInteger(minute) || minute < 0 || minute > 59) return { scheduledHourUtc: null, scheduledMinuteUtc: null };
  return { scheduledHourUtc: hour, scheduledMinuteUtc: minute };
}

function parseSocialCalendarRequestText(value) {
  const text = normalizeTextBlock(value);
  if (!text) {
    return {
      sourceText: null,
      accountIds: [],
      creativeStyles: [],
      status: null,
      business: {},
      calendar: {}
    };
  }

  const inferredWindow = inferCalendarWindowFromText(text);
  const stylesText = extractLabeledText(text, ['styles', 'style', 'creative styles', 'creative style', 'visual styles', 'visual style']);
  const scheduledTime = extractScheduledTimeFromText(text);
  const accountIds = parseInlineList(extractLabeledText(text, ['account ids', 'accounts', 'accountids']));

  return {
    sourceText: text,
    accountIds,
    creativeStyles: stylesText ? parseStyleList(stylesText) : detectStyleListFromText(text),
    variantCount: extractVariantCountFromText(text),
    status: extractStatusFromText(text),
    business: {
      name: extractBusinessNameFromText(text),
      industry: extractIndustryFromText(text),
      audience: parseInlineList(extractLabeledText(text, ['audience', 'target audience'])),
      offers: parseInlineList(extractLabeledText(text, ['offers', 'services', 'solutions'])),
      differentiators: parseInlineList(extractLabeledText(text, ['differentiators', 'strengths', 'advantages'])),
      goals: parseInlineList(extractLabeledText(text, ['goals', 'goal', 'outcomes'])),
      ctas: parseInlineList(extractLabeledText(text, ['ctas', 'calls to action', 'call to action'])),
      hashtags: parseInlineList(extractLabeledText(text, ['hashtags', 'tags'])),
      knowledgeBaseRef: extractKnowledgeBaseRefFromText(text)
    },
    calendar: {
      postCount: extractPostCountFromText(text, inferredWindow),
      cadenceDays: extractCadenceDaysFromText(text, inferredWindow),
      startDate: extractStartDateFromText(text, inferredWindow),
      timezone: extractTimezoneFromText(text),
      scheduledHourUtc: scheduledTime.scheduledHourUtc,
      scheduledMinuteUtc: scheduledTime.scheduledMinuteUtc
    }
  };
}

function socialCalendarMutationRequestFrom(event) {
  const request = event?.payload?.mutationRequest || event?.payload?.actionRequest || null;
  if (!request) return null;

  const business = normalizePlainObject(request.business || request.businessProfile || request.profile) || {};
  const calendar = normalizePlainObject(request.calendar || request.plan) || {};
  const review = normalizePlainObject(request.review || request.adjustments || request.reviewAdjustments) || {};
  const plainTextRequest = normalizeTextBlock(
    request.requestText
    || request.brief
    || request.description
    || request.text
    || request.instructions
    || request.prompt
    || request.message
  );
  const parsedRequest = parseSocialCalendarRequestText(plainTextRequest);
  const requestedBusinessName = normalizeString(request.businessName)
    || normalizeString(business.name)
    || normalizeString(business.businessName)
    || normalizeString(parsedRequest.business.name)
    || null;
  const activeBusiness = activeBusinessDefaultsFromRecord(readActiveBusinessRecord(process.cwd()));
  const useActiveBusiness = shouldUseActiveBusinessDefaults(requestedBusinessName, activeBusiness);
  const activeBusinessDefaults = useActiveBusiness ? activeBusiness : null;
  const eventLocationId = normalizeString(event?.locationId) || normalizeString(event?.payload?.locationId) || null;
  const preferredEventLocationId = eventLocationId && eventLocationId !== 'demo-location' ? eventLocationId : null;
  const reviewInstructionText = normalizeTextBlock(
    request.reviewText
    || request.reviewNotes
    || request.reviewPrompt
    || review.instructions
    || review.text
    || review.notes
    || review.prompt
    || review.message
    || parsedRequest.sourceText
  );
  const parsedReview = parseSocialCalendarReviewInstructions(reviewInstructionText);
  const structuredReplacePosts = Array.isArray(review.replacePosts)
    ? review.replacePosts.filter((item) => item && typeof item === 'object' && !Array.isArray(item)).map((item) => ({ ...item }))
    : [];
  const structuredAppendPosts = Array.isArray(review.appendPosts)
    ? review.appendPosts.filter((item) => item && typeof item === 'object' && !Array.isArray(item)).map((item) => ({ ...item }))
    : [];
  const structuredPostEdits = Array.isArray(review.postEdits)
    ? review.postEdits.filter((item) => item && typeof item === 'object' && !Array.isArray(item)).map((item) => ({ ...item }))
    : [];

  return {
    action: request.action || null,
    sourceText: parsedRequest.sourceText,
    locationId: request.locationId || preferredEventLocationId || activeBusinessDefaults?.locationId || eventLocationId || null,
    accountIds: normalizeStringArray(request.accountIds || calendar.accountIds || parsedRequest.accountIds),
    status: normalizeString(request.status) || normalizeString(calendar.status) || normalizeString(parsedRequest.status) || 'draft',
    creativeStyles: normalizeStringArray(request.creativeStyles || calendar.creativeStyles || parsedRequest.creativeStyles),
    variantCount: normalizeVariantCount(request.variantCount || request.variants || calendar.variantCount || calendar.variants || parsedRequest.variantCount, 4),
    continueOnError: normalizeBoolean(request.continueOnError),
    postDefaults: normalizePlainObject(request.postDefaults || calendar.postDefaults) || {},
    business: {
      name: requestedBusinessName || normalizeString(activeBusinessDefaults?.name) || null,
      industry: normalizeString(request.industry)
        || normalizeString(business.industry)
        || normalizeString(business.category)
        || normalizeString(parsedRequest.business.industry)
        || normalizeString(activeBusinessDefaults?.industry)
        || null,
      audience: normalizeStringArray(request.audience || business.audience || business.audiences || parsedRequest.business.audience || activeBusinessDefaults?.audience),
      offers: normalizeStringArray(request.offers || request.services || business.offers || business.services || business.solutions || parsedRequest.business.offers || activeBusinessDefaults?.offers),
      differentiators: normalizeStringArray(request.differentiators || business.differentiators || business.strengths || business.advantages || parsedRequest.business.differentiators || activeBusinessDefaults?.differentiators),
      goals: normalizeStringArray(request.goals || business.goals || business.outcomes || parsedRequest.business.goals || activeBusinessDefaults?.goals),
      ctas: normalizeStringArray(request.ctas || business.ctas || business.callsToAction || parsedRequest.business.ctas || activeBusinessDefaults?.ctas),
      hashtags: normalizeStringArray(request.hashtags || business.hashtags || parsedRequest.business.hashtags || activeBusinessDefaults?.hashtags),
      knowledgeBaseRef: normalizeString(request.knowledgeBaseRef)
        || normalizeString(request.knowledgeBaseId)
        || normalizeString(request.knowledgeBase)
        || normalizeString(business.knowledgeBaseRef)
        || normalizeString(business.knowledgeBaseId)
        || normalizeString(business.knowledgeBase)
        || normalizeString(parsedRequest.business.knowledgeBaseRef)
        || normalizeString(activeBusinessDefaults?.knowledgeBaseRef)
        || null,
      brandVoice: normalizeString(request.brandVoice)
        || normalizeString(request.voice)
        || normalizeString(business.brandVoice)
        || normalizeString(business.voice)
        || normalizeString(activeBusinessDefaults?.brandVoice)
        || null,
      websiteUrl: normalizeString(request.websiteUrl)
        || normalizeString(request.website)
        || normalizeString(business.websiteUrl)
        || normalizeString(business.website)
        || normalizeString(activeBusinessDefaults?.websiteUrl)
        || null,
      logoUrl: normalizeString(request.logoUrl)
        || normalizeString(request.logo)
        || normalizeString(business.logoUrl)
        || normalizeString(business.logo)
        || normalizeString(activeBusinessDefaults?.logoUrl)
        || null
    },
    calendar: {
      postCount: normalizeNumber(calendar.postCount || request.postCount || parsedRequest.calendar.postCount),
      cadenceDays: normalizeNumber(calendar.cadenceDays || request.cadenceDays || parsedRequest.calendar.cadenceDays),
      startDate: normalizeString(calendar.startDate) || normalizeString(request.startDate) || normalizeString(parsedRequest.calendar.startDate) || null,
      timezone: normalizeString(calendar.timezone) || normalizeString(request.timezone) || normalizeString(parsedRequest.calendar.timezone) || null,
      scheduledHourUtc: normalizeNumber(calendar.scheduledHourUtc || request.scheduledHourUtc || parsedRequest.calendar.scheduledHourUtc),
      scheduledMinuteUtc: normalizeNumber(calendar.scheduledMinuteUtc || request.scheduledMinuteUtc || parsedRequest.calendar.scheduledMinuteUtc)
    },
    review: {
      removeDays: uniqueIntegers([
        ...normalizeIntegerArray(review.removeDays || review.removeIndexes || review.removePosts),
        ...parsedReview.removeDays
      ]),
      replacePosts: structuredReplacePosts.length > 0 ? structuredReplacePosts : parsedReview.replacePosts,
      appendPosts: mergePostInstructions(structuredAppendPosts, parsedReview.appendPosts),
      postEdits: mergePostInstructions(structuredPostEdits, parsedReview.postEdits)
    }
  };
}

function isBulkSocialPostAction(action) {
  return ['create_social_posts_bulk', 'bulk_create_social_posts', 'create_social_post_batch'].includes(action);
}

function isGenerateSocialCalendarAction(action) {
  return ['generate_social_calendar', 'plan_social_calendar', 'create_social_calendar_plan'].includes(action);
}

function isRunSocialCalendarFlowAction(action) {
  return ['run_social_calendar_flow', 'create_social_calendar_flow', 'plan_review_create_social_posts'].includes(action);
}

function hasSocialCalendarReviewAdjustments(request) {
  const review = request?.review || {};
  return (Array.isArray(review.removeDays) && review.removeDays.length > 0)
    || (Array.isArray(review.replacePosts) && review.replacePosts.length > 0)
    || (Array.isArray(review.appendPosts) && review.appendPosts.length > 0)
    || (Array.isArray(review.postEdits) && review.postEdits.length > 0);
}

function looksLikeSocialCalendarRequestText(text) {
  const source = normalizeTextBlock(text)?.toLowerCase() || '';
  if (!source) return false;
  if (/\bsocial calendar\b|\bcontent calendar\b/.test(source)) return true;
  if (/\bcalendar\b/.test(source) && /\bpost(?:s)?\b/.test(source)) return true;
  if (/\bcreate\s+(?:a\s+)?\d+\s+post(?:s)?\b/.test(source)) return true;
  if (/\bplan\s+(?:a\s+)?\d+\s+post(?:s)?\b/.test(source)) return true;
  if (/\b(?:this|next)\s+(?:week|month)(?:['’]s?)?\s+posts?\b|\bposts?\s+for\s+(?:this|next)\s+(?:week|month)\b/.test(source)) return true;
  if (/\bnext\s+\d{1,3}\s+days'?\s+posts?\b|\bposts?\s+for\s+the\s+next\s+\d{1,3}\s+days\b/.test(source)) return true;
  if (/\bremove\s+day\b|\bedit\s+day\b|\badd\s+post\b|\breplace\s+calendar\b/.test(source)) return true;
  return false;
}

function inferRunSocialCalendarFlowRequest(request) {
  if (!request || request.action) return false;
  const hasPlanningFields = Boolean(request?.business?.name)
    || Boolean(request?.business?.industry)
    || Boolean(request?.business?.knowledgeBaseRef)
    || Number.isFinite(normalizeNumber(request?.calendar?.postCount))
    || Number.isFinite(normalizeNumber(request?.calendar?.cadenceDays))
    || Boolean(request?.calendar?.startDate)
    || (Array.isArray(request?.creativeStyles) && request.creativeStyles.length > 0);
  const hasReviewAdjustments = hasSocialCalendarReviewAdjustments(request);
  const hasTextIntent = looksLikeSocialCalendarRequestText(request?.sourceText);
  return hasTextIntent && (hasPlanningFields || hasReviewAdjustments);
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
    sourcePostId: normalizeString(request.sourcePostId) || normalizeString(request.socialPostId) || normalizeString(request.postId),
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

function socialPostFetchResumeData(liveResult) {
  const data = liveResult?.data || null;
  const post = data?.results?.post || data?.post || null;
  return {
    post: post && typeof post === 'object'
      ? {
          id: post._id || post.id || post.postId || null,
          locationId: post.locationId || null,
          status: post.status || null,
          summary: post.summary || post.text || null,
          media: Array.isArray(post.media) ? post.media : [],
          accountIds: Array.isArray(post.accountIds) ? post.accountIds : [],
          raw: post
        }
      : null,
    raw: data
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

function socialPostRequestedVariantCount(mutationRequest) {
  return normalizeVariantCount(
    mutationRequest?.variantCount
    || (Array.isArray(mutationRequest?.creativeStyles) && mutationRequest.creativeStyles.length > 0 ? mutationRequest.creativeStyles.length : null)
    || 1,
    4
  ) || 1;
}

function shouldGenerateSocialPostVariants(mutationRequest) {
  const hasExistingMedia = Array.isArray(mutationRequest?.post?.media) && mutationRequest.post.media.length > 0;
  if (hasExistingMedia) return false;
  if (socialPostRequestedVariantCount(mutationRequest) > 1) return true;
  return Array.isArray(mutationRequest?.creativeStyles) && mutationRequest.creativeStyles.length > 1;
}

function socialPostVariantSetFromContext(context) {
  return normalizePlainObject(context?.runtime?.stepOutputs?.generate_social_post_variants?.data?.openClaw?.creative)
    || normalizePlainObject(context?.runtime?.stepOutputs?.generate_social_post_variants?.data?.creative)
    || null;
}

function parseSocialPostVariantSelection(note, variants = []) {
  const list = Array.isArray(variants) ? variants : [];
  if (list.length === 0) return { index: 0, variant: null, via: 'none' };

  const source = normalizeString(note)?.toLowerCase();
  if (!source) return { index: 0, variant: list[0], via: 'default' };

  const indexedMatch = source.match(/\b(?:variant|option|style)\s*(\d+)\b/) || source.match(/^\s*(\d+)\s*$/);
  const indexed = normalizeNumber(indexedMatch?.[1]);
  if (Number.isInteger(indexed) && indexed >= 1 && indexed <= list.length) {
    return { index: indexed - 1, variant: list[indexed - 1], via: 'index' };
  }

  const styleIndex = list.findIndex((variant) => {
    const styleId = normalizeString(variant?.model?.styleId || variant?.styleId)?.toLowerCase();
    const styleLabel = normalizeString(variant?.model?.styleLabel || variant?.styleLabel)?.toLowerCase();
    return (styleId && source.includes(styleId)) || (styleLabel && source.includes(styleLabel));
  });
  if (styleIndex >= 0) {
    return { index: styleIndex, variant: list[styleIndex], via: 'style' };
  }

  return { index: 0, variant: list[0], via: 'default' };
}

function selectedSocialPostVariantFromContext(context) {
  const creative = socialPostVariantSetFromContext(context);
  const variants = Array.isArray(creative?.variants) ? creative.variants : [];
  const approvalNote = context?.runtime?.approvals?.create_social_post?.note || null;
  const selection = parseSocialPostVariantSelection(approvalNote, variants);
  return {
    ...selection,
    note: approvalNote,
    variants
  };
}

function socialPostVariantGenerateResumeData(liveResult, context) {
  const data = liveResult?.data || null;
  const mutationRequest = socialPostMutationRequestFrom(context?.event);
  const creative = normalizePlainObject(data?.openClaw?.creative) || {};
  const variants = Array.isArray(creative.variants) ? creative.variants : [];

  return {
    requested: {
      locationId: mutationRequest?.locationId || null,
      summary: mutationRequest?.summary || null,
      businessName: mutationRequest?.businessName || null,
      creativeStyles: mutationRequest?.creativeStyles || [],
      variantCount: socialPostRequestedVariantCount(mutationRequest)
    },
    creative: {
      variantCount: variants.length,
      variantStyles: variants.map((variant) => variant?.model?.styleId || variant?.styleId || null).filter(Boolean),
      variants: variants.map((variant, index) => ({
        index: index + 1,
        styleId: variant?.model?.styleId || null,
        styleLabel: variant?.model?.styleLabel || null,
        headline: variant?.model?.headline || null,
        pngPath: variant?.pngPath || null
      }))
    },
    raw: data
  };
}

function socialPostCreateDetails(context, mutationRequest) {
  const variantSet = socialPostVariantSetFromContext(context);
  const variants = Array.isArray(variantSet?.variants) ? variantSet.variants : [];
  const selection = selectedSocialPostVariantFromContext(context);

  return {
    action: 'create_social_post',
    locationId: mutationRequest?.locationId || null,
    accountIds: mutationRequest?.accountIds || [],
    status: mutationRequest?.status || null,
    scheduleDate: mutationRequest?.scheduleDate || null,
    businessName: mutationRequest?.businessName || null,
    summaryPreview: mutationRequest?.summary ? mutationRequest.summary.slice(0, 160) : null,
    autoGenerateCreative: !Array.isArray(mutationRequest?.post?.media) || mutationRequest.post.media.length === 0,
    creativeStyles: mutationRequest?.creativeStyles || [],
    variantCount: socialPostRequestedVariantCount(mutationRequest),
    selectedVariantIndex: selection?.note && selection?.variant ? selection.index + 1 : null,
    selectedVariantStyle: selection?.note ? (selection?.variant?.model?.styleId || selection?.variant?.styleId || null) : null,
    selectedVia: selection?.note ? selection?.via || null : null,
    variantOptions: variants.map((variant, index) => `${index + 1}. ${variant?.model?.styleLabel || variant?.styleLabel || variant?.model?.styleId || variant?.styleId || 'variant'}${(variant?.pngPath || null) ? ` (${variant.pngPath})` : ''}`),
    selectionHint: variants.length > 1
      ? 'Approve with a note like "variant 2" or a style name such as "editorial" to choose the creative.'
      : null
  };
}

function socialPostCreateResumeData(liveResult, context) {
  const data = liveResult?.data || null;
  const rawPost = data?.results?.post || data?.post || null;
  const mutationRequest = socialPostMutationRequestFrom(context?.event);
  const creative = normalizePlainObject(data?.openClaw?.creative) || {};
  const variants = Array.isArray(creative.variants) ? creative.variants : [];
  const selection = selectedSocialPostVariantFromContext(context);
  return {
    post: (rawPost && typeof rawPost === 'object') || (data && typeof data === 'object')
      ? {
          id: rawPost?._id || rawPost?.id || rawPost?.postId || data?.id || data?.postId || null,
          status: rawPost?.status || data?.status || mutationRequest?.status || null,
          scheduleDate: rawPost?.scheduleDate || rawPost?.scheduledAt || data?.scheduleDate || data?.scheduledAt || mutationRequest?.scheduleDate || null,
          summary: rawPost?.summary || rawPost?.text || data?.summary || data?.text || mutationRequest?.summary || null,
          media: Array.isArray(rawPost?.media) ? rawPost.media : Array.isArray(data?.media) ? data.media : []
        }
      : null,
    requested: {
      locationId: mutationRequest?.locationId || null,
      accountIds: mutationRequest?.accountIds || [],
      status: mutationRequest?.status || null,
      scheduleDate: mutationRequest?.scheduleDate || null,
      summary: mutationRequest?.summary || null,
      creativeStyles: mutationRequest?.creativeStyles || [],
      variantCount: mutationRequest?.variantCount || null
    },
    creative: {
      uploadedMedia: Array.isArray(creative.media) ? creative.media : [],
      variantCount: variants.length,
      variantStyles: variants.map((variant) => variant?.model?.styleId || variant?.styleId || null).filter(Boolean),
      selectedVariantIndex: selection?.variant ? selection.index + 1 : null,
      selectedVariantStyle: selection?.variant?.model?.styleId || selection?.variant?.styleId || null,
      selectionNote: selection?.note || null
    },
    raw: data
  };
}

function socialCalendarGenerateResumeData(liveResult, context) {
  const data = liveResult?.data || null;
  const mutationRequest = socialCalendarMutationRequestFrom(context?.event);
  return {
    calendar: data && typeof data === 'object'
      ? {
          generatedAt: data.generatedAt || null,
          postCount: Array.isArray(data.posts) ? data.posts.length : 0,
          businessName: data?.business?.name || mutationRequest?.business?.name || null,
          industry: data?.business?.industry || mutationRequest?.business?.industry || null,
          knowledgeBaseMode: data?.knowledgeBaseMode || null,
          creativeStyles: Array.isArray(data?.calendar?.creativeStyles) ? data.calendar.creativeStyles : mutationRequest?.creativeStyles || [],
          firstScheduleDate: Array.isArray(data?.posts) && data.posts[0] ? data.posts[0].scheduleDate || null : null,
          lastScheduleDate: Array.isArray(data?.posts) && data.posts.length > 0 ? data.posts[data.posts.length - 1].scheduleDate || null : null
        }
      : null,
    raw: data
  };
}

function socialCalendarFlowCreateResumeData(liveResult, context) {
  const data = liveResult?.data || null;
  const mutationRequest = socialCalendarMutationRequestFrom(context?.event);
  const reviewed = socialCalendarFlowReviewedPosts(context, mutationRequest);
  const items = Array.isArray(data?.results?.items)
    ? data.results.items
    : Array.isArray(data?.items)
      ? data.items
      : [];

  return {
    posts: items.map((item) => {
      const rawPost = item?.data?.results?.post || item?.data?.post || null;
      return {
        index: item?.index ?? null,
        ok: item?.ok !== false,
        id: rawPost?._id || rawPost?.id || rawPost?.postId || item?.data?.id || item?.data?.postId || null,
        status: rawPost?.status || item?.data?.status || null,
        scheduleDate: rawPost?.scheduleDate || rawPost?.scheduledAt || item?.data?.scheduleDate || item?.data?.scheduledAt || null,
        summary: rawPost?.summary || rawPost?.text || item?.data?.summary || item?.data?.text || null,
        error: item?.error || null
      };
    }),
    requested: {
      locationId: mutationRequest?.locationId || null,
      businessName: mutationRequest?.business?.name || null,
      generatedCount: reviewed.generatedCount,
      finalCount: reviewed.finalCount,
      removedDays: reviewed.removedDays,
      editedCount: reviewed.editedCount,
      appendedCount: reviewed.appendedCount,
      replaced: reviewed.replaced,
      creativeStyles: mutationRequest?.creativeStyles || [],
      variantCount: mutationRequest?.variantCount || null
    },
    raw: data
  };
}

function socialPostBulkCreateResumeData(liveResult, context) {
  const data = liveResult?.data || null;
  const mutationRequest = socialPostMutationRequestFrom(context?.event);
  const items = Array.isArray(data?.results?.items)
    ? data.results.items
    : Array.isArray(data?.items)
      ? data.items
      : [];

  return {
    posts: items.map((item) => {
      const rawPost = item?.data?.results?.post || item?.data?.post || null;
      const creative = normalizePlainObject(item?.data?.openClaw?.creative) || {};
      const variants = Array.isArray(creative.variants) ? creative.variants : [];
      return {
        index: item?.index ?? null,
        ok: item?.ok !== false,
        id: rawPost?._id || rawPost?.id || rawPost?.postId || item?.data?.id || item?.data?.postId || null,
        status: rawPost?.status || item?.data?.status || null,
        scheduleDate: rawPost?.scheduleDate || rawPost?.scheduledAt || item?.data?.scheduleDate || item?.data?.scheduledAt || null,
        summary: rawPost?.summary || rawPost?.text || item?.data?.summary || item?.data?.text || null,
        creativeVariantCount: variants.length,
        creativeVariantStyles: variants.map((variant) => variant?.model?.styleId || null).filter(Boolean),
        error: item?.error || null
      };
    }),
    requested: {
      locationId: mutationRequest?.locationId || null,
      count: Array.isArray(mutationRequest?.posts) ? mutationRequest.posts.length : 0,
      accountIds: mutationRequest?.accountIds || [],
      status: mutationRequest?.status || null,
      businessName: mutationRequest?.businessName || null,
      creativeStyles: mutationRequest?.creativeStyles || [],
      variantCount: mutationRequest?.variantCount || null
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
  const createStep = context?.runtime?.stepOutputs?.create_social_post?.data;
  const createdPost = createStep?.results?.post || createStep?.post || null;
  const openClawRequestBody = createStep?.openClaw?.requestBody || null;
  const requestedPost = mutationRequest?.post && typeof mutationRequest.post === 'object' && !Array.isArray(mutationRequest.post)
    ? mutationRequest.post
    : {};

  return {
    ...requestedPost,
    ...(openClawRequestBody && typeof openClawRequestBody === 'object' && !Array.isArray(openClawRequestBody) ? openClawRequestBody : {}),
    ...(createdPost && typeof createdPost === 'object' && !Array.isArray(createdPost) ? createdPost : {}),
    ...(mutationRequest?.accountIds?.length > 0 ? { accountIds: mutationRequest.accountIds } : {}),
    ...(mutationRequest?.status ? { status: mutationRequest.status } : {}),
    ...(mutationRequest?.scheduleDate ? { scheduleDate: mutationRequest.scheduleDate } : {}),
    ...(mutationRequest?.summary ? { summary: mutationRequest.summary } : {})
  };
}

function resolvedSocialPostId(context) {
  const createdPost = context?.runtime?.stepOutputs?.create_social_post?.data;
  return createdPost?.results?.post?._id
    || createdPost?.results?.post?.id
    || createdPost?.results?.post?.postId
    || createdPost?.post?._id
    || createdPost?.post?.id
    || createdPost?.post?.postId
    || createdPost?.id
    || createdPost?.postId
    || null;
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

function socialPostBulkCreateBodies(mutationRequest) {
  const posts = Array.isArray(mutationRequest?.posts) ? mutationRequest.posts : [];
  if (posts.length === 0) {
    throw new Error('create_social_posts_bulk requires a non-empty posts array.');
  }

  const defaults = mutationRequest?.postDefaults && typeof mutationRequest.postDefaults === 'object' && !Array.isArray(mutationRequest.postDefaults)
    ? mutationRequest.postDefaults
    : mutationRequest?.post && typeof mutationRequest.post === 'object' && !Array.isArray(mutationRequest.post)
      ? mutationRequest.post
      : {};

  return posts.map((post) => {
    const mergedPost = {
      ...defaults,
      ...post
    };
    const body = socialPostCreateBody({
      post: mergedPost,
      accountIds: Array.isArray(post?.accountIds) && post.accountIds.length > 0
        ? post.accountIds
        : Array.isArray(defaults?.accountIds) && defaults.accountIds.length > 0
          ? defaults.accountIds
          : mutationRequest?.accountIds,
      status: normalizeString(post?.status) || normalizeString(defaults?.status) || mutationRequest?.status,
      scheduleDate: normalizeString(post?.scheduleDate)
        || normalizeString(post?.scheduledAt)
        || normalizeString(defaults?.scheduleDate)
        || normalizeString(defaults?.scheduledAt)
        || mutationRequest?.scheduleDate,
      summary: normalizeString(post?.summary) || normalizeString(post?.text) || null
    });

      const businessName = normalizeString(post?.businessName)
      || normalizeString(defaults?.businessName)
      || mutationRequest?.businessName;
    const brandVoice = normalizeString(post?.brandVoice)
      || normalizeString(post?.voice)
      || normalizeString(defaults?.brandVoice)
      || normalizeString(defaults?.voice)
      || normalizeString(mutationRequest?.brandVoice);
    const websiteUrl = normalizeString(post?.websiteUrl)
      || normalizeString(post?.website)
      || normalizeString(defaults?.websiteUrl)
      || normalizeString(defaults?.website)
      || normalizeString(mutationRequest?.websiteUrl);
    const logoUrl = normalizeString(post?.logoUrl)
      || normalizeString(post?.logo)
      || normalizeString(defaults?.logoUrl)
      || normalizeString(defaults?.logo)
      || normalizeString(mutationRequest?.logoUrl);
    const variantCount = normalizeVariantCount(
      post?.variantCount
      || post?.variants
      || defaults?.variantCount
      || defaults?.variants
      || mutationRequest?.variantCount,
      4
    );
    const creativeStyle = normalizeString(post?.creativeStyle)
      || normalizeString(post?.visualStyle)
      || normalizeString(post?.style)
      || normalizeString(defaults?.creativeStyle)
      || normalizeString(defaults?.visualStyle)
      || normalizeString(defaults?.style)
      || mutationRequest?.creativeStyle;
    const creativeStyles = normalizeStringArray(
      post?.creativeStyles
      || defaults?.creativeStyles
      || mutationRequest?.creativeStyles
    );

    return {
      ...body,
      ...(businessName ? { businessName } : {}),
      ...(brandVoice ? { brandVoice } : {}),
      ...(websiteUrl ? { websiteUrl } : {}),
      ...(logoUrl ? { logoUrl } : {}),
      ...(variantCount ? { variantCount } : {}),
      ...(creativeStyle ? { creativeStyle } : {}),
      ...(creativeStyles.length > 0 ? { creativeStyles } : {})
    };
  });
}

function socialCalendarPlanFromContext(context) {
  return context?.runtime?.stepOutputs?.generate_social_calendar?.data || null;
}

function sanitizeSocialCalendarFlowPost(post, fallbackDay = null) {
  const value = normalizePlainObject(post) || {};
  const summary = normalizeString(value.summary) || normalizeString(value.text);
  const scheduleDate = normalizeString(value.scheduleDate) || normalizeString(value.scheduledAt);
  const status = normalizeString(value.status);
  const businessName = normalizeString(value.businessName);
  const brandVoice = normalizeString(value.brandVoice) || normalizeString(value.voice);
  const websiteUrl = normalizeString(value.websiteUrl) || normalizeString(value.website);
  const logoUrl = normalizeString(value.logoUrl) || normalizeString(value.logo);
  const variantCount = normalizeVariantCount(value.variantCount || value.variants, 4);
  const creativeStyle = normalizeString(value.creativeStyle) || normalizeString(value.visualStyle) || normalizeString(value.style);
  const creativeStyles = normalizeStringArray(value.creativeStyles);
  const accountIds = normalizeStringArray(value.accountIds);
  const day = normalizeNumber(value.day) || fallbackDay;

  return {
    ...(day ? { day } : {}),
    ...(businessName ? { businessName } : {}),
    ...(summary ? { summary } : {}),
    ...(scheduleDate ? { scheduleDate } : {}),
    ...(status ? { status } : {}),
    ...(brandVoice ? { brandVoice } : {}),
    ...(websiteUrl ? { websiteUrl } : {}),
    ...(logoUrl ? { logoUrl } : {}),
    ...(variantCount ? { variantCount } : {}),
    ...(accountIds.length > 0 ? { accountIds } : {}),
    ...(creativeStyle ? { creativeStyle } : {}),
    ...(creativeStyles.length > 0 ? { creativeStyles } : {})
  };
}

function socialCalendarFlowBasePosts(plan, mutationRequest) {
  const planPosts = Array.isArray(plan?.posts) ? plan.posts : [];
  const mutationPosts = Array.isArray(plan?.mutationRequest?.posts) ? plan.mutationRequest.posts : [];
  const max = Math.max(planPosts.length, mutationPosts.length);

  return Array.from({ length: max }, (_, index) => {
    const planPost = normalizePlainObject(planPosts[index]) || {};
    const mutationPost = normalizePlainObject(mutationPosts[index]) || {};
    const day = normalizeNumber(planPost.day) || index + 1;
    return sanitizeSocialCalendarFlowPost({
      day,
      businessName: mutationPost.businessName || planPost.businessName || mutationRequest?.business?.name || null,
      summary: mutationPost.summary || planPost.summary || null,
      scheduleDate: mutationPost.scheduleDate || planPost.scheduleDate || null,
      status: mutationPost.status || plan?.calendar?.status || mutationRequest?.status || 'draft',
      brandVoice: mutationPost.brandVoice || planPost.brandVoice || mutationRequest?.business?.brandVoice || null,
      websiteUrl: mutationPost.websiteUrl || planPost.websiteUrl || mutationRequest?.business?.websiteUrl || null,
      logoUrl: mutationPost.logoUrl || planPost.logoUrl || mutationRequest?.business?.logoUrl || null,
      variantCount: mutationPost.variantCount || planPost.variantCount || mutationRequest?.variantCount || null,
      accountIds: mutationPost.accountIds || plan?.calendar?.accountIds || mutationRequest?.accountIds || [],
      creativeStyle: mutationPost.creativeStyle || planPost.creativeStyle || null,
      creativeStyles: mutationPost.creativeStyles || planPost.creativeStyles || mutationRequest?.creativeStyles || []
    }, day);
  }).filter((item) => item.summary || item.scheduleDate);
}

function socialCalendarIsoAtUtc(startDate, dayOffset = 0, scheduledHourUtc = 16, scheduledMinuteUtc = 0, cadenceDays = 1) {
  const base = normalizeString(startDate) ? new Date(startDate) : new Date();
  const safeBase = Number.isNaN(base.getTime()) ? new Date() : base;
  const scheduled = new Date(Date.UTC(
    safeBase.getUTCFullYear(),
    safeBase.getUTCMonth(),
    safeBase.getUTCDate() + (dayOffset * cadenceDays),
    scheduledHourUtc,
    scheduledMinuteUtc,
    0,
    0
  ));
  return scheduled.toISOString();
}

function socialCalendarScheduleDateForDay(plan, day) {
  const normalizedDay = normalizeNumber(day);
  if (!normalizedDay) return null;

  const planPosts = Array.isArray(plan?.posts) ? plan.posts : [];
  const direct = planPosts.find((post) => Number(post?.day) === Number(normalizedDay));
  if (normalizeString(direct?.scheduleDate)) return normalizeString(direct.scheduleDate);

  const calendar = normalizePlainObject(plan?.calendar) || {};
  const startDate = normalizeString(calendar.startDate);
  if (!startDate) return null;

  return socialCalendarIsoAtUtc(
    startDate,
    Math.max(0, normalizedDay - 1),
    normalizeNumber(calendar.scheduledHourUtc) ?? 16,
    normalizeNumber(calendar.scheduledMinuteUtc) ?? 0,
    normalizeNumber(calendar.cadenceDays) ?? 1
  );
}

function applyMissingScheduleDatesToFlowPosts(plan, posts = []) {
  return posts.map((post, index) => {
    const normalized = sanitizeSocialCalendarFlowPost(post, index + 1);
    if (normalized.scheduleDate) return normalized;
    const day = normalizeNumber(normalized.day) || index + 1;
    const inferredScheduleDate = socialCalendarScheduleDateForDay(plan, day);
    return inferredScheduleDate ? { ...normalized, scheduleDate: inferredScheduleDate } : normalized;
  });
}

function resolveSocialCalendarFlowPostTargetIndex(posts, edit) {
  const day = normalizeNumber(edit?.day) || normalizeNumber(edit?.index);
  if (day) {
    const byDay = posts.findIndex((post) => Number(post?.day) === Number(day));
    if (byDay >= 0) return byDay;
    const byIndex = day - 1;
    if (byIndex >= 0 && byIndex < posts.length) return byIndex;
  }

  const scheduleDate = normalizeString(edit?.scheduleDate);
  if (scheduleDate) {
    const byScheduleDate = posts.findIndex((post) => normalizeString(post?.scheduleDate) === scheduleDate);
    if (byScheduleDate >= 0) return byScheduleDate;
  }

  const summaryContains = normalizeString(edit?.summaryContains);
  if (summaryContains) {
    const needle = summaryContains.toLowerCase();
    const bySummary = posts.findIndex((post) => normalizeString(post?.summary)?.toLowerCase().includes(needle));
    if (bySummary >= 0) return bySummary;
  }

  return -1;
}

function socialCalendarFlowReviewedPosts(context, mutationRequest) {
  const plan = socialCalendarPlanFromContext(context);
  if (!plan?.ready || !plan?.mutationRequest) {
    return {
      ready: false,
      reason: 'calendar_not_ready',
      posts: [],
      generatedCount: Array.isArray(plan?.posts) ? plan.posts.length : 0,
      finalCount: 0,
      removedDays: [],
      editedCount: 0,
      appendedCount: 0,
      replaced: false,
      firstScheduleDate: null,
      lastScheduleDate: null
    };
  }

  const review = mutationRequest?.review || {};
  const reviewReplacePosts = Array.isArray(review.replacePosts) ? review.replacePosts : [];
  const reviewAppendPosts = Array.isArray(review.appendPosts) ? review.appendPosts : [];
  const reviewPostEdits = Array.isArray(review.postEdits) ? review.postEdits : [];
  const reviewRemoveDays = Array.isArray(review.removeDays) ? review.removeDays : [];

  let posts = reviewReplacePosts.length > 0
    ? applyMissingScheduleDatesToFlowPosts(plan, reviewReplacePosts.map((post, index) => sanitizeSocialCalendarFlowPost(post, index + 1)).filter((item) => item.summary || item.scheduleDate))
    : socialCalendarFlowBasePosts(plan, mutationRequest);

  if (reviewRemoveDays.length > 0) {
    const removeSet = new Set(reviewRemoveDays.map((value) => Number(value)));
    posts = posts.filter((post) => !removeSet.has(Number(post?.day)));
  }

  let editedCount = 0;
  for (const edit of reviewPostEdits) {
    const targetIndex = resolveSocialCalendarFlowPostTargetIndex(posts, edit);
    if (targetIndex < 0) continue;
    const next = sanitizeSocialCalendarFlowPost({
      ...posts[targetIndex],
      ...edit,
      day: posts[targetIndex].day
    }, posts[targetIndex].day);
    posts[targetIndex] = next;
    editedCount += 1;
  }

  const nextAppendDay = posts.reduce((max, post) => Math.max(max, normalizeNumber(post?.day) || 0), 0);
  const appendedPosts = applyMissingScheduleDatesToFlowPosts(plan, reviewAppendPosts
    .map((post, index) => sanitizeSocialCalendarFlowPost(post, nextAppendDay + index + 1))
    .filter((item) => item.summary || item.scheduleDate));
  posts = applyMissingScheduleDatesToFlowPosts(plan, [...posts, ...appendedPosts]);

  return {
    ready: true,
    posts,
    generatedCount: Array.isArray(plan?.posts) ? plan.posts.length : 0,
    finalCount: posts.length,
    removedDays: reviewRemoveDays,
    editedCount,
    appendedCount: appendedPosts.length,
    replaced: reviewReplacePosts.length > 0,
    firstScheduleDate: posts[0]?.scheduleDate || null,
    lastScheduleDate: posts.length > 0 ? posts[posts.length - 1]?.scheduleDate || null : null
  };
}

function socialCalendarFlowCreateBodies(context, mutationRequest) {
  const reviewed = socialCalendarFlowReviewedPosts(context, mutationRequest);
  if (!reviewed.ready) {
    throw new Error('Social calendar flow cannot create posts until a ready calendar plan exists.');
  }

  return socialPostBulkCreateBodies({
    locationId: mutationRequest?.locationId || null,
    accountIds: mutationRequest?.accountIds || [],
    status: mutationRequest?.status || 'draft',
    businessName: mutationRequest?.business?.name || null,
    brandVoice: mutationRequest?.business?.brandVoice || null,
    websiteUrl: mutationRequest?.business?.websiteUrl || null,
    logoUrl: mutationRequest?.business?.logoUrl || null,
    variantCount: mutationRequest?.variantCount || null,
    creativeStyles: mutationRequest?.creativeStyles || [],
    postDefaults: mutationRequest?.postDefaults || {},
    posts: reviewed.posts.map((post) => {
      const body = {
        ...post,
        accountIds: Array.isArray(post.accountIds) && post.accountIds.length > 0 ? post.accountIds : mutationRequest?.accountIds || []
      };
      delete body.day;
      return body;
    })
  });
}

function socialCalendarFlowReviewDetails(context, mutationRequest) {
  const plan = socialCalendarPlanFromContext(context);
  const reviewed = socialCalendarFlowReviewedPosts(context, mutationRequest);
  return {
    action: 'review_social_calendar_flow',
    locationId: mutationRequest?.locationId || null,
    businessName: mutationRequest?.business?.name || plan?.business?.name || null,
    generatedCount: reviewed.generatedCount,
    finalCount: reviewed.finalCount,
    removedDays: reviewed.removedDays,
    editedCount: reviewed.editedCount,
    appendedCount: reviewed.appendedCount,
    replaced: reviewed.replaced,
    firstScheduleDate: reviewed.firstScheduleDate,
    lastScheduleDate: reviewed.lastScheduleDate,
    knowledgeBaseMode: plan?.knowledgeBaseMode || null
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

function resolvedFacebookAdId(context, mutationRequest) {
  return mutationRequest?.adId
    || normalizeString(mutationRequest?.ad?.adId)
    || extractFacebookEntityId(context?.runtime?.stepOutputs?.upsert_facebook_ad?.data, ['adId'])
    || null;
}

function facebookPreviewResultResumeData(liveResult) {
  const data = liveResult?.data || liveResult || null;
  return {
    preview: data && typeof data === 'object'
      ? {
          htmlPath: data.htmlPath || null,
          jsonPath: data.jsonPath || null,
          pngPath: data.pngPath || null,
          campaignId: data.campaignId || null,
          adsetId: data.adsetId || null,
          adId: data.adId || null,
          locationId: data.locationId || null
        }
      : null,
    raw: data
  };
}

function facebookAdPreviewPayload(context, mutationRequest) {
  return {
    locationId: mutationRequest?.locationId || context?.event?.locationId || context?.event?.payload?.locationId || null,
    campaignId: resolvedFacebookCampaignId(context, mutationRequest),
    adsetId: resolvedFacebookAdsetId(context, mutationRequest),
    adId: resolvedFacebookAdId(context, mutationRequest),
    campaign: facebookCampaignBody(context, mutationRequest),
    adset: facebookAdsetBody(context, mutationRequest),
    ad: facebookAdBody(context, mutationRequest),
    sourcePost: resolvedPromotionSourcePost(context, mutationRequest)
  };
}

function facebookCampaignDependencyReady(context, mutationRequest) {
  return Boolean(resolvedFacebookCampaignId(context, mutationRequest));
}

function facebookAdsetDependencyReady(context, mutationRequest) {
  return Boolean(resolvedFacebookAdsetId(context, mutationRequest));
}

function facebookCampaignBody(context, mutationRequest) {
  if (facebookPromotionRequested(mutationRequest)) {
    return facebookPromotionCampaignBody({ ...mutationRequest, sourcePost: resolvedPromotionSourcePost(context, mutationRequest) });
  }
  const campaign = mutationRequest?.campaign;
  if (!campaign || Object.keys(campaign).length === 0) {
    throw new Error('Facebook campaign upsert requires a campaign object.');
  }
  return { ...campaign };
}

function facebookAdsetBody(context, mutationRequest) {
  if (facebookPromotionRequested(mutationRequest)) {
    return facebookPromotionAdsetBody(context, { ...mutationRequest, sourcePost: resolvedPromotionSourcePost(context, mutationRequest) });
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
    return facebookPromotionAdBody(context, { ...mutationRequest, sourcePost: resolvedPromotionSourcePost(context, mutationRequest) });
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

function splitCamelCaseWords(value) {
  const normalized = normalizeString(value);
  if (!normalized) return [];
  return normalized
    .replace(/([a-z])([A-Z])/g, '$1 $2')
    .split(/[^a-zA-Z0-9]+/)
    .map((part) => part.trim())
    .filter(Boolean);
}

function toTitleCase(value) {
  const normalized = normalizeString(value);
  if (!normalized) return null;
  return normalized
    .split(/\s+/)
    .map((part) => part ? `${part.charAt(0).toUpperCase()}${part.slice(1).toLowerCase()}` : part)
    .join(' ')
    .trim() || null;
}

function sourcePostHashtags(sourcePost) {
  const summary = sourcePostSummary(sourcePost);
  if (!summary) return [];
  const matches = summary.match(/#[A-Za-z0-9_]+/g) || [];
  return [...new Set(matches.map((tag) => splitCamelCaseWords(tag.replace(/^#/, '')).join(' ')).map(toTitleCase).filter(Boolean))];
}

function sourcePostTitle(sourcePost) {
  return normalizeString(sourcePost?.title)
    || normalizeString(sourcePost?.name)
    || normalizeString(sourcePost?.headline)
    || normalizeString(sourcePost?.subject)
    || null;
}

function sourcePostWebsiteUrl(sourcePost) {
  return normalizeString(sourcePost?.websiteUrl)
    || normalizeString(sourcePost?.link)
    || normalizeString(sourcePost?.url)
    || normalizeString(sourcePost?.destinationUrl)
    || normalizeString(sourcePost?.callToActionUrl)
    || normalizeString(sourcePost?.ogTagsDetails?.url)
    || normalizeString(sourcePost?.linkDetails?.url)
    || normalizeString(sourcePost?.cta?.url)
    || null;
}

function promotionBaseText(value) {
  const normalized = normalizeString(value);
  if (!normalized) return null;
  return normalized
    .replace(/https?:\/\/\S+/gi, ' ')
    .replace(/[#@][\p{L}\p{N}_-]+/gu, ' ')
    .replace(/\b\d+\.\s*/g, ' ')
    .replace(/\s+/g, ' ')
    .trim() || null;
}

function capitalizeSentence(value) {
  const normalized = normalizeString(value);
  if (!normalized) return null;
  return `${normalized.charAt(0).toUpperCase()}${normalized.slice(1)}`;
}

function promotionParagraphs(sourcePost) {
  const summary = sourcePostSummary(sourcePost);
  if (!summary) return [];
  return summary
    .split(/\n\s*\n/)
    .map((part) => promotionBaseText(part))
    .filter(Boolean);
}

function promotionSentences(value) {
  const normalized = normalizeString(value);
  if (!normalized) return [];
  return normalized
    .replace(/https?:\/\/\S+/gi, ' ')
    .replace(/[#@][\p{L}\p{N}_-]+/gu, ' ')
    .split(/(?<=[.!?])\s+|\n+/)
    .map((part) => promotionBaseText(part))
    .filter(Boolean);
}

function decompanyPromotionCopy(value) {
  const normalized = promotionBaseText(value);
  if (!normalized) return null;
  const cleaned = normalized
    .replace(/^(?:at\s+[^,.]+,\s+)?we\s+(?:help|build|create|turn|make)\s+(?:businesses|brands|teams|companies)\s+/i, '')
    .replace(/^(?:at\s+[^,.]+,\s+)?we\s+/i, '')
    .replace(/^that is where\s+/i, '')
    .replace(/^it is\s+/i, '')
    .replace(/^there is\s+/i, '')
    .replace(/^when\s+[^,]+,\s+/i, '')
    .replace(/^good design should\s+/i, '')
    .replace(/^great design makes it easy\.?$/i, 'Make the next step easy')
    .replace(/^most brands do not need to\s+/i, '')
    .replace(/^they need to\s+/i, '')
    .replace(/^a lot of lost revenue is not a traffic problem\.?\s*/i, '')
    .trim();
  return capitalizeSentence(cleaned);
}

function meaningfulPromotionLine(value, { maxLength = 90, minLength = 24 } = {}) {
  const cleaned = decompanyPromotionCopy(value);
  if (!cleaned) return null;
  if (/starts to feel\b/i.test(cleaned)) return null;
  const compact = clampText(cleaned, maxLength);
  if (!compact || compact.length < minLength) return null;
  return compact;
}

function cleanedPromotionPhrase(value, maxLength = 48) {
  const normalized = normalizeString(value);
  if (!normalized) return null;
  const cleaned = normalized
    .replace(/[#*_`]+/g, ' ')
    .replace(/[.!?]+$/g, '')
    .replace(/\s+/g, ' ')
    .trim();
  if (!cleaned) return null;
  return clampText(cleaned, maxLength);
}

function inferredPromotionThemeLabel(sourcePost) {
  const explicitTheme = cleanedPromotionPhrase(sourcePostTheme(sourcePost), 48);
  if (explicitTheme) return explicitTheme;

  const explicitTitle = cleanedPromotionPhrase(sourcePostTitle(sourcePost), 48);
  if (explicitTitle) return explicitTitle;

  const hashtags = sourcePostHashtags(sourcePost);
  if (hashtags.length > 0) {
    const preferred = hashtags.slice(0, 2).join(' and ');
    return cleanedPromotionPhrase(preferred, 48);
  }

  const firstLine = firstNonHashtagLine(sourcePostSummary(sourcePost));
  if (!firstLine) return null;

  const softened = firstLine
    .replace(/^(great|good|strong|better|best)\s+/i, '')
    .replace(/\b(gets|is|are|was|were|feels|looks|becomes)\b.*$/i, '')
    .trim();

  return cleanedPromotionPhrase(softened || firstLine, 48);
}

function concisePromotionHeadline(sourcePost) {
  const summary = normalizeString(sourcePostSummary(sourcePost))?.toLowerCase() || '';
  const theme = cleanedPromotionPhrase(sourcePostTheme(sourcePost), 28);
  const candidates = [
    /systems finally talk to each other|operations get lighter|internal handoffs/.test(summary)
      ? 'Get your systems in sync'
      : null,
    /operations are easier to trust|easier to trust/.test(summary)
      ? 'Build operations you can trust'
      : null,
    /faster decisions|less friction/.test(summary)
      ? 'Cut friction across operations'
      : null,
    /click to client|clear growth decisions|scattered activity|better tracking|better offers/.test(summary)
      ? 'Turn clicks into clients'
      : null,
    /books calls|captures leads|moves people to action|next step obvious/.test(summary)
      ? 'Make the next step obvious'
      : null,
    /show up consistently|consistency compounds/.test(summary)
      ? 'Consistency builds momentum'
      : null,
    /right message,?\s*for the right audience,?\s*at the right time/.test(summary)
      ? 'Reach the right people'
      : null,
    /feel more human|repetitive work|real conversations|faster follow-up/.test(summary)
      ? 'Give your team time back'
      : null,
    /follow-up problem|fast response|simple pipeline/.test(summary)
      ? 'Fix the follow-up gap'
      : null,
    /what is working\??.*what is not\??.*what do we change next\??/s.test(summary)
      ? 'Know what to change next'
      : null,
    /working together|predictable|strategy,?\s*site,?\s*content,?\s*automation,?\s*and follow-up/.test(summary)
      ? 'Get your growth stack aligned'
      : null,
    theme && /clarity/.test(theme.toLowerCase()) ? 'Make growth clearer' : null,
    theme && /website/.test(theme.toLowerCase()) ? 'Build a site that converts' : null,
    theme && /automation/.test(theme.toLowerCase()) ? 'Automate the repetitive work' : null,
    theme && /reporting/.test(theme.toLowerCase()) ? 'See what is working faster' : null,
    theme ? `Get clearer ${theme}` : null
  ];

  return candidates
    .map((value) => cleanedPromotionPhrase(value, 40))
    .find(Boolean) || null;
}

function themedPromotionHeadline(sourcePost) {
  const theme = toTitleCase(cleanedPromotionPhrase(inferredPromotionThemeLabel(sourcePost), 32));
  if (!theme) return null;

  const summary = normalizeString(sourcePostSummary(sourcePost))?.toLowerCase() || '';
  const themeLower = theme.toLowerCase();
  const candidates = [
    /website/.test(themeLower) && /convert|books calls|captures leads|moves people to action/.test(summary) ? 'Websites that convert' : null,
    /analytics|clarity/.test(themeLower) && /tracking|measurable|clear growth decisions/.test(summary) ? 'Analytics clarity that converts' : null,
    /consistency/.test(themeLower) || /compounds/.test(summary) ? 'Consistency that compounds' : null,
    /automation/.test(themeLower) && /feel more human/.test(summary) ? 'Automation that feels human' : null,
    /follow/.test(themeLower) ? 'Lead follow-up that closes' : null,
    /reporting/.test(themeLower) ? 'Reporting that answers fast' : null,
    /full stack|growth/.test(themeLower) && /working together|predictable/.test(summary) ? 'Growth systems that connect' : null,
    /books calls|captures leads|moves people to action/.test(summary) ? `${theme} that converts` : null,
    /tracking|measurable|clear growth decisions/.test(summary) ? `${theme} that converts` : null,
    /feel more human/.test(summary) ? `${theme} that feels human` : null,
    /compounds/.test(summary) ? `${theme} that compounds` : null,
    /predictable/.test(summary) ? `${theme} that feels predictable` : null,
    theme
  ];

  return candidates.map((value) => cleanedPromotionPhrase(value, 40)).find(Boolean) || null;
}

function concisePromotionDescription(sourcePost) {
  const summary = normalizeString(sourcePostSummary(sourcePost))?.toLowerCase() || '';
  const theme = cleanedPromotionPhrase(sourcePostTheme(sourcePost), 32);
  const candidates = [
    /simplify follow-up,? reporting,? and internal handoffs/.test(summary)
      ? 'Simplify follow-up, reporting, and internal handoffs.'
      : null,
    /consulting,? follow-up,? delivery,? and reporting are aligned/.test(summary)
      ? 'Align consulting, follow-up, delivery, and reporting.'
      : null,
    /faster decisions|less friction/.test(summary)
      ? 'Help leaders decide faster and teams move with less friction.'
      : null,
    /strengthen operations|automation systems that support cleaner growth/.test(summary)
      ? 'Strengthen operations with automation systems built for cleaner growth.'
      : null,
    /tracking|clear growth decisions|click to client|better offers/.test(summary)
      ? 'Improve tracking, offers, and the path from click to client.'
      : null,
    /books calls|captures leads|moves people to action/.test(summary)
      ? 'Book more calls, capture more leads, and drive action.'
      : null,
    /right message,?\s*for the right audience,?\s*at the right time/.test(summary)
      ? 'Reach the right audience with the right message at the right time.'
      : null,
    /feel more human|real conversations|better service/.test(summary)
      ? 'Free your team for faster follow-up and better service.'
      : null,
    /fast response,?\s*clear messaging,?\s*and a simple pipeline/.test(summary)
      ? 'Speed up follow-up with clearer messaging and a simpler pipeline.'
      : null,
    /what is working\??.*what is not\??.*what do we change next\??/s.test(summary)
      ? 'See what is working, what is not, and what to change next.'
      : null,
    /strategy,?\s*site,?\s*content,?\s*automation,?\s*and follow-up/.test(summary)
      ? 'Align strategy, site, content, automation, and follow-up.'
      : null,
    theme ? `Get clearer ${theme} with a simpler path to action.` : null
  ];

  return candidates
    .map((value) => cleanedPromotionPhrase(value, 72))
    .find(Boolean) || null;
}

function inferredPromotionBodyLine(sourcePost) {
  const paragraphs = promotionParagraphs(sourcePost);
  const laterParagraphLines = paragraphs.slice(1).flatMap((paragraph) => promotionSentences(paragraph));
  const sentenceLines = promotionSentences(sourcePostSummary(sourcePost));
  const candidates = [
    ...laterParagraphLines.map((line) => meaningfulPromotionLine(line, { maxLength: 90, minLength: 30 })),
    ...sentenceLines.slice(1).map((line) => meaningfulPromotionLine(line, { maxLength: 90, minLength: 30 })),
    meaningfulPromotionLine(sentenceLines[0], { maxLength: 90, minLength: 30 }),
    ...laterParagraphLines.map((line) => meaningfulPromotionLine(line, { maxLength: 90, minLength: 20 })),
    meaningfulPromotionLine(sourcePostTheme(sourcePost) ? `Get clearer ${sourcePostTheme(sourcePost)} with a simpler path to action.` : null, { maxLength: 90, minLength: 20 })
  ].filter(Boolean);

  return candidates[0] || null;
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

function fetchedPromotionSourcePost(context) {
  return context?.runtime?.stepOutputs?.get_source_social_post?.data?.results?.post
    || context?.runtime?.stepOutputs?.get_source_social_post?.data?.post
    || context?.runtime?.stepOutputs?.get_source_social_post?.data?.raw
    || null;
}

function resolvedPromotionSourcePost(context, mutationRequest) {
  const fetched = fetchedPromotionSourcePost(context);
  const explicit = normalizePlainObject(mutationRequest?.sourcePost) || {};
  if (fetched && typeof fetched === 'object') {
    return {
      ...fetched,
      ...explicit,
      media: Array.isArray(explicit.media) && explicit.media.length > 0 ? explicit.media : Array.isArray(fetched.media) ? fetched.media : [],
      mediaUrls: Array.isArray(explicit.mediaUrls) && explicit.mediaUrls.length > 0 ? explicit.mediaUrls : Array.isArray(fetched.mediaUrls) ? fetched.mediaUrls : [],
      imageUrls: Array.isArray(explicit.imageUrls) && explicit.imageUrls.length > 0 ? explicit.imageUrls : Array.isArray(fetched.imageUrls) ? fetched.imageUrls : []
    };
  }
  return explicit;
}

function facebookPromotionMediaUrls(mutationRequest) {
  return [...new Set([...(mutationRequest?.mediaUrls || []), ...sourcePostMediaUrls(mutationRequest?.sourcePost)])];
}

function resolvedFacebookPromotionMediaUrls(context, mutationRequest) {
  const sourcePost = resolvedPromotionSourcePost(context, mutationRequest);
  return [...new Set([...(mutationRequest?.mediaUrls || []), ...sourcePostMediaUrls(sourcePost)])];
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
  const sourcePost = mutationRequest?.sourcePost;
  const headlineLines = promotionParagraphs(sourcePost).slice(1)
    .flatMap((paragraph) => promotionSentences(paragraph))
    .map((line) => cleanedPromotionPhrase(decompanyPromotionCopy(line), 40))
    .filter(Boolean);
  return mutationRequest?.headline
    || cleanedPromotionPhrase(sourcePostTitle(sourcePost), 40)
    || concisePromotionHeadline(sourcePost)
    || themedPromotionHeadline(sourcePost)
    || headlineLines[0]
    || cleanedPromotionPhrase(firstNonHashtagLine(sourcePostSummary(sourcePost)), 40)
    || inferredPromotionThemeLabel(sourcePost)
    || clampText(sourcePostTheme(sourcePost), 40)
    || 'Learn more';
}

function inferredPromotionDescription(mutationRequest) {
  return mutationRequest?.description
    || concisePromotionDescription(mutationRequest?.sourcePost)
    || inferredPromotionBodyLine(mutationRequest?.sourcePost)
    || clampText(firstParagraph(sourcePostSummary(mutationRequest?.sourcePost)), 90)
    || clampText(sourcePostTheme(mutationRequest?.sourcePost), 90)
    || null;
}

function inferredPromotionCampaignName(mutationRequest) {
  const explicit = normalizeString(mutationRequest?.promotion?.campaignName) || normalizeString(mutationRequest?.campaign?.name);
  if (explicit) return explicit;
  const theme = inferredPromotionThemeLabel(mutationRequest?.sourcePost);
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
  const sourcePost = resolvedPromotionSourcePost(context, mutationRequest);
  const mediaUrls = resolvedFacebookPromotionMediaUrls(context, mutationRequest);
  const campaignId = resolvedFacebookCampaignId(context, mutationRequest);
  const adsetId = resolvedFacebookAdsetId(context, mutationRequest);
  const websiteUrl = mutationRequest?.websiteUrl || sourcePostWebsiteUrl(sourcePost) || null;
  const creative = {
    primaryText: sourcePostSummary(sourcePost),
    headline: inferredPromotionHeadline(mutationRequest),
    ...(inferredPromotionDescription({ ...mutationRequest, sourcePost }) ? { description: inferredPromotionDescription({ ...mutationRequest, sourcePost }) } : {}),
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
  const conversation = resolvedConversationRecord(context, mutationRequest);
  if (conversation?.contactId) return conversation.contactId;
  return resolvedSingleContactId(context, 'search_contacts_by_name', mutationRequest?.contactName);
}

function normalizeConversationStatus(value) {
  const normalized = normalizeString(value)?.toLowerCase();
  if (!normalized) return null;
  if (/closed|archived|resolved|done/.test(normalized)) return 'closed';
  if (/unread/.test(normalized)) return 'unread';
  if (/open|active/.test(normalized)) return 'open';
  return normalizeString(value);
}

function normalizeConversationRecord(value, fallback = {}) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const id = value.id || value._id || value.conversationId || value.conversationID || fallback.conversationId || null;
  if (!id) return null;
  return {
    id,
    locationId: value.locationId || fallback.locationId || null,
    contactId: value.contactId || value.contact?.id || value.contactID || fallback.contactId || null,
    contactName: value.fullName || value.contactName || value.contact?.name || value.contact?.fullName || fallback.contactName || null,
    email: value.email || value.contact?.email || null,
    phone: value.phone || value.contact?.phone || null,
    status: normalizeConversationStatus(value.status || value.conversationStatus) || null,
    channel: normalizeConversationMessageType(value.channel || value.type || value.lastMessageType || value.lastManualMessageType) || normalizeString(value.channel || value.type || value.lastMessageType || value.lastManualMessageType),
    unreadCount: normalizeNumber(value.unreadCount ?? value.unreadMessages ?? value.unread) || 0,
    lastMessageBody: value.lastMessageBody || value.lastMessage || value.message || value.snippet || null,
    lastMessageType: normalizeConversationMessageType(value.lastMessageType || value.messageType || value.type) || normalizeString(value.lastMessageType || value.messageType || value.type),
    lastMessageDate: value.lastMessageDate || value.lastManualMessageDate || value.dateUpdated || value.updatedAt || value.dateAdded || value.createdAt || null,
    dateAdded: value.dateAdded || value.createdAt || null
  };
}

function collectConversationRecords(value, output = [], fallback = {}) {
  if (!value) return output;
  if (Array.isArray(value)) {
    value.forEach((item) => collectConversationRecords(item, output, fallback));
    return output;
  }
  if (typeof value !== 'object') return output;

  const looksLikeConversation = (typeof value.id === 'string' || typeof value._id === 'string' || typeof value.conversationId === 'string')
    && (
      typeof value.contactId === 'string'
      || typeof value.fullName === 'string'
      || typeof value.contactName === 'string'
      || typeof value.unreadCount !== 'undefined'
      || typeof value.lastMessageBody === 'string'
      || typeof value.lastMessage === 'string'
      || typeof value.status === 'string'
      || typeof value.channel === 'string'
      || typeof value.phone === 'string'
      || typeof value.email === 'string'
    );

  if (looksLikeConversation) {
    const normalized = normalizeConversationRecord(value, fallback);
    if (normalized) output.push(normalized);
  }

  Object.values(value).forEach((item) => collectConversationRecords(item, output, fallback));
  return output;
}

function conversationRecordsFromData(data, fallback = {}) {
  const records = collectConversationRecords(data, [], fallback);
  if (records.length === 0) {
    const normalized = normalizeConversationRecord(data, fallback);
    if (normalized) records.push(normalized);
  }
  const deduped = [];
  const seen = new Set();
  for (const record of records) {
    if (!record?.id || seen.has(record.id)) continue;
    seen.add(record.id);
    deduped.push(record);
  }
  return deduped.sort((left, right) => {
    const leftMs = Date.parse(left?.lastMessageDate || left?.dateAdded || '') || 0;
    const rightMs = Date.parse(right?.lastMessageDate || right?.dateAdded || '') || 0;
    return rightMs - leftMs;
  });
}

function conversationMatchesFilters(conversation, mutationRequest, resolvedContactId = null) {
  if (!conversation) return false;

  if (resolvedContactId) {
    if (conversation.contactId && conversation.contactId !== resolvedContactId) return false;
  } else if (mutationRequest?.contactName) {
    const requested = mutationRequest.contactName.toLowerCase();
    const nameHaystack = [conversation.contactName, conversation.email, conversation.phone]
      .map((value) => normalizeString(value)?.toLowerCase())
      .filter(Boolean)
      .join(' ');
    if (nameHaystack && !nameHaystack.includes(requested)) return false;
  }

  if (mutationRequest?.unreadOnly === true && !(conversation.unreadCount > 0)) return false;
  if (mutationRequest?.openOnly === true && normalizeConversationStatus(conversation.status) === 'closed') return false;

  const query = normalizeString(mutationRequest?.searchQuery)?.toLowerCase();
  if (query) {
    const haystack = [
      conversation.contactName,
      conversation.email,
      conversation.phone,
      conversation.lastMessageBody,
      conversation.channel,
      conversation.status
    ]
      .map((value) => normalizeString(value)?.toLowerCase())
      .filter(Boolean)
      .join(' ');
    if (!haystack.includes(query)) return false;
  }

  return true;
}

function resolvedConversationSearchResults(context) {
  const conversations = context?.runtime?.stepOutputs?.search_conversations?.data?.conversations;
  return Array.isArray(conversations) ? conversations : [];
}

function resolvedConversationRecord(context, mutationRequest, { requireMatch = false } = {}) {
  const fetchedConversation = context?.runtime?.stepOutputs?.fetch_conversation?.data?.conversation;
  if (fetchedConversation && (!mutationRequest?.conversationId || fetchedConversation.id === mutationRequest.conversationId)) {
    return fetchedConversation;
  }

  const searchResults = resolvedConversationSearchResults(context);
  if (mutationRequest?.conversationId) {
    const exact = searchResults.find((conversation) => conversation.id === mutationRequest.conversationId);
    if (exact) return exact;
  }
  if (searchResults.length > 0) return searchResults[0];

  if (mutationRequest?.conversationId) {
    return {
      id: mutationRequest.conversationId,
      locationId: mutationRequest.locationId || null,
      contactId: mutationRequest.contactId || null,
      contactName: mutationRequest.contactName || null,
      channel: mutationRequest.channel || null,
      lastMessageType: mutationRequest.messageType || null,
      unreadCount: mutationRequest.unreadOnly ? 1 : 0,
      status: mutationRequest.openOnly ? 'open' : null,
      lastMessageBody: null,
      lastMessageDate: null,
      dateAdded: null
    };
  }

  if (requireMatch) {
    throw new Error(mutationRequest?.contactName
      ? `No conversation matched "${mutationRequest.contactName}".`
      : 'No conversation matched the request.');
  }
  return null;
}

function resolvedConversationId(context, mutationRequest, options = {}) {
  if (mutationRequest?.conversationId) return mutationRequest.conversationId;
  return resolvedConversationRecord(context, mutationRequest, options)?.id || null;
}

function normalizeConversationReplyTypeCandidate(value) {
  return normalizeConversationMessageType(value) || normalizeString(value);
}

function resolvedConversationMessageType(context, mutationRequest) {
  if (mutationRequest?.messageType) return mutationRequest.messageType;
  const conversation = resolvedConversationRecord(context, mutationRequest);
  const latestMessage = context?.runtime?.stepOutputs?.fetch_conversation_messages?.data?.latestMessage || null;
  return normalizeConversationReplyTypeCandidate(conversation?.lastMessageType)
    || normalizeConversationReplyTypeCandidate(conversation?.channel)
    || normalizeConversationReplyTypeCandidate(latestMessage?.type)
    || 'SMS';
}

function resolvedConversationChannel(context, mutationRequest) {
  if (mutationRequest?.channel) return mutationRequest.channel;
  const conversation = resolvedConversationRecord(context, mutationRequest);
  return normalizeString(conversation?.channel) || null;
}

function resolvedConversationContactDetails(context, mutationRequest, action = 'send_conversation_message') {
  const conversation = resolvedConversationRecord(context, mutationRequest);
  const resolvedContactId = mutationRequest?.contactId
    || conversation?.contactId
    || context?.runtime?.stepOutputs?.search_contacts_by_name?.data?.contacts?.[0]?.id
    || null;
  return {
    action,
    locationId: mutationRequest?.locationId || null,
    conversationId: mutationRequest?.conversationId || conversation?.id || null,
    contactId: resolvedContactId,
    contactName: mutationRequest?.contactName || conversation?.contactName || null,
    messageType: resolvedConversationMessageType(context, mutationRequest),
    channel: resolvedConversationChannel(context, mutationRequest),
    fromNumber: mutationRequest?.fromNumber || null,
    toNumber: mutationRequest?.toNumber || null,
    messagePreview: mutationRequest?.message ? mutationRequest.message.slice(0, 120) : null
  };
}

function conversationSearchResumeData(liveResult, context) {
  const mutationRequest = conversationMutationRequestFrom(context?.event);
  const data = liveResult?.data || null;
  const resolvedContactId = mutationRequest?.contactId || context?.runtime?.stepOutputs?.search_contacts_by_name?.data?.contacts?.[0]?.id || null;
  const limit = normalizeNumber(mutationRequest?.limit) || 10;
  const conversations = conversationRecordsFromData(data, {
    locationId: mutationRequest?.locationId || null,
    contactId: resolvedContactId,
    contactName: mutationRequest?.contactName || null
  })
    .filter((conversation) => conversationMatchesFilters(conversation, mutationRequest, resolvedContactId))
    .slice(0, limit);

  return {
    count: conversations.length,
    filters: {
      contactId: resolvedContactId,
      contactName: mutationRequest?.contactName || null,
      unreadOnly: mutationRequest?.unreadOnly === true,
      openOnly: mutationRequest?.openOnly === true,
      query: mutationRequest?.searchQuery || null,
      limit
    },
    conversations,
    raw: data
  };
}

function fetchConversationResumeData(liveResult, context) {
  const data = liveResult?.data || null;
  const mutationRequest = conversationMutationRequestFrom(context?.event);
  const conversation = normalizeConversationRecord(data, {
    conversationId: mutationRequest?.conversationId || null,
    locationId: mutationRequest?.locationId || null,
    contactId: mutationRequest?.contactId || null,
    contactName: mutationRequest?.contactName || null
  });
  return {
    conversation,
    raw: data
  };
}

function normalizeConversationMessageRecord(value, fallback = {}) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const id = value.id || value._id || value.messageId || fallback.messageId || null;
  const body = value.body || value.message || value.text || value.content || value.html || null;
  const type = normalizeConversationReplyTypeCandidate(value.messageType || value.type || fallback.type);
  const conversationId = value.conversationId || value.conversation?.id || fallback.conversationId || null;
  const dateAdded = value.dateAdded || value.createdAt || value.updatedAt || value.dateUpdated || value.timestamp || null;
  if (!id && !body && !type) return null;
  return {
    id: id || `${conversationId || 'conversation'}:${dateAdded || body || type}`,
    conversationId,
    body: normalizeString(body),
    type,
    direction: normalizeString(value.direction || value.messageDirection || value.directionType),
    status: normalizeString(value.status),
    dateAdded
  };
}

function collectConversationMessages(value, output = [], fallback = {}) {
  if (!value) return output;
  if (Array.isArray(value)) {
    value.forEach((item) => collectConversationMessages(item, output, fallback));
    return output;
  }
  if (typeof value !== 'object') return output;

  const looksLikeMessage = (typeof value.id === 'string' || typeof value._id === 'string' || typeof value.messageId === 'string')
    && (
      typeof value.body === 'string'
      || typeof value.message === 'string'
      || typeof value.text === 'string'
      || typeof value.content === 'string'
      || typeof value.type === 'string'
      || typeof value.messageType === 'string'
      || typeof value.conversationId === 'string'
    );

  if (looksLikeMessage) {
    const normalized = normalizeConversationMessageRecord(value, fallback);
    if (normalized) output.push(normalized);
  }

  Object.values(value).forEach((item) => collectConversationMessages(item, output, fallback));
  return output;
}

function conversationMessagesResumeData(liveResult, context) {
  const mutationRequest = conversationMutationRequestFrom(context?.event);
  const data = liveResult?.data || null;
  const conversationId = resolvedConversationId(context, mutationRequest, { requireMatch: false }) || mutationRequest?.conversationId || null;
  const messages = collectConversationMessages(data, [], { conversationId, type: mutationRequest?.messageType || null })
    .sort((left, right) => {
      const leftMs = Date.parse(left?.dateAdded || '') || 0;
      const rightMs = Date.parse(right?.dateAdded || '') || 0;
      return leftMs - rightMs;
    });
  const latestMessages = messages.slice(-10);
  return {
    conversationId,
    messageCount: messages.length,
    latestMessage: latestMessages[latestMessages.length - 1] || null,
    messages: latestMessages,
    raw: data
  };
}

function sendConversationMessageResumeData(liveResult, context) {
  const data = liveResult?.data || null;
  const mutationRequest = conversationMutationRequestFrom(context?.event);
  const conversation = resolvedConversationRecord(context, mutationRequest);
  return {
    requested: {
      locationId: mutationRequest?.locationId || null,
      conversationId: mutationRequest?.conversationId || conversation?.id || null,
      contactId: mutationRequest?.contactId || conversation?.contactId || context?.runtime?.stepOutputs?.search_contacts_by_name?.data?.contacts?.[0]?.id || null,
      contactName: mutationRequest?.contactName || conversation?.contactName || null,
      messageType: resolvedConversationMessageType(context, mutationRequest),
      channel: resolvedConversationChannel(context, mutationRequest),
      messagePreview: mutationRequest?.message ? mutationRequest.message.slice(0, 120) : null
    },
    result: data && typeof data === 'object'
      ? {
          id: data?.id || data?._id || null,
          conversationId: data?.conversationId || data?.conversation?.id || mutationRequest?.conversationId || null,
          messageId: data?.messageId || data?.id || null,
          status: data?.status || null,
          type: data?.type || data?.messageType || mutationRequest?.messageType || null
        }
      : null,
    raw: data
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
        const explicitConversationReply = mutationRequest?.action === 'reply_latest_conversation'
          && (Boolean(mutationRequest.contactId) || Boolean(mutationRequest.contactName) || Boolean(mutationRequest.conversationId))
          && Boolean(mutationRequest.message);
        const explicitConversationSearch = mutationRequest?.action === 'search_conversations';
        const explicitConversationSummary = mutationRequest?.action === 'fetch_conversation_summary';
        const needsContactLookup = (explicitConversationSend || explicitConversationReply || explicitConversationSearch || explicitConversationSummary)
          && Boolean(mutationRequest.contactName)
          && !Boolean(mutationRequest.contactId);
        const needsConversationSearch = explicitConversationSearch
          || (!Boolean(mutationRequest.conversationId) && (explicitConversationReply || explicitConversationSummary));
        return [
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
            skipIf: () => !needsContactLookup
          },
          {
            name: 'search_conversations',
            kind: 'adapter_call',
            adapter: 'ConversationsAdapter',
            method: 'listConversations',
            pathHint: '/conversations/',
            args: () => [context.credentialRef || defaultLocationCredential(context)],
            safe: true,
            mutation: false,
            requiresCredential: true,
            resumeData: conversationSearchResumeData,
            details: {
              action: explicitConversationSearch ? 'search_conversations' : (explicitConversationReply ? 'find_latest_conversation' : 'find_conversation_for_summary'),
              contactName: mutationRequest?.contactName || null,
              count: mutationRequest?.limit || null,
              status: mutationRequest?.unreadOnly ? 'unread' : (mutationRequest?.openOnly ? 'open' : null)
            },
            skipIf: () => !needsConversationSearch
          },
          {
            name: 'fetch_conversation',
            kind: 'adapter_call',
            adapter: 'ConversationsAdapter',
            method: 'getConversation',
            pathHint: '/conversations/:conversationId',
            args: (runtimeContext) => [runtimeContext.credentialRef || defaultLocationCredential(runtimeContext), resolvedConversationId(runtimeContext, mutationRequest, { requireMatch: explicitConversationReply || explicitConversationSummary || Boolean(conversationId) })],
            safe: true,
            mutation: false,
            requiresCredential: true,
            resumeData: fetchConversationResumeData,
            details: (runtimeContext) => ({
              action: explicitConversationSummary ? 'fetch_conversation_summary' : (explicitConversationReply ? 'fetch_latest_conversation' : 'fetch_conversation'),
              conversationId: resolvedConversationId(runtimeContext, mutationRequest, { requireMatch: explicitConversationReply || explicitConversationSummary || Boolean(conversationId) }),
              locationId: mutationRequest?.locationId || null,
              contactName: mutationRequest?.contactName || null
            }),
            skipIf: () => explicitConversationSearch || (explicitConversationSend && !explicitConversationReply) || (!conversationId && !explicitConversationReply && !explicitConversationSummary)
          },
          {
            name: 'fetch_conversation_messages',
            kind: 'adapter_call',
            adapter: 'ConversationsAdapter',
            method: 'getMessages',
            pathHint: '/conversations/:conversationId/messages',
            args: (runtimeContext) => [runtimeContext.credentialRef || defaultLocationCredential(runtimeContext), resolvedConversationId(runtimeContext, mutationRequest, { requireMatch: true })],
            safe: true,
            mutation: false,
            requiresCredential: true,
            resumeData: conversationMessagesResumeData,
            details: (runtimeContext) => ({
              action: explicitConversationReply ? 'fetch_latest_conversation_messages' : 'fetch_conversation_messages',
              conversationId: resolvedConversationId(runtimeContext, mutationRequest, { requireMatch: true }),
              contactName: mutationRequest?.contactName || null
            }),
            skipIf: () => !(explicitConversationSummary || explicitConversationReply)
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
              type: resolvedConversationMessageType(runtimeContext, mutationRequest),
              ...(mutationRequest.channel ? { channel: mutationRequest.channel } : {}),
              ...(mutationRequest.fromNumber ? { fromNumber: mutationRequest.fromNumber } : {}),
              ...(mutationRequest.toNumber ? { toNumber: mutationRequest.toNumber } : {})
            }],
            safe: false,
            mutation: true,
            requiresApproval: true,
            requiresCredential: true,
            resumeData: sendConversationMessageResumeData,
            details: (runtimeContext) => resolvedConversationContactDetails(runtimeContext, mutationRequest, explicitConversationReply ? 'reply_latest_conversation' : 'send_conversation_message'),
            skipIf: () => !(explicitConversationSend || explicitConversationReply)
          },
          {
            name: 'evaluate_sla_and_assignment',
            kind: 'intent',
            safe: true,
            mutation: false,
            details: { action: 'evaluate_response_rules', conversationId, mutationRequest },
            skipIf: () => explicitConversationSend || explicitConversationReply || explicitConversationSearch || explicitConversationSummary
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
        const socialCalendarRequest = socialCalendarMutationRequestFrom(context.event);
        const locationId = socialCalendarRequest?.locationId
          || resolvedSocialPostLocationId(context, mutationRequest)
          || context.event.locationId
          || null;
        const explicitCreateSocialPost = mutationRequest?.action === 'create_social_post';
        const explicitCreateSocialPostsBulk = isBulkSocialPostAction(mutationRequest?.action);
        const explicitGenerateSocialCalendar = isGenerateSocialCalendarAction(socialCalendarRequest?.action);
        const explicitRunSocialCalendarFlow = isRunSocialCalendarFlowAction(socialCalendarRequest?.action);
        const inferredRunSocialCalendarFlow = inferRunSocialCalendarFlowRequest(socialCalendarRequest);
        const shouldRunSocialCalendarFlow = explicitRunSocialCalendarFlow || inferredRunSocialCalendarFlow;
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
            skipIf: () => !locationId || explicitGenerateSocialCalendar || shouldRunSocialCalendarFlow
          },
          {
            name: 'generate_social_calendar',
            kind: 'adapter_call',
            adapter: 'SocialPlannerAdapter',
            method: 'generateCalendarPlan',
            pathHint: 'local://generated/social-calendar',
            args: () => [null, socialCalendarRequest],
            safe: true,
            mutation: false,
            requiresCredential: false,
            resumeData: socialCalendarGenerateResumeData,
            details: {
              action: 'generate_social_calendar',
              locationId: socialCalendarRequest?.locationId || null,
              businessName: socialCalendarRequest?.business?.name || null,
              industry: socialCalendarRequest?.business?.industry || null,
              postCount: socialCalendarRequest?.calendar?.postCount || null,
              cadenceDays: socialCalendarRequest?.calendar?.cadenceDays || null,
              knowledgeBaseRef: socialCalendarRequest?.business?.knowledgeBaseRef || null,
              creativeStyles: socialCalendarRequest?.creativeStyles || []
            },
            skipIf: () => !(explicitGenerateSocialCalendar || shouldRunSocialCalendarFlow)
          },
          {
            name: 'review_social_calendar_flow',
            kind: 'intent',
            safe: true,
            mutation: false,
            details: (runtimeContext) => socialCalendarFlowReviewDetails(runtimeContext, socialCalendarRequest),
            skipIf: () => !shouldRunSocialCalendarFlow
          },
          {
            name: 'generate_social_post_variants',
            kind: 'adapter_call',
            adapter: 'SocialPlannerAdapter',
            method: 'generateCreativeVariants',
            pathHint: 'local://generated/social-post-creative-variants',
            args: () => [
              null,
              locationId,
              socialPostCreateBody(mutationRequest),
              {
                businessName: normalizeString(mutationRequest?.post?.businessName)
                  || normalizeString(mutationRequest?.businessName)
                  || null,
                brandVoice: normalizeString(mutationRequest?.post?.brandVoice)
                  || normalizeString(mutationRequest?.post?.voice)
                  || normalizeString(mutationRequest?.brandVoice)
                  || null,
                websiteUrl: normalizeString(mutationRequest?.post?.websiteUrl)
                  || normalizeString(mutationRequest?.post?.website)
                  || normalizeString(mutationRequest?.websiteUrl)
                  || null,
                logoUrl: normalizeString(mutationRequest?.post?.logoUrl)
                  || normalizeString(mutationRequest?.post?.logo)
                  || normalizeString(mutationRequest?.logoUrl)
                  || null,
                variantCount: mutationRequest?.variantCount || null,
                creativeStyles: mutationRequest?.creativeStyles || []
              }
            ],
            safe: true,
            mutation: false,
            requiresCredential: false,
            resumeData: socialPostVariantGenerateResumeData,
            details: {
              action: 'generate_social_post_variants',
              locationId,
              businessName: mutationRequest?.businessName || null,
              summaryPreview: mutationRequest?.summary ? mutationRequest.summary.slice(0, 160) : null,
              creativeStyles: mutationRequest?.creativeStyles || [],
              variantCount: socialPostRequestedVariantCount(mutationRequest)
            },
            skipIf: () => !explicitCreateSocialPost || !shouldGenerateSocialPostVariants(mutationRequest)
          },
          {
            name: 'create_social_post',
            kind: 'adapter_call',
            adapter: 'SocialPlannerAdapter',
            method: 'createPostWithCreative',
            httpMethod: 'POST',
            pathHint: '/social-media-posting/:locationId/posts',
            args: (runtimeContext) => {
              const selection = selectedSocialPostVariantFromContext(runtimeContext);
              return [
                context.credentialRef || defaultLocationCredential(context),
                locationId,
                socialPostCreateBody(mutationRequest),
                {
                  generateCreative: !Array.isArray(mutationRequest?.post?.media) || mutationRequest.post.media.length === 0,
                  businessName: normalizeString(mutationRequest?.post?.businessName)
                    || normalizeString(mutationRequest?.businessName)
                    || null,
                  brandVoice: normalizeString(mutationRequest?.post?.brandVoice)
                    || normalizeString(mutationRequest?.post?.voice)
                    || normalizeString(mutationRequest?.brandVoice)
                    || null,
                  websiteUrl: normalizeString(mutationRequest?.post?.websiteUrl)
                    || normalizeString(mutationRequest?.post?.website)
                    || normalizeString(mutationRequest?.websiteUrl)
                    || null,
                  logoUrl: normalizeString(mutationRequest?.post?.logoUrl)
                    || normalizeString(mutationRequest?.post?.logo)
                    || normalizeString(mutationRequest?.logoUrl)
                    || null,
                  generatedVariant: selection?.variant || null,
                  generatedVariants: selection?.variants || [],
                  variantStyles: Array.isArray(selection?.variants)
                    ? selection.variants.map((variant) => variant?.model?.styleId || variant?.styleId || null).filter(Boolean)
                    : [],
                  variantCount: mutationRequest?.variantCount || null,
                  creativeStyles: mutationRequest?.creativeStyles || []
                }
              ];
            },
            safe: false,
            mutation: true,
            requiresApproval: true,
            requiresCredential: true,
            resumeData: socialPostCreateResumeData,
            details: (runtimeContext) => socialPostCreateDetails(runtimeContext, mutationRequest),
            skipIf: () => !explicitCreateSocialPost
          },
          {
            name: 'create_social_posts_bulk',
            kind: 'adapter_call',
            adapter: 'SocialPlannerAdapter',
            method: 'createPostsWithCreative',
            httpMethod: 'POST',
            pathHint: '/social-media-posting/:locationId/posts (bulk orchestrated)',
            args: () => [
              context.credentialRef || defaultLocationCredential(context),
              locationId,
              socialPostBulkCreateBodies(mutationRequest),
              {
                generateCreative: true,
                businessName: normalizeString(mutationRequest?.businessName) || null,
                brandVoice: normalizeString(mutationRequest?.brandVoice) || null,
                websiteUrl: normalizeString(mutationRequest?.websiteUrl) || null,
                logoUrl: normalizeString(mutationRequest?.logoUrl) || null,
                variantCount: mutationRequest?.variantCount || null,
                creativeStyles: mutationRequest?.creativeStyles || [],
                continueOnError: mutationRequest?.continueOnError === true
              }
            ],
            safe: false,
            mutation: true,
            requiresApproval: true,
            requiresCredential: true,
            resumeData: socialPostBulkCreateResumeData,
            details: {
              action: 'create_social_posts_bulk',
              locationId,
              count: Array.isArray(mutationRequest?.posts) ? mutationRequest.posts.length : 0,
              accountIds: mutationRequest?.accountIds || [],
              status: mutationRequest?.status || null,
              businessName: mutationRequest?.businessName || null,
              creativeStyles: mutationRequest?.creativeStyles || [],
              variantCount: mutationRequest?.variantCount || null,
              continueOnError: mutationRequest?.continueOnError === true
            },
            skipIf: () => !explicitCreateSocialPostsBulk
          },
          {
            name: 'create_social_calendar_posts',
            kind: 'adapter_call',
            adapter: 'SocialPlannerAdapter',
            method: 'createPostsWithCreative',
            httpMethod: 'POST',
            pathHint: '/social-media-posting/:locationId/posts (calendar flow)',
            args: (runtimeContext) => {
              if (!locationId) {
                throw new Error('Social calendar flow requires a locationId.');
              }
              return [
                context.credentialRef || defaultLocationCredential(context),
                locationId,
                socialCalendarFlowCreateBodies(runtimeContext, socialCalendarRequest),
                {
                  generateCreative: true,
                  businessName: normalizeString(socialCalendarRequest?.business?.name) || null,
                  brandVoice: normalizeString(socialCalendarRequest?.business?.brandVoice) || null,
                  websiteUrl: normalizeString(socialCalendarRequest?.business?.websiteUrl) || null,
                  logoUrl: normalizeString(socialCalendarRequest?.business?.logoUrl) || null,
                  variantCount: socialCalendarRequest?.variantCount || null,
                  creativeStyles: socialCalendarRequest?.creativeStyles || [],
                  continueOnError: socialCalendarRequest?.continueOnError === true
                }
              ];
            },
            safe: false,
            mutation: true,
            requiresApproval: true,
            requiresCredential: true,
            resumeData: socialCalendarFlowCreateResumeData,
            details: (runtimeContext) => ({
              ...socialCalendarFlowReviewDetails(runtimeContext, socialCalendarRequest),
              action: 'run_social_calendar_flow',
              locationId,
              businessName: socialCalendarRequest?.business?.name || null,
              variantCount: socialCalendarRequest?.variantCount || null,
              continueOnError: socialCalendarRequest?.continueOnError === true
            }),
            skipIf: (runtimeContext) => !shouldRunSocialCalendarFlow || !socialCalendarFlowReviewedPosts(runtimeContext, socialCalendarRequest).ready
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
            skipIf: () => !explicitCreateSocialPost || explicitCreateSocialPostsBulk || shouldRunSocialCalendarFlow
          }
        ];
      }
    },
    facebook_ad_pack: {
      trigger_events: ['ManualRun'],
      buildExecutionPlan(context) {
        const mutationRequest = facebookAdMutationRequestFrom(context.event);
        const hasInlinePromotionSource = Boolean(sourcePostSummary(mutationRequest?.sourcePost))
          && (facebookPromotionMediaUrls(mutationRequest).length > 0 || Boolean(mutationRequest?.websiteUrl));
        const explicitPromoteSocialPost = facebookPromotionRequested(mutationRequest)
          && (hasInlinePromotionSource || Boolean(mutationRequest?.sourcePostId));
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
            name: 'get_source_social_post',
            kind: 'adapter_call',
            adapter: 'SocialPlannerAdapter',
            method: 'getPost',
            pathHint: '/social-media-posting/:locationId/posts/:postId',
            args: () => [
              context.credentialRef || defaultLocationCredential(context),
              mutationRequest?.locationId || context.event.locationId || null,
              mutationRequest?.sourcePostId,
              {}
            ],
            safe: true,
            mutation: false,
            requiresCredential: true,
            resumeData: socialPostFetchResumeData,
            details: {
              action: 'get_source_social_post',
              locationId: mutationRequest?.locationId || context.event.locationId || null,
              postId: mutationRequest?.sourcePostId || null
            },
            skipIf: () => !explicitPromoteSocialPost || !mutationRequest?.sourcePostId
          },
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
            args: (runtimeContext) => [runtimeContext.credentialRef || defaultLocationCredential(runtimeContext), facebookCampaignBody(runtimeContext, mutationRequest)],
            safe: false,
            mutation: true,
            requiresApproval: true,
            requiresCredential: true,
            resumeData: facebookCampaignUpsertResumeData,
            details: (runtimeContext) => ({
              action: 'upsert_facebook_campaign',
              locationId: mutationRequest?.locationId || runtimeContext.event.locationId || null,
              campaignId: mutationRequest?.campaignId || null,
              name: mutationRequest?.campaign?.name || inferredPromotionCampaignName({ ...mutationRequest, sourcePost: resolvedPromotionSourcePost(runtimeContext, mutationRequest) }),
              objective: mutationRequest?.campaign?.objective || mutationRequest?.campaign?.goal || inferredPromotionObjective(mutationRequest),
              status: mutationRequest?.campaign?.status || inferredPromotionStatus(mutationRequest),
              sourceSummaryPreview: sourcePostSummary(resolvedPromotionSourcePost(runtimeContext, mutationRequest))?.slice(0, 160) || null
            }),
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
              name: mutationRequest?.adset?.name || inferredPromotionAdsetName({ ...mutationRequest, sourcePost: resolvedPromotionSourcePost(runtimeContext, mutationRequest) }),
              status: mutationRequest?.adset?.status || inferredPromotionStatus(mutationRequest),
              sourceMediaCount: resolvedFacebookPromotionMediaUrls(runtimeContext, mutationRequest).length
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
              name: mutationRequest?.ad?.name || inferredPromotionAdName({ ...mutationRequest, sourcePost: resolvedPromotionSourcePost(runtimeContext, mutationRequest) }),
              status: mutationRequest?.ad?.status || inferredPromotionStatus(mutationRequest),
              websiteUrl: mutationRequest?.websiteUrl || null,
              mediaUrls: resolvedFacebookPromotionMediaUrls(runtimeContext, mutationRequest)
            }),
            skipIf: (runtimeContext) => !explicitAdUpsert || !facebookCampaignDependencyReady(runtimeContext, mutationRequest) || !facebookAdsetDependencyReady(runtimeContext, mutationRequest)
          },
          {
            name: 'generate_facebook_ad_preview',
            kind: 'adapter_call',
            adapter: 'PreviewArtifactsAdapter',
            method: 'generateFacebookAdPreview',
            safe: true,
            mutation: false,
            requiresCredential: false,
            resumeData: facebookPreviewResultResumeData,
            args: (runtimeContext) => [
              runtimeContext.credentialRef || defaultLocationCredential(runtimeContext),
              facebookAdPreviewPayload(runtimeContext, mutationRequest)
            ],
            details: (runtimeContext) => ({
              action: 'generate_facebook_ad_preview',
              locationId: mutationRequest?.locationId || runtimeContext.event.locationId || null,
              campaignId: resolvedFacebookCampaignId(runtimeContext, mutationRequest),
              adsetId: resolvedFacebookAdsetId(runtimeContext, mutationRequest),
              adId: resolvedFacebookAdId(runtimeContext, mutationRequest)
            }),
            skipIf: (runtimeContext) => {
              const adWrite = runtimeContext?.runtime?.stepOutputs?.upsert_facebook_ad;
              return !explicitAdUpsert || !resolvedFacebookAdId(runtimeContext, mutationRequest) || adWrite?.ok !== true;
            }
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
