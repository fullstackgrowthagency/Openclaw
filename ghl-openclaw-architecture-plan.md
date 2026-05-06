# OpenClaw HighLevel / GoHighLevel Multi-Agent Architecture Plan

## 1. Objective Summary

Build an OpenClaw multi-agent control plane for HighLevel API 2.0 that:

- uses official HighLevel docs as the source of truth,
- separates agency and sub-account authority,
- supports OAuth 2.0 and Private Integration Tokens (PITs),
- performs documented CRUD only,
- routes event work through verified webhooks first and polling second,
- enforces least privilege, approval gates, auditability, and location isolation,
- and can refresh its own capability registry when HighLevel docs, scopes, endpoints, or webhook coverage change.

The required agent IDs are:

- `ghl-agency-lead`
- `ghl-sub-account-agent-{locationId}`
- `ghl-contacts-agent-{locationId}`
- `ghl-sales-pipeline-agent-{locationId}`
- `ghl-conversations-agent-{locationId}`
- `ghl-calendar-agent-{locationId}`
- `ghl-workflow-agent-{locationId}`
- `ghl-payments-agent-{locationId}`
- `ghl-marketing-agent-{locationId}`
- `ghl-snapshot-agent-{locationId}`
- `ghl-voice-ai-agent-{locationId}`
- `ghl-reporting-agent-{locationId}`
- `ghl-compliance-audit-agent-{locationId}`

## 2. Documentation Findings

### 2.1 Verified sources

Validated against current official docs under `https://marketplace.gohighlevel.com/docs/`, including:

- OAuth 2.0
- Private Integrations
- Scopes
- Webhook Integration Guide
- Webhook Logs Dashboard
- Webhook event catalog
- Locations, Contacts, Conversations, Calendars, Opportunities, Payments, Products, Invoices, Users, Snapshots, Social Planner, Voice AI, Phone System, Workflows

### 2.2 Current platform conclusions

- Public API family is HighLevel API 2.0.
- Current docs version shown across inspected pages is `2023-02-21`.
- Some examples still show `Version: 2021-07-28` headers. Implementation should pin the documented header per endpoint example while treating the API family as V2.
- OAuth uses Authorization Code Grant.
- Access tokens expire in about 24 hours.
- Refresh tokens are valid for 1 year or until used, and rotate on use.
- Agency tokens can mint location tokens via documented OAuth location token exchange.
- PITs exist for both agency and sub-account contexts, are static until rotated, and should be rotated every 90 days per docs guidance.
- V2 rate limits documented in FAQ are per marketplace app per resource: 100 requests per 10 seconds and 200,000 requests per day.
- Webhooks now prefer `X-GHL-Signature` using Ed25519. Legacy `X-WH-Signature` RSA is deprecated July 1, 2026.
- HighLevel retries all non-2xx webhook failures. `429` uses 10 minute retry cadence, other non-2xx and transport failures retry with exponential backoff for up to 3 days.
- Webhook circuit breaker can pause delivery if a high-volume endpoint stays below 90 percent success over repeated 3 day checks.

### 2.3 Important documented boundaries

- Agency-only operations are explicitly documented for areas like sub-account create/update/delete, snapshots, some company/custom-menu/company-marketplace flows.
- Most operational CRM objects are sub-account scoped.
- Webhook subscriptions are configured on the OAuth app, not per agent.
- Scopes are locked once an app version is live; new scope sets require a new draft.
- Webhook URLs and subscribed events can be modified after the app is live.

### 2.4 Not Publicly API-Supported findings

Treat these as unsupported unless re-verified at runtime:

- Any GHL UI feature with no official endpoint page or scope entry.
- Direct webhook events for `payment.failed`, `invoice.overdue`, `form.submitted`, `survey.submitted`, `missed.call`, and generic `workflow.event` were not found in the verified webhook catalog. Use documented polling or adjacent documented events instead.
- Any cross-location bulk operator endpoint not explicitly listed in docs.
- Any destructive workflow/template/global asset mutation not explicitly documented.

## 3. Capability Map

### 3.1 Capability registry design

At startup and on scheduled refresh, `ghl-agency-lead` builds a live registry from:

- docs version marker,
- scope table,
- module overview pages,
- child endpoint pages,
- webhook catalog,
- webhook guide,
- FAQ rate-limit guidance.

Each registry record should contain:

- `category`
- `resource`
- `doc_url`
- `path`
- `method`
- `api_version_header`
- `auth_methods`
- `token_types`
- `scope`
- `access_level` (`agency`, `location`, `both`)
- `crud_support`
- `webhook_events`
- `request_schema_ref`
- `response_schema_ref`
- `pagination_mode`
- `rate_limit_class`
- `plan_or_feature_notes`
- `status` (`supported`, `read_only`, `conditional`, `not_publicly_api_supported`)
- `last_verified_at`
- `doc_fingerprint`

### 3.2 Dynamic capability summary by module

