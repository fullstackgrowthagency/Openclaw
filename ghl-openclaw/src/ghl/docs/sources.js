export const VERIFIED_DOC_SOURCES = [
  {
    category: 'oauth',
    resource: 'oauth_2_0',
    url: 'https://marketplace.gohighlevel.com/docs/ghl/oauth/oauth-2-0/',
    accessLevel: 'agency',
    tokenTypes: ['oauth_access_token'],
    scopeHint: 'oauth.*',
    status: 'supported'
  },
  {
    category: 'authorization',
    resource: 'private_integration_tokens',
    url: 'https://marketplace.gohighlevel.com/docs/Authorization/PrivateIntegrationsToken',
    accessLevel: 'both',
    tokenTypes: ['private_integration_token'],
    scopeHint: 'ui_managed',
    status: 'supported'
  },
  {
    category: 'authorization',
    resource: 'scopes',
    url: 'https://marketplace.gohighlevel.com/docs/Authorization/Scopes/',
    accessLevel: 'both',
    tokenTypes: ['oauth_access_token', 'private_integration_token'],
    scopeHint: 'all',
    status: 'supported'
  },
  {
    category: 'webhooks',
    resource: 'integration_guide',
    url: 'https://marketplace.gohighlevel.com/docs/webhook/WebhookIntegrationGuide',
    accessLevel: 'agency',
    tokenTypes: ['oauth_access_token'],
    scopeHint: 'webhook',
    status: 'supported'
  },
  {
    category: 'webhooks',
    resource: 'logs_dashboard',
    url: 'https://marketplace.gohighlevel.com/docs/webhook/WebhookLogsDashboard',
    accessLevel: 'agency',
    tokenTypes: ['oauth_access_token'],
    scopeHint: 'webhook',
    status: 'supported'
  },
  {
    category: 'contacts',
    resource: 'contacts',
    url: 'https://marketplace.gohighlevel.com/docs/ghl/contacts/contacts',
    accessLevel: 'location',
    tokenTypes: ['oauth_access_token', 'private_integration_token'],
    scopeHint: 'contacts.*',
    status: 'supported'
  },
  {
    category: 'conversations',
    resource: 'conversations',
    url: 'https://marketplace.gohighlevel.com/docs/ghl/conversations/conversations',
    accessLevel: 'location',
    tokenTypes: ['oauth_access_token', 'private_integration_token'],
    scopeHint: 'conversations.*',
    status: 'supported'
  },
  {
    category: 'calendars',
    resource: 'calendars',
    url: 'https://marketplace.gohighlevel.com/docs/ghl/calendars/calendars',
    accessLevel: 'location',
    tokenTypes: ['oauth_access_token', 'private_integration_token'],
    scopeHint: 'calendars.*',
    status: 'supported'
  },
  {
    category: 'locations',
    resource: 'sub_accounts',
    url: 'https://marketplace.gohighlevel.com/docs/ghl/locations/sub-account-formerly-location-api',
    accessLevel: 'agency',
    tokenTypes: ['oauth_access_token', 'private_integration_token'],
    scopeHint: 'locations.*',
    status: 'supported'
  },
  {
    category: 'opportunities',
    resource: 'opportunities',
    url: 'https://marketplace.gohighlevel.com/docs/ghl/opportunities/opportunities',
    accessLevel: 'location',
    tokenTypes: ['oauth_access_token', 'private_integration_token'],
    scopeHint: 'opportunities.*',
    status: 'supported'
  },
  {
    category: 'payments',
    resource: 'payments',
    url: 'https://marketplace.gohighlevel.com/docs/ghl/payments/payments-api',
    accessLevel: 'location',
    tokenTypes: ['oauth_access_token', 'private_integration_token'],
    scopeHint: 'payments/*',
    status: 'supported'
  },
  {
    category: 'products',
    resource: 'products',
    url: 'https://marketplace.gohighlevel.com/docs/ghl/products/products-api',
    accessLevel: 'location',
    tokenTypes: ['oauth_access_token', 'private_integration_token'],
    scopeHint: 'products.*',
    status: 'supported'
  },
  {
    category: 'invoices',
    resource: 'invoices',
    url: 'https://marketplace.gohighlevel.com/docs/ghl/invoices/invoice-api',
    accessLevel: 'location',
    tokenTypes: ['oauth_access_token', 'private_integration_token'],
    scopeHint: 'invoices.*',
    status: 'supported'
  },
  {
    category: 'users',
    resource: 'users',
    url: 'https://marketplace.gohighlevel.com/docs/ghl/users/users-api',
    accessLevel: 'both',
    tokenTypes: ['oauth_access_token', 'private_integration_token'],
    scopeHint: 'users.*',
    status: 'supported'
  },
  {
    category: 'snapshots',
    resource: 'snapshots',
    url: 'https://marketplace.gohighlevel.com/docs/ghl/snapshots/snapshots-api',
    accessLevel: 'agency',
    tokenTypes: ['oauth_access_token', 'private_integration_token'],
    scopeHint: 'snapshots.*',
    status: 'supported'
  },
  {
    category: 'social_planner',
    resource: 'social_media_posting',
    url: 'https://marketplace.gohighlevel.com/docs/ghl/social-planner/social-media-posting-api',
    accessLevel: 'location',
    tokenTypes: ['oauth_access_token', 'private_integration_token'],
    scopeHint: 'socialplanner/*',
    status: 'supported'
  },
  {
    category: 'voice_ai',
    resource: 'voice_ai',
    url: 'https://marketplace.gohighlevel.com/docs/ghl/voice-ai/voice-ai-api',
    accessLevel: 'location',
    tokenTypes: ['oauth_access_token', 'private_integration_token'],
    scopeHint: 'voice-ai-*',
    status: 'supported'
  },
  {
    category: 'phone_system',
    resource: 'phone_system',
    url: 'https://marketplace.gohighlevel.com/docs/ghl/phone-system/phone-system-api',
    accessLevel: 'location',
    tokenTypes: ['oauth_access_token', 'private_integration_token'],
    scopeHint: 'phonenumbers.read',
    status: 'conditional'
  },
  {
    category: 'workflows',
    resource: 'workflows',
    url: 'https://marketplace.gohighlevel.com/docs/ghl/workflows/workflows-api',
    accessLevel: 'location',
    tokenTypes: ['oauth_access_token', 'private_integration_token'],
    scopeHint: 'workflows.readonly',
    status: 'conditional'
  },
  {
    category: 'webhooks',
    resource: 'event_catalog',
    url: 'https://marketplace.gohighlevel.com/docs/category/webhook/',
    accessLevel: 'agency',
    tokenTypes: ['oauth_access_token'],
    scopeHint: 'webhook',
    status: 'supported'
  },
  {
    category: 'oauth',
    resource: 'faq',
    url: 'https://marketplace.gohighlevel.com/docs/oauth/Faqs',
    accessLevel: 'agency',
    tokenTypes: ['oauth_access_token'],
    scopeHint: 'oauth.*',
    status: 'supported'
  }
];

