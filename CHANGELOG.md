# Changelog

## 0.3.2 (12 November 2026)

- Hermes to Grok out of beta: there is no switch for the whole plugin any more. It is off for every twin until you
  allow that twin, and nothing is sent before you saw the preview and applied exactly that preview.
- The preview shows exactly what is sent: whole texts, the whole instructions diff, routine prompts in full, and
  the schedule in words when it is simple. The terminal shows a routine's schedule and prompt changes.
- A signed list of the bots you allow now (`to-grok/allowed.json`) sits next to the requests. The Mirror's box
  makes a change only for a bot in that list, never from an older list, and never twice.
- Stopping a twin takes its waiting requests back and names the exact Auto-Review rule to delete in Grok; allowing
  records that rule.
- Bot content cannot steer the Mirror: a bot name with quotes, commas or linking words is left out (the id is
  used), routine names sit between BEGIN and END lines like other texts, a schedule must be plain cron, and a text
  that holds the change's own id is not sent. The Mirror's read-back gives only a name and description.
- Undo and moving where a routine runs check again that the twin is allowed, the item's direction and the Mirror's
  permission. A routine set to run in Grok while its Hermes copy is on is held, so it never runs twice.

## 0.3.1 (12 November 2026)

- Full transcripts (off for every twin until you switch them on): the archive
  of whole conversations, from Hermes chats and from a Grok export you hand
  over, kept on this computer. The text is encrypted at rest with a key kept
  apart from the archive. The twin's own tool searches only that twin's
  conversations, and never inside a request from another computer. Sending
  to Grok is a second switch, off by default, needs the Mirror, offers only
  chats held on this computer, and masks passwords and keys first. Stop
  twinning or `hermes twin transcripts forget --yes` deletes what was kept.
  See docs/DISCLOSURE.md for exactly what is kept and what it does not
  protect against.

### Moved to a later release

- Hermes to Grok out of beta: in 0.3.2.

## 0.3.0 (12 November 2026)

More than skills now syncs between a twin and its Grok bot.

- Memory facts (off for every twin until you switch it on): the facts a Grok bot remembers and the notes in the twin's
  memory files are kept the same, both ways. Anything that looks like a
  secret or an instruction stays where it was written. This needs the
  Mirror; until it is set up, no fact moves.
- Session briefs: a finished conversation becomes a short brief (who took
  part, what was decided, open threads, next step) for the other side. Off
  for every twin until you switch it on; it needs the Mirror to reach Grok.
- Instructions: with skill sync set up, a twin's instructions follow its Grok
  bot's every 15 minutes, unless you switch that off. When it is unsure which
  to keep, you are asked.
- What syncs panel: one place per twin to choose what copies and in which
  direction.
- Skills from Grok to Hermes: an edit made in Grok to a skill Hermes sent now
  comes back (it was dropped silently before), and skills made in Grok come
  back when you choose them (`hermes twin skill-sync TWIN --take NAME`).
- Hermes to Grok (beta, off by default): name, description, instructions and
  routines go from Hermes to the Grok bot through the Mirror, after you look
  at a preview and apply it.

### Moved to a later release

- Full transcripts (the archive of whole conversations): in 0.3.1.
- Hermes to Grok out of beta: moves to 0.3.2.