| API Area | Publicly Documented | Primary Scope Family | Token Context | Webhook Coverage | Notes |
|---|---|---:|---|---|---|
| OAuth 2.0 | Yes | `oauth.*` | Agency | App install/uninstall related | Use for multi-location installs |
| Private Integration Tokens | Yes | UI-managed | Agency or Location | No direct PIT events | Static until rotated |
| Ad Manager / Ad Publishing | Yes | `adPublishing.*` | Location | None verified | Large surface, location only |
| Affiliate Manager | Yes, module present | Runtime crawl required | Likely location or agency by child docs | None verified | Enable only after child endpoint crawl |
| AI Agent Studio | Yes, module present | Runtime crawl required | Runtime crawl required | None verified | Do not assume writes until crawled |
| Associations | Yes | `associations.*` | Location | `Association*`, `Relation*` | Supported |
| Blogs | Yes | `blogs.*` | Location | None verified | Partial docs visible in scopes |
| Brand Boards | Yes, module present | Runtime crawl required | Runtime crawl required | None verified | Crawl before enablement |
| Business | Yes | `businesses.*` | Location | None verified | Supported |
| Calendars | Yes | `calendars.*` | Location | `AppointmentCreate`, `AppointmentUpdate`, `AppointmentDelete` | Supported |
| Calendar Events | Yes | `calendars/events.*` | Location | Appointment events | Supported |
| Campaigns | Yes | `campaigns.readonly` | Location | `CampaignStatusUpdate` | Read-only verified in scopes snapshot |
| Companies | Yes | `companies.readonly` | Agency | None verified | Read-only verified |
| Contacts | Yes | `contacts.*` | Location | `Contact*`, `Note*`, `Task*`, `ContactDndUpdate`, `ContactTagUpdate` | Strong coverage |
| Contact Tags / Notes / Tasks | Yes | `contacts.*` | Location | Tag/note/task events | Strong coverage |
| Contact Custom Fields / Values | Via location custom fields/custom values and custom fields v2 | `locations/customFields.*`, `locations/customValues.*` | Location | None verified | Separate location config vs record data |
| Conversation AI | Yes | Runtime crawl required | Location | None verified | Enable cautiously |
| Conversations | Yes | `conversations.*`, `conversations/message.*` | Location | `ConversationUnreadWebhook`, `ConversationUpdate`, `InboundMessage`, `OutboundMessage`, `ProviderOutboundMessage` | Strong coverage |
| SMS / Email / Calls via conversations | Partially | `conversations/message.*`, `emails.*`, phone scopes | Location | Inbound/outbound message events | Model as channel actions under conversation ownership |
| LC Email | Yes, module present | Runtime crawl required | Runtime crawl required | `LCEmailStats` | Crawl before writes |
| Courses | Yes | `courses.write` seen | Location | None verified | Partial verified |
| Custom Fields V2 | Yes, module present | Runtime crawl required | Runtime crawl required | None verified | Crawl before writes |
| Custom Menus | Yes | `custom-menu-link.*` | Agency | None verified | Agency-managed |
| Forms | Yes | `forms.*` | Location | No form submission webhook verified | Poll submissions |
| Funnels | Yes | `funnels.*` | Location | None verified | Partial read/write verified |
| Invoices | Yes | `invoices.*` | Location | `InvoiceCreate`, `InvoiceUpdate`, `InvoiceDelete`, `InvoicePaid`, `InvoicePartiallyPaid`, `InvoiceSent`, `InvoiceVoid` | Strong coverage |
| Knowledge Base | Yes, module present | Runtime crawl required | Runtime crawl required | None verified | Crawl before writes |
| Trigger Links | Yes | `links.*` | Location | None verified | Supported |
| Sub-Accounts / Locations | Yes | `locations.*` | Agency for write, both for read | `LocationCreate`, `LocationUpdate` | Core agency control plane |
| Location Templates / Recurring Tasks / Conversation Channels / Tags / Permissions / Timezones | Yes | `locations/*` scope families | Mostly Location | None or `LocationUpdate` | Supported where child docs exist |
| Media Storage | Yes | `medias.*` | Location | None verified | Supported |
| Custom Objects / Schemas / Records | Yes | `objects/schema.*`, `objects/record.*` | Location | `ObjectSchemaCreate`, `ObjectSchemaUpdate`, `Record*` | Strong coverage |
| Opportunities / Pipelines | Yes | `opportunities.*` | Location | `Opportunity*` | Strong coverage |
| Payments / Orders / Transactions / Subscriptions / Coupons / Custom Provider | Yes | `payments/*` | Location | `OrderCreate`, `OrderStatusUpdate` | No verified payment.failed webhook |
| Products / Prices / Collections / Reviews | Yes | `products.*` | Location | `Product*`, `Price*` | Strong coverage |
| Proposals | Yes | `documents_contracts*` | Agency or Location | None verified | Send/read patterns verified |
| SaaS | Yes | `saas/*` | Agency and sometimes both | `PlanChange`, `SaaSPlanCreate` | Conditional by plan |
| Snapshots | Yes | `snapshots.*` | Agency | None verified | Agency-owned |
| Social Planner | Yes | `socialplanner/*` | Location | None verified | Supported |
| Store | Yes, module present | Runtime crawl required | Runtime crawl required | None verified | Crawl before writes |
| Surveys | Yes | `surveys.readonly` verified | Location | No survey submission webhook verified | Poll submissions |
| Users | Yes | `users.*` | Agency and Location | `UserCreate`, `UserUpdate`, `UserDelete` | Supported |
| Phone System | Yes | `phonenumbers.read`, `numberpools.read` and child docs | Location and some agency scope | None verified | Read-first by default |
| Voice AI | Yes | `voice-ai-*` | Location | `VoiceAiCallEnd` | Strong enough for reporting and controlled writes |
| Workflows | Yes | `workflows.readonly` verified | Location | No generic workflow webhook verified | Read-only unless child docs confirm writes |
| Webhook Integration Guide / Logs | Yes | OAuth app config | Agency app owner | Webhook dashboard only | Operational, not agent business data |
| Marketplace app install/uninstall events | Yes | OAuth app events | Agency app owner | `AppInstall`, `AppUninstall`, `AppUpdate` | Critical for lifecycle handling |

