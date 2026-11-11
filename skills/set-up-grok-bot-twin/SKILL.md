---
name: set-up-grok-bot-twin
description: "Set up Grok Bot Twin and link your computers."
version: 1.0.0
author: Josh Hudson
license: MIT
metadata:
  hermes:
    tags: [grok, twin, setup, onboarding]
    category: productivity
---

# Set Up Grok Bot Twin Skill

Walks the owner through setting up Grok Bot Twin in this chat. One command, `hermes twin setup`, does every
step a program may do and says who does the rest. This skill never touches the Grok app, never asks for a
password, key or pass, and never lists the owner's bots by name.

## When to Use

- The owner asks to set up Grok Bot Twin, to connect or twin their Grok bots, to link another computer, or
  whether setup is finished.
- The owner pointed Hermes at the Grok Bot Twin repository and said "set this up".
- Not for everyday questions about a twin (use the `twin_status` tool), and not for moving a bot to Hermes
  once and for all (that is `hermes import-agent grok-bot`, from the agent-importers plugin).

## Prerequisites

- Grok Bot Twin is switched on (this skill is only listed while it is).
- The `terminal` tool, to run `hermes twin setup`.
- On the computer with the Grok desktop app the owner needs botport, a small free app that reads the bot
  list from Grok. The owner downloads it with their browser; nobody else does. No account and no key.

## How to Run

Run every command below with `terminal`. Start with `hermes twin setup --check`. It changes nothing.

## Quick Reference

| Command | What it does |
|---|---|
| `hermes twin setup --check` | Where this computer stands and who does what is left. Changes nothing. Exit status 0: nothing is left. |
| `hermes twin setup --plan [options]` | Exactly what a run with the same options would do. Changes nothing. |
| `hermes twin setup --yes [options]` | Does it. Only after the owner said yes to the plan. |
| `--bot "NAME"` (repeatable), `--all` | Also give these Grok bots, or every Grok bot, a twin. |
| `--share` | Also share the twins with the owner's other computers; prints a link code. |
| `--link CODE` | On a computer without the Grok app: link it with that code. |

In the answer: `✓` done, `!` the owner's to do, `·` waits for an earlier step, `–` optional or not needed.
Exit status 1 from `--check` is not a failure: it means something is left, and the lines say what.
Where an answer says "run hermes twin setup again", you run it with `--plan` first, and with `--yes` only
after the owner's yes. A plain `hermes twin setup` asks its question in a terminal, which you do not have.

## Procedure

1. **Look.** Run `hermes twin setup --check`. Tell the owner in two or three short sentences where things
   stand. Its first line says which kind of computer this is: "it has the Grok app" or "it gets its twins over
   a link". You may add that the Twins page in Hermes Desktop does the same with buttons.
2. **botport is missing** (only on the computer with the Grok app): give the owner the file name and the page
   exactly as printed, and ask them to download it with their browser. Do not fetch it with any tool. When
   they say it is there, go on: setup finds the file in Downloads and verifies it before it is ever run. No
   second check is needed: the plan in step 5 then has a "Will do" line for the download. If the plan still
   marks botport with `!`, the file is not in Downloads under that exact name.
3. **Ask which bots get a twin** (this and the next question may go in one message). The owner types the
   names, or says "all". Do not look the names up yourself: `hermes twin setup` counts twins and prints no
   name you did not type, and that is on purpose. Run `hermes twin list-grok` or plain `hermes twin` only if
   the owner asks to see names in this chat.
4. **Ask about other computers.** "Do you want these twins on your other computers too?" If yes, add
   `--share`.
5. **Show the plan.** Run `hermes twin setup --plan` with those options, for example `hermes twin setup --plan
   --bot "Acme Support" --share`. Show the owner every line marked `→ Will do:` with the indented lines under
   it, word for word, and any line marked `!`. The other lines can stay out. Ask: "Shall I go ahead?"
6. **Only after a clear yes**, run the same line with `--yes` in place of `--plan`. A yes covers the plan the
   owner saw: if anything changed since (botport was downloaded, another bot was named), show the new plan
   first.
7. **Say what is left.** The run ends with "Left for you:" or "Setup is complete on this computer." Pass the
   lines under "Left for you:" on word for word; they say whether a twin waits for its instructions or for a
   model. Steps that stay the owner's: bringing each twin's instructions over, pasted on the Twins page in
   Hermes Desktop or imported from the setup export the bot writes about itself
   (the Grok app does not keep them on the computer, so setup cannot copy them), and choosing a model for a
   twin that has none (a new twin takes the model of the owner's other chats; the Twins page asks only when
   there is none).
8. **Link the other computers.** The run with `--share` prints a link code such as `7KQ4-M2XD-P9RA`: one
   computer, 10 minutes. Give the owner the code at once, before anything else, and say when it was made. The
   other computer needs Hermes with Grok Bot Twin switched on, and the same private network (the same home
   network, or Tailscale on both); no Grok app and no botport. There the owner opens Twins, presses **Link
   this computer** and types the code, or types `hermes twin setup --link CODE` in a terminal (they are asked
   once; an agent adds `--yes`). Hermes on that computer can do it with this same skill. Tell the owner that
   Hermes has to stay open on this computer, or the other one cannot reach it. When the code ran out, or for
   one more computer, `hermes twin share code` makes the next code.
   If this computer is the one without the Grok app, ask the owner for the code from the other computer, then
   `hermes twin setup --plan --link CODE`, then the same with `--yes`.
9. **Offer skill sync, once** (not at all if the owner already said no). "Skills can also travel between Grok
   and Hermes. That needs a second small bot in your own Grok account, and every run of that bot spends Grok
   credit. Want to set it up?" If yes: on the Twins page, the twin's **…** menu, **Skill sync…**. The sheet
   there guides every step and makes the files to attach. Do not set it up from this chat, and never ask for
   the pass (a small key file): the owner attaches it in Grok by hand.
10. **Check.** Run `hermes twin setup --check` once more and tell the owner the result. If a line there looks
    wrong, `hermes twin doctor` says more.

## Pitfalls

- `'twin' is not a hermes command`: the plugin is installed but off. `hermes plugins enable grok-bot-twin`.
- "Not a terminal: pass --yes to go ahead.": nothing was done. Add `--yes` only after the owner agreed.
- A step fails: tell the owner its exact words and stop. Do not retry with other options. Never add
  `--allow-unattested`; it accepts a botport that was not verified and is only for a botport the owner
  built themselves and asks for by name.
- Never ask for, read out or paste a password, key, token or pass. A link code is the one code that may
  appear here: it is short-lived, works once, and only from the owner's own private network.
- Do nothing in the Grok app: no clicks, no computer use, no reading of its folder. Hermes doing the Grok
  clicks is a design that is not built (docs/SETUP-ORIGINS.md in the plugin's repository).
- Do not edit `config.yaml`, a bot's `SOUL.md` or the plugin's data folder by hand. Every change has a
  command.
- Do not switch on requests between computers, the background helper, the activity digest, skills, routines
  or a twin's memory between computers, the model setting, or approval from linked computers unless the
  owner asks for that by name.
- Undo is the owner's decision: `hermes twin stop "NAME" --dry-run`, `hermes twin unlink --dry-run` and
  `hermes twin uninstall --dry-run` show what would go. Show that first.

## Verification

- `hermes twin setup --check` exits with status 0 and says "Setup is complete on this computer.", or every
  line marked `!` names a step that is the owner's.
- If you ran it: `hermes twin doctor` shows `✓` lines, or `–` lines with a reason.
- On a linked computer, `hermes twin setup --check` says "Linked to" and the name of the other computer.