export const VERIFIED_ENDPOINT_SEEDS = [
  ['contacts', 'GET', '/contacts/:contactId', 'location', 'contacts.readonly'],
  ['contacts', 'POST', '/contacts/', 'location', 'contacts.write'],
  ['contacts', 'GET', '/contacts/', 'location', 'contacts.readonly'],
  ['conversations', 'GET', '/conversations/:conversationsId', 'location', 'conversations.readonly'],
  ['conversations', 'POST', '/conversations/', 'location', 'conversations.write'],
  ['conversations', 'POST', '/conversations/messages', 'location', 'conversations/message.write'],
  ['calendars', 'GET', '/calendars/', 'location', 'calendars.readonly'],
  ['calendars', 'POST', '/calendars/', 'location', 'calendars.write'],
  ['calendars', 'GET', '/calendars/events', 'location', 'calendars/events.readonly'],
  ['calendars', 'POST', '/calendars/events/appointments', 'location', 'calendars/events.write'],
  ['opportunities', 'GET', '/opportunities/:id', 'location', 'opportunities.readonly'],
  ['opportunities', 'POST', '/opportunities', 'location', 'opportunities.write'],
  ['opportunities', 'PUT', '/opportunities/:id', 'location', 'opportunities.write'],
  ['opportunities', 'DELETE', '/opportunities/:id', 'location', 'opportunities.write'],
  ['locations', 'GET', '/locations/:locationId', 'agency', 'locations.readonly'],
  ['locations', 'GET', '/locations/search', 'agency', 'locations.readonly'],
  ['locations', 'POST', '/locations/', 'agency', 'locations.write'],
  ['locations', 'PUT', '/locations/:locationId', 'agency', 'locations.write'],
  ['locations', 'DELETE', '/locations/:locationId', 'agency', 'locations.write'],
  ['users', 'GET', '/users/', 'both', 'users.readonly'],
  ['users', 'POST', '/users/', 'both', 'users.write'],
  ['users', 'PUT', '/users/:userId', 'both', 'users.write'],
  ['users', 'DELETE', '/users/:userId', 'both', 'users.write'],
  ['invoices', 'GET', '/invoices/', 'location', 'invoices.readonly'],
  ['invoices', 'POST', '/invoices', 'location', 'invoices.write'],
  ['invoices', 'POST', '/invoices/:invoiceId/send', 'location', 'invoices.write'],
  ['invoices', 'POST', '/invoices/:invoiceId/void', 'location', 'invoices.write'],
  ['payments', 'GET', '/payments/orders/', 'location', 'payments/orders.readonly'],
  ['payments', 'GET', '/payments/transactions/', 'location', 'payments/transactions.readonly'],
  ['payments', 'GET', '/payments/subscriptions/', 'location', 'payments/subscriptions.readonly'],
  ['products', 'GET', '/products/', 'location', 'products.readonly'],
  ['products', 'POST', '/products/', 'location', 'products.write'],
  ['products', 'POST', '/products/:productId/price/', 'location', 'products/prices.write'],
  ['snapshots', 'GET', '/snapshots', 'agency', 'snapshots.readonly'],
  ['snapshots', 'POST', '/snapshots/share/link', 'agency', 'snapshots.write'],
  ['social_planner', 'GET', '/social-media-posting/:locationId/accounts', 'location', 'socialplanner/accounts.readonly'],
  ['social_planner', 'POST', '/social-media-posting/:locationId/posts', 'location', 'socialplanner/posts.write'],
  ['voice_ai', 'GET', '/voice-ai/dashboard/call-logs', 'location', 'voice-ai-dashboard.readonly'],
  ['voice_ai', 'GET', '/voice-ai/agents', 'location', 'voice-ai-agents.readonly'],
  ['voice_ai', 'POST', '/voice-ai/agents', 'location', 'voice-ai-agents.write'],
  ['workflows', 'GET', '/workflows/', 'location', 'workflows.readonly'],
  ['oauth', 'POST', '/oauth/locationToken', 'agency', 'oauth.*'],
  ['oauth', 'GET', '/oauth/installedLocations', 'agency', 'oauth.*']
].map(([category, method, path, accessLevel, scope]) => ({
  category,
  method,
  path,
  accessLevel,
  scope,
  tokenTypes: ['oauth_access_token', 'private_integration_token'],
  status: 'supported'
}));

export const NOT_PUBLICLY_API_SUPPORTED = [
  'payment.failed webhook',
  'invoice.overdue webhook',
  'form.submitted webhook',
  'survey.submitted webhook',
  'missed.call webhook',
  'generic workflow.event webhook',
  'undocumented cross-location bulk operations',
  'undocumented UI-only features'
];