### 3.3 Minimum exact endpoint families already verified

- `GET /contacts/:contactId`
- `POST /contacts/`
- `GET /contacts/`
- `GET /contacts/:contactId/tasks`
- `POST /contacts/:contactId/tasks`
- `POST /contacts/:contactId/notes`
- `POST /contacts/:contactId/tags`
- `GET /conversations/:conversationsId`
- `POST /conversations/`
- `PUT /conversations/:conversationsId`
- `DELETE /conversations/:conversationsId`
- `POST /conversations/messages`
- `GET /calendars/`
- `POST /calendars/`
- `GET /calendars/events`
- `POST /calendars/events/appointments`
- `GET /opportunities/:id`
- `POST /opportunities`
- `PUT /opportunities/:id`
- `DELETE /opportunities/:id`
- `GET /locations/:locationId`
- `GET /locations/search`
- `POST /locations/`
- `PUT /locations/:locationId`
- `DELETE /locations/:locationId`
- `GET /users/`
- `POST /users/`
- `PUT /users/:userId`
- `DELETE /users/:userId`
- `GET /invoices/`
- `POST /invoices`
- `POST /invoices/:invoiceId/send`
- `POST /invoices/:invoiceId/void`
- `GET /payments/orders/`
- `GET /payments/transactions/`
- `GET /payments/subscriptions/`
- `GET /products/`
- `POST /products/`
- `POST /products/:productId/price/`
- `GET /snapshots`
- `POST /snapshots/share/link`
- `GET /social-media-posting/:locationId/accounts`
- `POST /social-media-posting/:locationId/posts`
- `GET /voice-ai/dashboard/call-logs`
- `GET /voice-ai/agents`
- `POST /voice-ai/agents`
- `GET /workflows/`
- `POST /oauth/locationToken`
- `GET /oauth/installedLocations`

### 3.4 Capability enablement policy

- Enable writes only when both a child endpoint page and scope mapping are present.
- If a module is visible only as an overview page, mark it `conditional` and read-only until crawler verifies child endpoints.
- If an event is not in the webhook catalog, do not synthesize it. Use polling or derived logic.

## 4. Concise Implementation Rationale

- Put all doc discovery under `ghl-agency-lead` so the rest of the system does not guess.
- Use agency token only for agency tasks and for minting location tokens. Day-to-day work should run on location-scoped credentials.
- Make sub-account agents durable and specialist agents stateless workers under the location parent.
- Prefer webhook-triggered work for freshness and rate efficiency.
- Treat payments, invoice voids, deletes, scope changes, and multi-location actions as approval-gated because the docs support them but they are high-risk.
- Keep unsupported features blocked rather than approximated. That avoids silent policy drift and broken automations.

## 5. Agent Architecture

### 5.1 Topology

```text
ghl-agency-lead
├── capability-registry service
├── auth broker
├── webhook ingress + event router
├── approval service
├── audit/log service
├── rate-limit coordinator
├── doc drift monitor
└── one location tree per installed sub-account
    └── ghl-sub-account-agent-{locationId}
        ├── ghl-contacts-agent-{locationId}
        ├── ghl-sales-pipeline-agent-{locationId}
        ├── ghl-conversations-agent-{locationId}
        ├── ghl-calendar-agent-{locationId}
        ├── ghl-workflow-agent-{locationId}
        ├── ghl-payments-agent-{locationId}
        ├── ghl-marketing-agent-{locationId}
        ├── ghl-snapshot-agent-{locationId}   (read-only at location unless approved path exists)
        ├── ghl-voice-ai-agent-{locationId}
        ├── ghl-reporting-agent-{locationId}
        └── ghl-compliance-audit-agent-{locationId}
```

### 5.2 Agency lead responsibilities

`ghl-agency-lead` owns:

- capability discovery and registry refresh,
- OAuth app lifecycle and token exchange,
- PIT registration metadata,
- location-agent provisioning and revocation,
- approval workflows,
- cross-location policy enforcement,
- webhook config governance,
- rate-limit arbitration,
- global reporting,
- documentation drift detection,
- escalation intake.

### 5.3 Sub-account parent responsibilities

`ghl-sub-account-agent-{locationId}` owns:

- location boundary enforcement,
- task-pack orchestration for that location,
- specialist delegation,
- local audit mirror,
- event-to-specialist routing,
- location health reporting,
- safe fallback polling.

### 5.4 Specialist responsibilities

- `ghl-contacts-agent-{locationId}`: contacts, tasks, notes, tags, dedupe, lifecycle.
- `ghl-sales-pipeline-agent-{locationId}`: opportunities, pipelines, stale deal logic, forecast.
- `ghl-conversations-agent-{locationId}`: conversations, channel messages, assignment, SLA alerts.
- `ghl-calendar-agent-{locationId}`: calendars, appointments, blocks, reminders, no-show handling.
- `ghl-workflow-agent-{locationId}`: documented workflow reads, supported triggers/enrollments only, automation QA.
- `ghl-payments-agent-{locationId}`: products, prices, orders, transactions, subscriptions, invoices, revenue alerts.
- `ghl-marketing-agent-{locationId}`: forms, surveys, trigger links, funnels, social planner, email builder where documented.
- `ghl-snapshot-agent-{locationId}`: snapshot validation and apply tracking, but agency approval required for global snapshot operations.
- `ghl-voice-ai-agent-{locationId}`: voice AI dashboards, agents, actions, post-call routing.
- `ghl-reporting-agent-{locationId}`: KPI aggregation and scheduled summaries.
- `ghl-compliance-audit-agent-{locationId}`: permission drift, webhook health, destructive action review, token-scope mismatch detection.

