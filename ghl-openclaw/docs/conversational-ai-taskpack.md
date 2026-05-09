# Conversational AI taskpack design

## Goal

Add a project-agnostic conversational AI layer to `ghl-openclaw` that can:

- handle inbound text conversations in plain English
- answer grounded project questions using business context, project context, and a knowledge base
- qualify, support, triage, route, schedule, or hand off depending on the project type
- reuse existing GHL action packs for sends, reads, contact updates, opportunity updates, and appointments
- become the shared decision engine for later voice AI work

## Core design shift

This should be built as a **project-profile-driven conversation engine**, not a single hardcoded lead-gen bot.

That means the conversational AI should be customizable by profile data for:

- project type
- audience types
- intent taxonomy
- slot schema
- allowed actions
- escalation policy
- tone and reply style
- channel behavior

Examples of supported project types:

- service business lead intake
- support or customer success
- appointment booking and rescheduling
- ecommerce pre-sales and order help
- recruiting and candidate screening
- real estate inquiry triage
- internal ops assistant
- education or community onboarding

The reusable unit is not just a business record. It is a **project profile** plus business context.

## Recommendation

Do **not** turn `conversation_management_pack` into the whole AI brain.

Instead:

- keep `conversation_management_pack` as the low-level conversation transport and read/write utility pack
- add a new orchestrator pack named `conversational_ai_pack`
- let voice AI later call the same conversational decision path instead of building a separate logic stack

This keeps transport separate from policy and reasoning.

## Why a separate pack

`conversation_management_pack` already does these jobs well:

- contact lookup by name
- conversation search and summary fetch
- reply-to-latest behavior
- direct message send with approval controls

But conversational AI needs a different responsibility set:

- turn classification
- grounded answer generation
- slot filling and follow-up questions
- project policy checks
- action planning across multiple domains
- escalation and handoff rules
- response strategy that works for both chat and voice

That is an orchestration layer, not just a conversations CRUD layer.

## Proposed pack

### Name

`conversational_ai_pack`

### Initial owner

Phase 1: `ghl-conversations-agent-{locationId}`

Rationale:

- fastest path with the current agent model
- inbound text conversations are the first target
- the pack can still call supporting adapters for contacts, opportunities, and calendars as needed

Phase 2 option:

- introduce a dedicated `ghl-conversation-ai-agent-{locationId}` if separation becomes important

## Trigger events

Initial:

- `InboundMessage`
- `ConversationUpdate`
- `ConversationUnreadWebhook`
- `ManualRun`

Later for shared voice logic:

- `VoiceAiCallStart`
- `VoiceAiTurn`
- `VoiceAiCallEnd`

## Phase 1 scope

The first live slice should still be narrow, but the architecture should already be **project-agnostic**.

Phase 1 target:

- inbound text conversations
- one configurable project profile loaded per run
- one default shipped archetype: service-business lead handling

Supported behaviors in the generalized design:

1. answer grounded project questions using the active profile and knowledge base
2. detect project-specific intent
   - service or offer questions
   - support issues
   - scheduling
   - qualification or intake
   - objection or hesitation
   - human handoff
3. extract configurable slots defined by the profile
   - examples: need, urgency, budget, product, issue type, timeframe, property type, role, order status
4. suggest or trigger next actions based on the profile
   - reply with answer only
   - ask one clarifying question
   - create or update contact fields or notes
   - create or update opportunity or case state
   - offer booking or next-step routing
   - hand off to human

Out of scope for phase 1:

- full multi-turn autonomous appointment booking
- outbound nurture sequencing
- voice runtime execution
- payment collection
- complex workflow authoring

## Architecture

### Layer 1: transport and records

Reuse existing pieces:

- `conversation_management_pack`
- `ContactsAdapter`
- `ConversationsAdapter`
- `OpportunitiesAdapter`
- `CalendarsAdapter`

### Layer 2: project grounding

Reuse existing business context patterns from `registry.js`:

- `ACTIVE_BUSINESS.json`
- active business defaults helpers
- `knowledgeBaseRef`
- location resolution from the active business record

Add a project-grounding helper that returns:

