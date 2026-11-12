# Changelog

## 0.3.5 (12 November 2026)

The fixes from the cycle-3 review: a possible hang, two ways a choice or a routine could slip through, and a much
shorter README and plugin description.

- Fixed a possible hang: the status read and a Hermes to Grok change could each wait for the lock the other held.
  Hermes to Grok now reads the Grok side from the last status, and the one lock order is checked in code.
- Reading the status no longer writes any file. The background round saves what the read learnt, and a status
  never shows "up to date" while a choice is still being worked out.
- An instructions or Hermes to Grok conflict is now the one "changed in both places" box on the Twins page, with
  buttons that settle it. Before, the row could show an empty box with nothing to press.
- A computer stays linked, with a plain sentence, while a routine it runs cannot be paused, so a routine never
  runs on two computers.
- One status sentence for every twin, worked out in one place: the Twins page, the terminal and `/twin` say the
  same words, and a twin's state follows its counts of changes and choices.
- The sample tour no longer sends you to the Grok app: after `demo secret` it names `demo grok-edit`, it ends with
  `send-to-grok`, and `finish --all` with only the sample says so instead of "No twins yet".
- The README says at the top that the Plugins button and the catalog name give the older 0.2.1, gives the one
  command for this version, and states the minimum Hermes (0.22.7) before any install line.
- The README is one screen (133 lines, was 1,289); the rest moved, once, to docs/GUIDE.md, docs/SHARING.md and
  docs/TERMINAL.md. The plugin description that `hermes plugins list` prints is 58 words (was 2,464) and points to
  docs/DISCLOSURE.md, now the one disclosure.

## 0.3.4 (12 November 2026)

Smaller and safer: the fixes from the cycle-2 review, mostly by removing code (about 1,300 fewer product lines).

- Removed "Let Hermes do the Grok part" (Hermes clicking in the Grok app). Its guard stopped protecting a chat once
  its 30 minutes ran out, and it was never run against the real Grok app. Bringing instructions over stays as it
  was: `hermes twin finish --all` prints the request you paste into each bot, and `import-exports` reads the answers.
- Removed the model tool `twin_ask_machine`: a chat can no longer start a request on another computer. You still can,
  with `/twin ask`, `hermes twin ask` or the Twins page. `hermes twin ask` refuses a command that a chat started.
- Removed the approval mode "Let my own linked computers approve". A stored choice now reads as "Ask on this
  computer". `/twin` and the request checks refuse a chat whose platform is missing or unknown.
- One password-and-key check for everything (memory, setup exports, instructions, Hermes to Grok). Short
  `NAME=value` passwords such as `DB_PASSWORD=hunter2` are now caught on the way to Grok. Ordinary instruction text
  ("Passwords must have at least 8 characters") is not held.
- A twin with an instructions or Hermes to Grok conflict no longer says "Up to date": it says it needs you and
  which item to choose, in the terminal, in `/twin` and on the Twins page.
- Stop twinning now also withdraws the twin's Hermes to Grok permission (the Mirror's allow list no longer names the
  bot) and switches off its memory layers.
- "Move routines to Hermes" refuses a routine that runs in Grok (or is being moved), so a routine never runs in
  both places.
- Fixed a rare hang when the Mirror's answer and a What syncs change arrived together. Moving a routine between
  Hermes and Grok no longer holds the plugin's lock while `hermes cron` runs.
- No background thread before you have a twin (or share or link a computer); the threads end when nothing is left.
  A round checks and syncs by default; the optional parts run only while they are on, and one failing part no
  longer holds up or slows the others.
- One place talks to Hermes' own code (`twin/config.py`). The checks "is this chat local?" and "is this skill safe?"
  refuse when Hermes cannot answer. A Hermes routines file (`cron/jobs.json`) in a shape the plugin does not know is
  left alone with a clear message, instead of being read as empty and written over.

## 0.3.3 (12 November 2026)

- Installs from GitHub again without `--force`: Hermes' install scan stopped the install because three source
  lines held invisible characters typed in raw form. Those characters are now written as escapes (`\u200b`), in
  the tests too, and a new test fails if any file of the plugin holds a raw invisible or text-direction character.
- README: names the minimum Hermes version (0.22.7) and the install line for the `next` branch, and no longer
  points to a catalog file that is not in this repository.

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