## 6. Authentication + Credential Strategy

### 6.1 Preferred model

- Primary: OAuth 2.0 for production multi-location deployments.
- Secondary: PIT for internal single-agency or single-location deployments, or as controlled fallback.

### 6.2 OAuth strategy

- Register a HighLevel Marketplace app.
- Prefer target user `Sub-account` unless agency-only install is required by the deployment.
- Configure install visibility based on distribution model.
- Request least-privilege scopes by task-pack bundle, not by "everything".
- Store `client_id`, `client_secret`, access token, refresh token, token metadata, and install context in encrypted storage.
- Refresh proactively at T-15 minutes and reactively on expired-token responses.
- Rotate refresh-token record on each successful refresh because the refresh token changes when used.
- Use documented `POST /oauth/locationToken` to mint location tokens from agency token when the app pattern requires that flow.
- Handle `AppInstall`, `AppUninstall`, and `AppUpdate` webhooks as lifecycle control events.

### 6.3 PIT strategy

- Store PITs in encrypted vault only.
- Maintain explicit mapping to `agencyId` or `locationId`.
- Record granted scopes from setup time.
- Enforce internal software policy even if the PIT is broader than the agent policy.
- Rotate PITs on a 90 day standard unless stricter policy applies.
- Support dual-token overlap during rotation.

### 6.4 Credential brokerage

Only `ghl-agency-lead` and the auth broker can access raw credentials. All other agents receive ephemeral request grants:

- `credential_ref`
- `token_type`
- `location_id`
- `scope_set`
- `expires_at`
- `approval_context`

Never pass raw token strings through prompts, agent memory, or logs.

## 7. Permission + Scope Strategy

### 7.1 RBAC model

Every request must pass:

- `agent_id` check,
- `role` check,
- `location_id` boundary check,
- `endpoint capability` check,
- `scope` check,
- `token type` check,
- `approval requirement` check,
- `feature status` check (`supported`, `conditional`, blocked unsupported).

### 7.2 Role templates

- `agency_admin_orchestrator`: only `ghl-agency-lead`
- `location_operator`: `ghl-sub-account-agent-{locationId}`
- `location_specialist_contacts`
- `location_specialist_sales`
- `location_specialist_conversations`
- `location_specialist_calendar`
- `location_specialist_workflow`
- `location_specialist_payments`
- `location_specialist_marketing`
- `location_specialist_snapshot`
- `location_specialist_voice_ai`
- `location_specialist_reporting`
- `location_specialist_compliance`

### 7.3 Destructive approval policy

Approval required for:

- deletes of contacts, opportunities, conversations, products, prices, invoices, users, locations,
- voids, cancellations, subscription terminations, refunds,
- workflow disable/delete,
- snapshot deletion/application affecting multiple locations,
- OAuth scope changes,
- PIT replacement or rotation cutover,
- any multi-location action,
- any bulk write above configured threshold.

### 7.4 Permission drift detection

`ghl-compliance-audit-agent-{locationId}` compares:

- actual token scopes,
- assigned role scopes,
- live capability requirements,
- last approved permission baseline.

Any excess or missing scope raises an incident to `ghl-agency-lead`.

## 8. Endpoint-to-Agent Responsibility Matrix

