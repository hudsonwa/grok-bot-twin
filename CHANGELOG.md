# Changelog

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

### Moves to 0.3.2

- Hermes to Grok out of beta.

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