- business identity
- project identity
- project type
- audience schema
- intent schema
- slot schema
- allowed actions
- CTA or routing policy
- knowledge base ref or source
- tone or brand voice
- operating constraints

### Layer 2.5: project profile

The conversational system should load a normalized project profile, for example from:

- active business defaults plus AI-specific extensions
- a dedicated profile record such as `docs/conversational-ai-project-profile.template.json`
- future per-project saved profiles in workspace or credential-backed storage

The project profile should define:

- project type and summary
- channel policy
- domain entities and terminology
- custom intents
- custom slots and follow-up questions
- playbooks
- action permissions
- guardrails and escalation rules
- evaluation examples

### Layer 3: conversation intelligence

New orchestration logic should produce a structured decision object:

```json
{
  "projectType": "service_business",
  "intent": "ask_service_question",
  "intentId": "ask_service_question",
  "confidence": 0.92,
  "needsHuman": false,
  "needsReply": true,
  "replyMode": "answer_then_qualify",
  "slots": {
    "service": "done-for-you automation",
    "urgency": null,
    "budget": null
  },
  "proposedActions": [
    { "type": "send_reply" },
    { "type": "append_contact_note" }
  ],
  "citations": [
    "knowledgeBaseRef:..."
  ]
}
```

### Layer 4: action execution

The AI pack should not directly duplicate every project mutation.

Instead it should either:

- call the underlying adapters directly for small safe updates, or
- invoke the same request-building helpers already used by existing packs

Preferred rule:

- **read and reasoning stay in `conversational_ai_pack`**
- **domain writes reuse existing domain logic whenever possible**

Examples:

- send a reply -> reuse conversation send behavior
- update lead note or tag -> reuse lead-management primitives
- create appointment -> reuse appointment pack request shape
- route support case -> reuse future support or workflow primitives

## Proposed execution plan

### For inbound text events

1. `fetch_conversation_context`
   - get conversation
   - get latest messages
   - get contact

2. `load_project_context`
   - resolve active business defaults
   - resolve project profile
   - require knowledge base for grounded answer mode

3. `evaluate_conversation_turn`
   - classify intent
   - map the turn into the active project taxonomy
   - detect whether a reply is needed
   - detect whether human escalation is needed
   - extract slots

4. `plan_conversation_response`
   - draft reply
   - decide whether to answer, qualify, support, route, book, or hand off
   - produce proposed domain actions

5. `approve_high_risk_actions`
   - only when a write or risky automation is proposed

6. `execute_domain_actions`
   - optional contact, opportunity, appointment, or workflow-adjacent updates

7. `send_conversation_reply`
   - if policy says reply now

8. `record_turn_summary`
   - store structured trace in run history for debugging and later voice reuse

## Executor changes recommended

The current taskpack executor supports:

- `adapter_call`
- `intent`

That is enough for plumbing, but not enough for a real conversational AI pack.

Recommended addition:

### New step kind

`agent_turn`

Purpose:

- run a model-backed reasoning step with structured JSON output
- keep the step transcript and parsed result in run history
- allow dry-run behavior when no OpenAI credential is configured

Suggested shape:

```js
{
  name: 'evaluate_conversation_turn',
  kind: 'agent_turn',
  credentialRef: activeBusiness.openaiCredentialRef,
  responseSchema: 'conversation_turn_evaluation',
  prompt: (context) => ...,
  resumeData: (result) => ...
}
```

Fallback if executor changes are delayed:

- phase 1 can stub these steps as `intent` plus generated debug artifacts
- then wire true model calls once `agent_turn` exists

## Input contract

`conversational_ai_pack` should accept a normalized mutation request like:

```json
{
  "action": "run_conversational_ai",
  "locationId": "...",
  "conversationId": "...",
  "contactId": "...",
  "requestText": "Customer asked whether you handle HVAC follow-up and what pricing looks like",
  "mode": "reply",
  "project": {
    "type": "service_business",
    "profileRef": "optional override",
    "profile": null
  },
  "business": {
    "name": "optional override",
    "knowledgeBaseRef": "optional override"
  },
  "policy": {
    "allowAutoReply": true,
    "allowAutoQualify": true,
    "allowAutoBook": false,
    "allowAutoUpdateCRM": true,
    "allowAutoSupportActions": false,
    "allowAutoRouting": true
  }
}
```