| API Area | Example Resources | Owner Agent | Token Type | Scope Type | Allowed Actions | Restricted Actions |
|---|---|---|---|---|---|---|
| OAuth + install management | `/oauth/token`, `/oauth/locationToken`, install lifecycle | `ghl-agency-lead` | OAuth agency | `oauth.*` | exchange, refresh, mint location token, installation mapping | credential disclosure, ad hoc scope expansion |
| Sub-Accounts / Locations | `/locations`, `/locations/search` | `ghl-agency-lead` | Agency OAuth or Agency PIT | `locations.*` | create, read, update, search | delete requires approval |
| Location config | `/locations/:id/customFields`, `/customValues`, `/tags`, `/permissions`, `/timeZones` | `ghl-sub-account-agent-{locationId}` with specialists | Location OAuth/PIT | `locations/*` | read, create, update as documented | cross-location writes, destructive deletes without approval |
| Contacts | `/contacts`, `/contacts/search`, `/contacts/:id/tasks`, `/notes`, `/tags` | `ghl-contacts-agent-{locationId}` | Location OAuth/PIT | `contacts.*` | CRUD, tag, note, task, workflow/campaign attach where documented | bulk delete requires approval |
| Companies | `/companies/:companyId` | `ghl-agency-lead` | Agency OAuth/PIT | `companies.readonly` | read | mutation unless later documented |
| Opportunities / Pipelines | `/opportunities`, `/opportunities/pipelines` | `ghl-sales-pipeline-agent-{locationId}` | Location OAuth/PIT | `opportunities.*` | CRUD, status/stage updates, reporting | delete requires approval |
| Conversations | `/conversations`, `/conversations/search`, `/conversations/messages` | `ghl-conversations-agent-{locationId}` | Location OAuth/PIT | `conversations.*`, `conversations/message.*` | create, read, update, send documented messages, assignment | delete requires approval, unapproved outbound automation blocked |
| Calendars / Events | `/calendars`, `/calendars/events`, `/blocked-slots` | `ghl-calendar-agent-{locationId}` | Location OAuth/PIT | `calendars.*`, `calendars/events.*` | CRUD appointments/calendars as documented | bulk cancel/delete requires approval |
| Campaigns | `/campaigns/` | `ghl-workflow-agent-{locationId}` | Location OAuth/PIT | `campaigns.readonly` | read/report | write until documented |
| Workflows | `/workflows/` and child endpoints if crawled | `ghl-workflow-agent-{locationId}` | Location OAuth/PIT | `workflows.*` | read, supported enroll/trigger if later verified | disable/delete requires approval |
| Forms / Surveys | `/forms`, `/forms/submissions`, `/surveys`, `/surveys/submissions` | `ghl-marketing-agent-{locationId}` | Location OAuth/PIT | `forms.*`, `surveys.*` | read, submission analysis, file upload where documented | delete or mutation only if child docs verify |
| Funnels / Trigger Links | `/funnels/*`, `/links/*` | `ghl-marketing-agent-{locationId}` | Location OAuth/PIT | `funnels.*`, `links.*` | read, create/update redirects and links | delete requires approval |
| Payments | `/payments/orders`, `/transactions`, `/subscriptions`, `/coupon` | `ghl-payments-agent-{locationId}` | Location OAuth/PIT | `payments/*` | read/report, supported fulfillments and coupon ops | cancellation/refund-like ops require approval |
| Products / Prices | `/products`, `/products/:id/price` | `ghl-payments-agent-{locationId}` | Location OAuth/PIT | `products.*` | CRUD as documented | deletes and bulk updates require approval |
| Invoices / Estimates / Templates / Schedule | `/invoices*` | `ghl-payments-agent-{locationId}` | Location OAuth/PIT | `invoices.*` | create, update, send, record payment, scheduling | void/delete/cancel requires approval |
| Snapshots | `/snapshots` | `ghl-agency-lead` and `ghl-snapshot-agent-{locationId}` for validation | Agency OAuth/PIT | `snapshots.*` | read, share/apply per policy | global delete/apply requires approval |
| Users | `/users` | `ghl-agency-lead` or authorized `ghl-sub-account-agent-{locationId}` | Agency or Location token | `users.*` | invite, read, update | remove admin/user delete requires approval |
| Media Storage | `/medias/*` | `ghl-marketing-agent-{locationId}` | Location OAuth/PIT | `medias.*` | upload/read/delete by policy | delete requires approval |
| Social Planner | `/social-media-posting/*` | `ghl-marketing-agent-{locationId}` | Location OAuth/PIT | `socialplanner/*` | connect accounts, create/edit posts, stats | publishing without approval optional policy gate |
| Voice AI | `/voice-ai/dashboard/*`, `/voice-ai/agents`, `/voice-ai/actions` | `ghl-voice-ai-agent-{locationId}` | Location OAuth/PIT | `voice-ai-*` | read logs, manage documented agents/actions | deletes require approval |
| Phone System | `/phone-system/*` | `ghl-voice-ai-agent-{locationId}` or `ghl-reporting-agent-{locationId}` | Location OAuth/PIT | phone scopes | read inventories/pools, report | provisioning/mutations only if later verified |
| Objects / Associations | `/objects/*`, `/associations/*` | depends on business workflow, default `ghl-sub-account-agent-{locationId}` | Location OAuth/PIT | `objects/*`, `associations.*` | CRUD records and relations | destructive schema changes require approval |
| Custom Menus | `/custom-menus/*` | `ghl-agency-lead` | Agency OAuth/PIT | `custom-menu-link.*` | CRUD | delete requires approval |
| Proposals | `/proposals/*` | `ghl-marketing-agent-{locationId}` or `ghl-agency-lead` | Agency or Location token | proposals scopes | read/send links | destructive edits only if documented |
| Webhook operations | app webhook config + dashboard | `ghl-agency-lead` and `ghl-compliance-audit-agent-{locationId}` | OAuth app context | app config + event scopes | configure, monitor, analyze | disabling events requires approval |

## 9. Event Monitoring + Webhook Strategy

### 9.1 Ingress model

One shared webhook ingress owned by `ghl-agency-lead`:

1. receive raw request,
2. verify `X-GHL-Signature` first, else legacy signature during transition,
3. compute payload hash,
4. persist raw payload in secure event store,
5. dedupe by `webhookId` + payload hash,
6. resolve `locationId`, `companyId`, `event type`,
7. route to owning location tree,
8. ACK fast with 2xx,
9. process downstream asynchronously.

### 9.2 Event routing map

