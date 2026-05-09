# START_PROTOCOL.md

Use this when the user sends exactly `/start` in a direct chat.

## Goal

Set up one active business at a time.

Do not start building campaigns, calendars, creatives, ads, automations, or reports until the active business has a knowledge base reference or knowledge base content attached.

## Core rule

**Knowledge base first.**

If the business knowledge base is missing, onboarding is incomplete.
Do not proceed past setup planning.

## What `/start` should do

Reply with a short onboarding message that:

1. says you will set up one business at a time
2. says the knowledge base is required
3. asks for the required setup items below
4. asks the user to send attachments directly when possible
5. keeps the request concise and checklist-style

## Required setup items

Ask for these on `/start`:

1. **Business name**
2. **Business knowledge base**
   - file, docs link, exported notes, pasted text, or other source of truth
   - this is required before production work
3. **Business logo**
   - image attachment or hosted URL
4. **Website / domain**
5. **Primary offer(s)**
6. **Target audience**
7. **Brand voice / positioning**
8. **Timezone**
9. **Required API keys / platform access**
   - GHL access details
   - social platform access if needed
   - ad platform access if needed
   - any other external tool credentials relevant to the requested work
10. **Approval/contact preference**
   - what can be drafted automatically
   - what must wait for approval

## Helpful optional items

If missing, ask later only when useful:

- brand colors
- extra brand assets
- preferred posting cadence
- compliance constraints
- restricted claims/topics
- CTA preferences
- service areas / geo targets
- competitors / market context

## Response template for `/start`

Use this structure, adapted naturally:

- short one-line summary
- say: one business at a time
- say: knowledge base required
- ask for:
  - business name
  - knowledge base
  - logo
  - website
  - offers
  - audience
  - brand voice
  - timezone
  - API keys / access
  - approval preference

Example shape:

"Summary: I’m setting up your active business workspace now. We’ll do one business at a time, and I need the business knowledge base before I build anything.

Please send:
- Business name
- Knowledge base or source-of-truth docs
- Logo
- Website/domain
- Main offers/services
- Target audience
- Brand voice/positioning
- Timezone
- Relevant API keys/access
- What I can draft automatically vs what should wait for approval"

## After the user replies

1. identify which required items are present
2. list only the missing required items
3. if enough info is present, store/update `business/ACTIVE_BUSINESS.json`
4. if the knowledge base is still missing, stop and ask for it plainly
5. do not pretend onboarding is complete until all required items are present

## Storage

Use `business/ACTIVE_BUSINESS.template.json` as the shape.
Persist the real active business as `business/ACTIVE_BUSINESS.json` once actual values are known.

## Switching businesses

If a different business is introduced later:

- confirm that the user wants to replace the current active business
- then update the active business record
- keep the one-business-at-a-time rule explicit
