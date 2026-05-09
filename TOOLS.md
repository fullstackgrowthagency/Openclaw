---
summary: "Workspace template for TOOLS.md"
read_when:
  - Bootstrapping a workspace manually
---

# TOOLS.md - Local Notes

Skills define _how_ tools work. This file is for _your_ specifics — the stuff that's unique to your setup.

## What Goes Here

Things like:

- Camera names and locations
- SSH hosts and aliases
- Preferred voices for TTS
- Speaker/room names
- Device nicknames
- Anything environment-specific

## Examples

```markdown
### Cameras

- living-room → Main area, 180° wide angle
- front-door → Entrance, motion-triggered

### SSH

- home-server → 192.168.1.100, user: admin

### TTS

- Preferred voice: "Nova" (warm, slightly British)
- Default speaker: Kitchen HomePod
```

## Why Separate?

Skills are shared. Your setup is yours. Keeping them apart means you can update skills without losing your notes, and share skills without leaking your infrastructure.

---

## GHL Voice AI notes

- `welcomeMessage` is shared across inbound and outbound, so do not treat it as an outbound-only greeting field.
- Outbound HighLevel trial calls can prepend a platform-level AI/opt-out disclosure before the saved agent prompt takes over.
- When checking bad outbound behavior, inspect call logs first and note whether `trialCall` is `true` before assuming the saved greeting is wrong.
- For outbound agents, explicitly instruct the prompt that after any forced system disclosure it must go straight into the sales reason for the call and never use inbound phrases like "How can I help you today?"
- Standard for future outbound Voice AI builds: do not rely on the agent test-call path for final QA. Use a real HighLevel outbound workflow with an internal QA contact/tag so resulting calls are non-trial when compliance rules allow it.

Add whatever helps you do your job. This is your cheat sheet.