- `INSTALL`, `AppInstall`, `AppUpdate`, `AppUninstall`, `PlanChange`, `LocationCreate`, `LocationUpdate` → `ghl-agency-lead`
- `Contact*`, `Note*`, `Task*`, `ContactTagUpdate`, `ContactDndUpdate` → `ghl-contacts-agent-{locationId}`
- `Opportunity*` → `ghl-sales-pipeline-agent-{locationId}`
- `ConversationUnreadWebhook`, `ConversationUpdate`, `InboundMessage`, `OutboundMessage`, `ProviderOutboundMessage` → `ghl-conversations-agent-{locationId}`
- `AppointmentCreate`, `AppointmentUpdate`, `AppointmentDelete` → `ghl-calendar-agent-{locationId}`
- `Invoice*`, `OrderCreate`, `OrderStatusUpdate`, `Price*`, `Product*` → `ghl-payments-agent-{locationId}`
- `User*` → location parent plus compliance agent
- `ObjectSchema*`, `Record*`, `Association*`, `Relation*` → location parent or specialist pack based on object mapping
- `VoiceAiCallEnd` → `ghl-voice-ai-agent-{locationId}`
- `LCEmailStats` → `ghl-marketing-agent-{locationId}` plus reporting agent

### 9.3 Polling fallback

Polling is enabled only for documented resources with weak webhook coverage:

- form submissions,
- survey submissions,
- workflow status checks,
- payment failures inferred from transactions/subscriptions/invoices,
- overdue invoices,
- campaign/reporting freshness,
- webhook outage backfill windows.

### 9.4 Webhook health

Track:

- total events,
- total attempts,
- consumer errors,
- retry depth,
- per-event failure rate,
- signature failures,
- duplicate rate,
- circuit-breaker risk score.

Mirror HighLevel dashboard concepts locally so OpenClaw can alert before GHL pauses deliveries.

## 10. Modular Task Packs

### 10.1 Pack schema

Every task pack must declare:

- `name`
- `purpose`
- `owner_agent_type`
- `required_scopes`
- `required_endpoints`
- `required_token_type`
- `input_schema`
- `output_schema`
- `trigger_events`
- `manual_command`
- `validation_rules`
- `error_rules`
- `logging_rules`
- `success_criteria`
- `rollback_plan`
- `permission_level`
- `approval_required`

### 10.2 Required packs

1. **Sub-Account Onboarding Pack**
   - owner: `ghl-agency-lead`
   - creates location record if documented, assigns location token, provisions `ghl-sub-account-agent-{locationId}`, applies approved snapshot, configures baseline location assets, validates config.

2. **Lead Management Pack**
   - owner: `ghl-contacts-agent-{locationId}`
   - contact create/update, notes, tasks, tags, dedupe, lifecycle routing, optional opportunity creation.

3. **Sales Pipeline Pack**
   - owner: `ghl-sales-pipeline-agent-{locationId}`
   - opportunity CRUD, stale deal alerts, follow-up tasks, close-rate reporting.

4. **Conversation Management Pack**
   - owner: `ghl-conversations-agent-{locationId}`
   - inbound monitor, SLA tracking, assignment, approved outbound messaging, escalation.

5. **Calendar + Appointment Pack**
   - owner: `ghl-calendar-agent-{locationId}`
   - appointment lifecycle, reminders, rescheduling, no-show reporting.

6. **Workflow + Automation QA Pack**
   - owner: `ghl-workflow-agent-{locationId}`
   - workflow inspection, supported trigger checks, dependency validation, automation QA.

7. **Payments + Invoicing Pack**
   - owner: `ghl-payments-agent-{locationId}`
   - products, prices, orders, subscriptions, transactions, invoices, overdue/failed state monitoring.

8. **Marketing Asset Pack**
   - owner: `ghl-marketing-agent-{locationId}`
   - forms, surveys, trigger links, funnels, social planner, campaign asset reporting.

9. **User + Permission Pack**
   - owner: `ghl-agency-lead` or location parent
   - user invite/update, approval-gated removals, role audits.

10. **Reporting Pack**
   - owner: `ghl-reporting-agent-{locationId}` and `ghl-agency-lead`
   - daily, weekly, monthly summaries and KPI exports.

11. **Snapshot + Template Pack**
   - owner: `ghl-agency-lead` plus `ghl-snapshot-agent-{locationId}`
   - apply/verify approved baseline assets.

12. **Compliance + Audit Pack**
   - owner: `ghl-compliance-audit-agent-{locationId}`
   - token audits, permission drift, destructive action traceability, webhook health, rate-limit risk.

## 11. Error Handling + Escalation Logic

### 11.1 Error classes

- auth failure
- expired token
- refresh failure
- missing scope
- permission denied
- capability missing
- unsupported endpoint
- schema mismatch
- validation failure
- duplicate resource
- not found
- cross-location attempt
- conflict / stale write
- rate limit exceeded
- timeout / transport failure
- GHL 5xx
- webhook verification failure
- webhook processing failure
- partial success
- destructive action blocked

### 11.2 Retry policy

- Retry idempotent reads and safe updates with exponential backoff.
- Respect GHL headers for API rate limits.
- For webhook processing, ACK quickly then retry internally from queue.
- Do not auto-retry deletes, cancellations, voids, or approval-gated mutations.
- Escalate after configurable threshold, default 3 failed attempts for API and 1 failure for high-risk actions.

### 11.3 Escalation targets

Escalate to `ghl-agency-lead` when:

- token refresh fails,
- app uninstall occurs,
- location token minting fails,
- a scope is missing,
- docs drift invalidates an action,
- webhook signature fails repeatedly,
- webhook success rate trends toward circuit-breaker threshold,
- rate-limit exhaustion is near,
- a cross-location attempt is detected,
- any destructive action is requested,
- payment cancellation/refund-like request appears,
- a bulk operation exceeds policy threshold.

## 12. Logging + Audit Trail Plan

### 12.1 Required log fields