The pack should also allow explicit project-profile overrides for custom deployments, for example:

- custom intents
- custom slots
- custom playbooks
- action allowlists
- escalation keywords
- channel-specific reply rules

## Output contract

Return a structured result that is useful in both chat and voice flows:

```json
{
  "decision": {
    "intent": "pricing_question",
    "projectType": "service_business",
    "needsReply": true,
    "needsHuman": false,
    "replyMode": "answer_then_question"
  },
  "reply": {
    "message": "Yes, we handle HVAC follow-up automation. Pricing depends on lead volume and the handoff setup. Roughly, are you looking for missed-call text back only, or full pipeline follow-up?"
  },
  "actions": [
    {
      "type": "contact_note",
      "status": "planned"
    }
  ],
  "grounding": {
    "businessName": "...",
    "projectProfileRef": "...",
    "knowledgeBaseRef": "..."
  }
}
```

## Project-agnostic guardrails

The AI layer should always:

- prefer the active project profile and business record, not hardcoded industry assumptions
- require a knowledge base for authoritative claims
- avoid invented pricing, guarantees, or policies
- ask a clarifying question when information is missing
- escalate to human when confidence is low or the request is high risk

Hard blocks for auto-send:

- legal or compliance claims
- refunds or billing disputes
- custom quotes beyond documented ranges
- contract negotiation
- crisis or complaint handling with strong negative sentiment

## Approval model

Recommended behavior:

- safe read-only reasoning: no approval
- auto-reply from grounded approved policy: no approval in low-risk mode
- CRM writes: approval optional by policy
- appointment booking, pipeline movement, or outbound mutations: approval required unless explicitly allowed later

This matches the current approval-first architecture better than a fully autonomous first pass.

## Relationship to voice AI

Voice AI should use the same reasoning core.

Later design:

- `voice_ai_runtime_pack` handles call transport, turns, and voice-agent resources
- it passes normalized turn state into the same evaluation and response planner used by `conversational_ai_pack`
- it uses the same project profile, intent taxonomy, slot schema, guardrails, and action policy
- it should emit the same structured decision and reply contract so voice and chat can reuse downstream actions
- text and voice then share:
  - intent detection
  - slot filling
  - project grounding
  - escalation policy
  - action planning

That avoids building one text brain and one voice brain.

## Suggested implementation order

### Step 1

Add the new pack definition, registry stub, and profile loading support:

- `src/ghl/taskpacks/definitions.js`
- `src/ghl/taskpacks/registry.js`
- `src/ghl/webhooks/processor.js`
- `docs/conversational-ai-project-profile.template.json`

### Step 2

Implement read-only context assembly:

- fetch conversation
- fetch messages
- fetch contact
- resolve active business and project-profile metadata

### Step 3

Add structured AI evaluation step support in the executor.

### Step 4

Ship the first narrow use case and keep it profile-driven:

- inbound lead question -> grounded reply + optional qualification question

### Step 5

Add controlled project actions:

- contact note or tag updates
- opportunity creation or update
- booking handoff suggestions
- support-routing or workflow suggestions

## Best first live scenario

The first scenario to validate live should be:

**An inbound message hits a configured project profile, the AI picks the right intent from that profile, answers with grounded context, and asks one useful next-step question.**

Success looks like:

- grounded answer
- no hallucinated project claims
- one useful follow-up question
- optional CRM note
- no approval unless a real write is attempted

## Files likely involved when implementation starts

- `src/ghl/taskpacks/definitions.js`
- `src/ghl/taskpacks/registry.js`
- `src/ghl/taskpacks/executor.js`
- `src/ghl/webhooks/processor.js`
- `src/ghl/api/adapters.js` (only if small helper reads are missing)
- `src/ghl/onboarding/start-service.js` or related active-business helpers if OpenAI credential lookup needs to be shared cleanly
- `docs/conversational-ai-project-profile.template.json`

## Recommendation for the next step

After this design, the best next coding step is:

**Implement the read-only scaffold for `conversational_ai_pack`, add project-profile loading, and add executor support for a structured `agent_turn` step.**

That gives us a real customizable conversational brain entry point without prematurely wiring risky autonomous writes.
