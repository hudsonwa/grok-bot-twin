# AGENTS.md: for coding agents pointed at this repository

You are a coding agent (Claude Code, Cursor, Codex or any other) and your owner pointed you at Grok Bot Twin.
This file tells you exactly what to run, what to leave to your owner, and what never to do. People: the
[README](README.md) is written for you.

Grok Bot Twin is a plugin for Hermes Agent. It gives a bot from the Grok desktop app a twin: a Hermes bot that
follows it. Nothing ever changes in Grok.

## If your owner asked you to set it up

One command does the setup, on every kind of computer: `hermes twin setup`. It can be run again at any time: a
step that is done is skipped. You never need another way in.

| Command | What it does |
|---|---|
| `hermes twin setup --check` | Says where this computer stands and who does each step that is left. Changes nothing. Exit status 0 means nothing is left. |
| `hermes twin setup --plan [options]` | Says exactly what a run with the same options would do. Changes nothing. |
| `hermes twin setup --yes [options]` | Does it, without asking. Run it only after your owner said yes to the plan. |

Add `--json` to `--check` or `--plan` for a machine-readable answer.

### The steps

1. **Is Hermes there?** Run `hermes --version`. It must be 0.22.7 or newer. If the command is missing, stop
   and tell your owner to install Hermes Agent first. Do not install or update Hermes yourself; if it offers
   an update, mention that to your owner.