- `timestamp`
- `correlation_id`
- `agent_id`
- `parent_agent_id`
- `requester_type`
- `requester_id`
- `agency_id`
- `company_id`
- `location_id`
- `task_pack`
- `trigger_source`
- `webhook_id`
- `event_type`
- `endpoint`
- `http_method`
- `capability_ref`
- `scope_required`
- `scope_granted`
- `token_type`
- `request_hash`
- `payload_redaction_profile`
- `status_code`
- `result_summary`
- `before_snapshot_ref`
- `after_snapshot_ref`
- `retry_count`
- `approval_status`
- `escalation_status`
- `error_class`
- `error_message_redacted`

### 12.2 Audit controls

- Append-only event log.
- Hash chain or signed batch manifest for tamper evidence.
- PII redaction templates per object type.
- Search by location, agent, event, status, date, correlation id.
- Separate secret store from operational logs.

## 13. Data Storage Plan

### 13.1 Core tables or collections

- `ghl_agencies`
- `ghl_locations`
- `ghl_agents`
- `ghl_roles`
- `ghl_role_bindings`
- `ghl_permissions`
- `ghl_tokens`
- `ghl_token_rotations`
- `ghl_scope_grants`
- `ghl_capability_registry`
- `ghl_doc_snapshots`
- `ghl_webhook_subscriptions`
- `ghl_webhook_events`
- `ghl_webhook_attempts`
- `ghl_action_logs`
- `ghl_task_packs`
- `ghl_task_runs`
- `ghl_incidents`
- `ghl_escalations`
- `ghl_approvals`
- `ghl_rate_limit_counters`
- `ghl_object_mappings`

### 13.2 Secret handling

- Encrypt token values at rest.
- Store only ciphertext plus KMS reference in database.
- Keep decrypted token lifetime in memory minimal.
- Never duplicate secrets into message payload archives.

## 14. Security + Compliance Controls

- least privilege scopes only,
- encrypted vault for client secrets, PITs, refresh tokens, access tokens,
- OAuth `state` validation and CSRF protection,
- strict redirect URI allowlist,
- signature verification using Ed25519 `X-GHL-Signature` first,
- request schema validation before every API call,
- response schema tolerance with drift alerts,
- location isolation enforced before token resolution,
- high-risk human approval gates,
- secret redaction in logs and prompts,
- no hardcoded credentials,
- PIT rotation support with dual-validity window tracking,
- doc-unavailable fallback to last verified snapshot or hard block,
- deny-by-default for unsupported modules or undocumented writes.

## 15. Implementation Steps

### Phase 1, Documentation Discovery

1. Build crawler for docs home, scopes, webhook catalog, module overviews, child endpoint pages.
2. Extract exact path, method, scope, token type, access level, and doc URL.
3. Hash snapshots and compare with previous crawl.
4. Mark changed endpoints and require revalidation before reuse.

### Phase 2, Auth System

1. Create Marketplace OAuth app.
2. Configure scopes bundles and redirect URIs.
3. Implement token exchange and refresh.
4. Implement agency-to-location token minting.
5. Add PIT registry and rotation metadata.
6. Add install/uninstall lifecycle processing.

### Phase 3, Control Plane

1. Create `ghl-agency-lead`.
2. Create auth broker, approval service, audit service, rate-limit service.
3. Create location parent agent template.
4. Create specialist agent templates.
5. Bind roles and scope policies.

### Phase 4, Event System

1. Deploy shared webhook ingress.
2. Add signature verification and dedupe.
3. Add event queue and dead-letter queue.
4. Add router from event to location and task pack.
5. Add webhook health dashboards and alerts.

### Phase 5, CRUD Adapters

1. Implement adapters for locations, contacts, conversations, calendars, opportunities, users, invoices, payments, products, social planner, snapshots, voice AI, objects, associations.
2. Add runtime feature flags per endpoint.
3. Add dry-run and approval hooks.

### Phase 6, Task Packs

1. Implement pack manifest format.
2. Ship required 12 packs.
3. Validate scope and endpoint availability before activation.
4. Run dry-run tests by module and by location.

### Phase 7, Reporting + Compliance

1. Build scheduled KPI rollups.
2. Build rate-limit dashboards.
3. Build token health dashboards.
4. Build permission drift and destructive-action reports.
5. Build doc-drift report.

## 16. Example Agent Definitions

### 16.1 `ghl-agency-lead`

```yaml
agent_id: ghl-agency-lead
scope_level: agency
auth:
  preferred: oauth_2
  fallback: private_integration_token
permissions:
  - oauth.manage
  - oauth.locationToken
  - locations.readonly
  - locations.write
  - users.readonly
  - users.write
  - snapshots.readonly
  - snapshots.write
  - custom-menu-link.readonly
  - custom-menu-link.write
  - companies.readonly
  - webhook.monitor
  - approvals.manage
  - audit.read
responsibilities:
  - maintain_capability_registry
  - provision_location_agents
  - route_webhooks
  - manage_credentials
  - enforce_rbac
  - supervise_rate_limits
  - approve_high_risk_actions
restricted_actions:
  - no_secret_disclosure
  - no_unapproved_destructive_action
  - no_undocumented_endpoint_use
```

### 16.2 `ghl-sub-account-agent-{locationId}`

