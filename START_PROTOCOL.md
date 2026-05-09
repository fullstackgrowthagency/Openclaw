# START_PROTOCOL.md

Use this when the user sends exactly `/start` in a direct chat.

## Goal

Set up one active business at a time.

Do not start building campaigns, calendars, creatives, ads, automations, or reports until the active business has the required `/start` setup complete.

## Core rule

**Knowledge base required.**

If the business knowledge base is missing, onboarding is incomplete.
Do not proceed past setup planning.

## What `/start` should do

Reply with a short onboarding message that:

1. says you will set up one business at a time
2. says the knowledge base is required
3. asks for the exact required setup items below
4. asks the user to send attachments directly when possible
5. says keys/access will be validated and stored securely, not written into plain business files
6. keeps the request concise and checklist-style

## Required setup items

Ask for these on `/start`:

1. **OpenAI API key**
2. **Business name**
3. **Business knowledge base**
   - file, docs link, exported notes, pasted text, or other source of truth
   - this is required before production work
4. **Business logo**
   - image attachment or hosted URL
5. **Website / domain**
6. **GHL access**
   - location/subaccount access details
   - credential reference, OAuth/PIT, or other usable connection details

## Optional follow-up items

Collect these right after `/start` if needed, but do not block initial setup on them:

- KPIs
- payment/product info

Other useful items can be collected later when relevant.

## Response template for `/start`

Use this structure, adapted naturally:

- short one-line summary
- say: one business at a time
- say: knowledge base required
- ask for:
  - OpenAI API key
  - business name
  - knowledge base
  - logo
  - website
  - GHL access

Example shape:

"Summary: I’m setting up your active business workspace now. We’ll do one business at a time, and I need the business knowledge base before I build anything.

Please send:
- OpenAI API key
- Business name
- Knowledge base or source-of-truth docs
- Logo
- Website/domain
- GHL access details"

## After the user replies

1. identify which required items are present
2. list only the missing required items
3. if enough info is present, validate and securely store the OpenAI key and GHL access
4. store/update `business/ACTIVE_BUSINESS.json` with refs and statuses only, never raw secrets
5. if the knowledge base is still missing, stop and ask for it plainly
6. do not pretend onboarding is complete until all required items are present
7. then collect optional follow-up items like KPIs and payment/product info

## Storage

Use `business/ACTIVE_BUSINESS.template.json` as the shape.
Persist the real active business as `business/ACTIVE_BUSINESS.json` once actual values are known.

## Switching businesses

If a different business is introduced later:

- confirm that the user wants to replace the current active business
- then update the active business record
- keep the one-business-at-a-time rule explicit