2. **Is the plugin there?** Run `hermes plugins list --plain --no-bundled` (the short list). If
   `grok-bot-twin` is not listed, run `hermes plugins install grok-bot-twin --enable`. If it is listed but
   off, run `hermes plugins enable grok-bot-twin`. (Until the catalog lists it, the README section "Prefer
   the terminal?" has the install line with a pinned commit.)
   If your owner wants the code of this very checkout instead, or the computer is offline, install from the
   checkout: `hermes plugins install file://ABSOLUTE-PATH --ref COMMIT --enable`. ABSOLUTE-PATH is where this
   checkout is, so the address has three slashes in a row, like `file:///home/you/twin`. COMMIT is
   the full commit id that `git rev-parse HEAD` prints here; only committed code is installed. Hermes then
   prints two warnings (the source is not from its catalog; the address is a local one). Pass both on to
   your owner.
3. **Look.** Run `hermes twin setup --check` and read every line. `✓` is done, `!` is your owner's to do, `·`
   waits for an earlier step, `–` is optional or not needed here. Its first two lines also tell you that the
   plugin is on and which kind of computer this is.
4. **If botport is missing, ask for it now.** botport is a small free app that reads the bot list from
   Grok; only the computer with the Grok desktop app needs it. When `--check` marks it with `!`, give your
   owner the file name and the page from that line and ask them to download it with their browser. This
   plugin never downloads botport, and neither do you. Go on when they say it is in their Downloads folder:
   setup finds it there by its exact name, and uses it only after a yes.
5. **Ask your owner what they want**, in the same message if you like:
   - On the computer with the Grok desktop app: which Grok bots should get a twin? They type the names, or
     say "all". Do not list their bots yourself (see "Never").
   - Should the twins also appear on their other computers?
6. **Show the plan.** Run the same options with `--plan` and paste its output for your owner, unchanged:
   - named bots: `hermes twin setup --plan --bot "Acme Support" --bot "Recipe Planner"`
   - every bot: `hermes twin setup --plan --all`
   - other computers too: add `--share`
   - on a computer without the Grok app: `hermes twin setup --plan --link CODE`
7. **Wait for a clear yes.** Then run the same line with `--yes` in place of `--plan`. A yes covers the plan
   your owner saw. If anything changed since (botport arrived, they named another bot), show the new plan
   first. You need `--yes` because you have no terminal to answer the question in; a person who types the
   command is asked once. So where an answer says "run hermes twin setup again", you run it with `--plan`
   first, and with `--yes` after the yes.
8. **Say what is left.** The run ends with "Left for you:" or "Setup is complete on this computer." Pass
   that on, unchanged. Three things stay your owner's:
   - Bringing each twin's instructions over: pasted on the Twins page in Hermes Desktop, or imported from
     the setup export the bot writes about itself (Import setup export…). The Grok app does not keep them
     on the computer, so setup cannot copy them.
   - Choosing a model for a twin that has none. A new twin takes the model of your owner's other Hermes
     chats; only when there is none does the Twins page ask them to choose one.
   - Signing in to anything.

### A second computer

On the computer with the Grok app, `hermes twin setup --share --yes` prints a link code like
`7KQ4-M2XD-P9RA`. It links one computer and lasts 10 minutes. On the other computer (Mac, Windows or Linux,
with Hermes and this plugin; no Grok app and no botport needed there), run
`hermes twin setup --link 7KQ4-M2XD-P9RA --yes`.

- Hermes has to be open on the computer that shares (Hermes Desktop, or any Hermes chat), or the other
  computer cannot reach it. Both computers have to be on the same private network (the same home network,
  or Tailscale on both). Say so when you hand over the code, hand it over at once, and say when it was made.
- The code ran out, or one more computer is to be linked: `hermes twin share code` makes a new one. You may
  run it when your owner asks for a code.
- The code may pass through your chat: it is short-lived, works once, and only from your owner's own
  private network.

## Never

- **No secrets in chat.** Never ask for, read, print or paste a password, a key, a token or a twin's pass
  (a small key file the plugin keeps for skill sync). If your owner pastes one, tell them to change it. The
  pass travels as a file that your owner attaches by hand, never as text.
- **No changes to Grok outside the guided steps.** Do not open, click, script or automate the Grok app. Do
  not read, copy or edit the Grok app's folder or its files. Do not edit, create or delete a Grok bot, its
  instructions or its routines. The only guided steps in Grok are the ones the Twins page shows your owner
  (pasting instructions, the Skill sync… sheet), and your owner does them.
- **Do not list your owner's bots.** `hermes twin setup` counts twins and never prints a name you did not
  type, so nothing private reaches your chat. `hermes twin`, `hermes twin list-grok` and `hermes twin fleet`
  do print names: run them only when your owner asks you to.
- **Do not edit Hermes' files by hand.** Not `config.yaml`, not a bot's `SOUL.md`, nothing under
  `plugin-data/grok-bot-twin/`. Every change has a command.
- **Do not go around a refusal.** If a step fails, report its exact words and stop. Do not retry with other
  options. In particular never add `--allow-unattested` (it accepts a botport that was NOT verified; it is
  only for a botport your owner built themselves and asks for by name), and never download or build botport
  to get past the check.
- **Do not turn on what was not asked for**: requests between computers (`hermes twin requests on`), the
  background helper (`hermes twin background on`), skill sync, the activity digest, skills between computers
  (`hermes twin fleet skills on`), routines (`hermes twin routines on`, `run-here on`), a twin's memory
  (`hermes twin fleet memory TWIN on`), the model setting (`hermes twin fleet settings model on`), and approval
  from linked computers (`hermes twin requests mode linked`). Each is your owner's
  decision. Skill sync also spends Grok credit on every run.
- **Do not remove anything without showing it first.** `hermes twin stop`, `hermes twin unlink` and
  `hermes twin uninstall` each take `--dry-run`; show that output and wait for a yes.
- **Do not push, publish or post** anything from this repository on your owner's behalf.

## Check your work

Run these three and report their output:

```sh
hermes twin setup --check     # exit status 0 and "Setup is complete on this computer."
hermes twin doctor            # every line starts with ✓, or with – and a reason
hermes plugins list --plain --no-bundled    # grok-bot-twin is enabled
```

If `--check` exits with 1, something is left: each line marked `!` says who does it and how. That is a
normal result while your owner still has to download botport or paste instructions; say so plainly.
`hermes twin doctor` shows one "pair set up" line per twin, without its name, and a line about `gh`
(GitHub's own tool, used only if your owner asks for the GitHub check of botport).

## Undo

| Your owner wants | Command (show `--dry-run` first) |
|---|---|
| One twin to stop following | `hermes twin stop "Acme Support"` (its Hermes bot and chats stay) |
| This computer out of the link | `hermes twin unlink` (its twins are kept for 30 days) |
| Sharing off | `hermes twin share off` (nothing is removed anywhere) |
| Everything of the plugin gone | `hermes twin uninstall`, then `hermes plugins remove grok-bot-twin` |

## Other ways in

- **Inside Hermes.** The plugin ships a skill, `grok-bot-twin:set-up-grok-bot-twin`. In a Hermes chat your
  owner can say "set up Grok Bot Twin" (or type `/grok-bot-twin:set-up-grok-bot-twin`), and Hermes walks
  them through the same command. Its text is in
  [skills/set-up-grok-bot-twin/SKILL.md](skills/set-up-grok-bot-twin/SKILL.md).
- **Designs, not built.** [docs/SETUP-ORIGINS.md](docs/SETUP-ORIGINS.md) describes two more: Hermes doing the
  clicks in the Grok app with its computer-use tool while the owner approves each step, and a Grok bot
  setting itself up as the sync bot ([docs/MIRROR-SELF-SETUP.md](docs/MIRROR-SELF-SETUP.md)). Neither has
  been run on the real Grok app. Do not try them on your own.

## If you are here to change the code

- Layout: `__init__.py` registers tools, hooks, the `hermes twin` command and the skill; `twin/` is the
  plugin (`onboard.py` is the setup plan); `dashboard/plugin_api.py` is the local API the Twins page calls;
  `desktop/plugin.js` is the page; `tests/` has the Python tests and `tests/desktop/` the page's.
- Tests: `python -m pytest -q tests` (Python 3.11 or newer with `pytest` and `pyyaml`; add `fastapi` and
  `httpx` to run the API tests too) and each file in `tests/desktop/` with `node FILE --modules DIR`, where `DIR` is a
  `node_modules` folder with React 19.
- Tests use invented bots only ("Acme Support", "Recipe Planner", "Test Scout") and a throwaway Hermes
  folder. Never point a test at a real Hermes folder or at the Grok app.
- The words on the page and in the terminal are part of the product, and tests check many of them. Keep
  the vocabulary: twin, Hermes bot, the Twins page, skill sync, the sync bot, the pass, botport, link code.
- Everything the plugin reads, writes, runs or sends is listed in [docs/DISCLOSURE.md](docs/DISCLOSURE.md)
  and in `plugin.yaml`. A change that adds one must add it there too.