```yaml
agent_id: ghl-sub-account-agent-{locationId}
scope_level: location
assigned_location_id: {locationId}
auth:
  preferred: oauth_location_token
  fallback: approved_location_pit
permissions:
  - location.boundary.enforce
  - contacts.dispatch
  - conversations.dispatch
  - calendars.dispatch
  - opportunities.dispatch
  - payments.dispatch
  - marketing.dispatch
  - reporting.dispatch
responsibilities:
  - route_location_events
  - coordinate_task_packs
  - maintain_location_audit_view
  - escalate_risk
restricted_actions:
  - no_cross_location_access
  - no_agency_settings_mutation
  - no_scope_changes
  - no_agent_creation
```

### 16.3 `ghl-contacts-agent-{locationId}`

```yaml
agent_id: ghl-contacts-agent-{locationId}
parent_agent: ghl-sub-account-agent-{locationId}
scope_level: location
permissions:
  - contacts.readonly
  - contacts.write
blocked_actions:
  - bulk_delete_without_approval
  - cross_location_search
  - credential_access
```

### 16.4 `ghl-payments-agent-{locationId}`

```yaml
agent_id: ghl-payments-agent-{locationId}
parent_agent: ghl-sub-account-agent-{locationId}
scope_level: location
permissions:
  - payments/orders.readonly
  - payments/transactions.readonly
  - payments/subscriptions.readonly
  - payments/coupons.readonly
  - payments/coupons.write
  - products.readonly
  - products.write
  - products/prices.readonly
  - products/prices.write
  - invoices.readonly
  - invoices.write
blocked_actions:
  - invoice_void_without_approval
  - delete_price_without_approval
  - cancellation_without_approval
  - cross_location_payment_access
```

## 17. Example Event Trigger Logic

### 17.1 New lead created

```yaml
event: ContactCreate
source: ghl_webhook
assigned_agent: ghl-contacts-agent-{locationId}
steps:
  - verify_signature
  - dedupe_by_webhook_id
  - verify_location_boundary
  - fetch_contact_if_needed
  - apply_default_tags
  - create_follow_up_task_if_rule_matches
  - create_opportunity_if_rule_matches_and_documented
  - log_result
failure_handling:
  - retry_safe_reads
  - escalate_missing_scope
  - escalate_schema_drift
```

### 17.2 Payment risk inferred

```yaml
event: inferred.payment_risk
source: polling_or_invoice_state
assigned_agent: ghl-payments-agent-{locationId}
steps:
  - verify_capability_registry_for_payments
  - fetch_invoice_subscription_transaction_records
  - detect_failed_or_overdue_state
  - create_internal_task
  - add_contact_note_if_documented_path_exists
  - notify_owner
  - log_result
failure_handling:
  - retry_reads_only
  - no_mutating_retry_without_approval
  - escalate_missing_payment_data
```

### 17.3 Sub-account created

```yaml
event: LocationCreate
source: ghl_webhook
assigned_agent: ghl-agency-lead
task_pack: sub_account_onboarding_pack
steps:
  - verify_agency_scope
  - fetch_location_details
  - provision_ghl_sub_account_agent
  - assign_default_specialists
  - mint_or_link_location_token
  - apply_approved_snapshot_if_configured
  - register_reporting_schedule
  - run_validation_checks
  - log_and_publish_onboarding_report
failure_handling:
  - rollback_partial_provisioning_if_safe
  - escalate_snapshot_failure
  - escalate_missing_scope
```

## 18. Edge Cases + Maintenance Plan

- **Docs drift:** freeze changed endpoints until crawler revalidates them.
- **Scope drift:** compare desired vs granted scopes daily.
- **Expired or revoked tokens:** refresh once, then quarantine affected agent.
- **App uninstall:** disable agents for that install context immediately.
- **Webhook duplication:** dedupe on `webhookId` and payload hash.
- **Webhook outage:** backfill via polling windows.
- **Rate-limit exhaustion:** queue low-priority jobs and reserve burst budget for interactive and webhook work.
- **Unsupported event asks:** translate to documented polling logic or block.
- **Cross-location leakage risk:** require token resolution from agent-assigned location only, never from input payload alone.
- **Concurrent human edits:** use read-before-write and conflict detection.
- **Bulk imports:** dry-run preview, threshold approval, rollback snapshots where possible.
- **Timezone mismatches:** normalize to UTC in storage, render in location timezone for reports.
- **Snapshot failures:** validate post-apply asset presence before marking onboarding complete.
- **Workflow ambiguity:** if no documented mutation endpoint exists, downgrade to inspection/reporting.
- **Marketplace scope changes:** create new app draft, diff scopes, test in staging install before production rollout.

## 19. Final Build Checklist

- [x] Latest official HighLevel docs inspected
- [x] API 2.0 chosen as source of truth
- [x] Deprecated or undocumented features blocked by policy
- [x] OAuth 2.0 strategy defined
- [x] PIT strategy defined
- [x] Token refresh and rotation strategy defined
- [x] Agency/location token separation defined
- [x] `ghl-agency-lead` defined
- [x] `ghl-sub-account-agent-{locationId}` defined
- [x] Specialist `ghl-*` agents defined
- [x] Capability registry design defined
- [x] Endpoint ownership matrix defined
- [x] Webhook verification and routing defined
- [x] Polling fallback defined
- [x] Rate-limit controls defined
- [x] Task packs modularized
- [x] Destructive approval policy defined
- [x] Logging and tamper-evident audit trail defined
- [x] Secure storage model defined
- [x] Unsupported features labeled instead of guessed
- [x] Maintenance and doc-drift plan defined
- [x] Plan is implementation-ready for OpenClaw
