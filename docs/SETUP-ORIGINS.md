# Setup, from wherever you start

**Status of this document.** Ways 1 and 2 below are built and were tested in a throwaway Hermes home with
invented bots: one agent that was given only `AGENTS.md`, and one that was given the skill's text in place of
Hermes' own chat model, each took a Hermes that had never been opened to twins and a link code, stopping for
the owner's download and the owner's yes; a Linux computer was then linked with the same command. Ways 3a
and 3b are DESIGN ONLY. Nothing in sections 3a and 3b has been run against the real Grok app. The test plan at
the end needs the owner's OK before any of its steps touches the Grok app.

## Four ways in, one command

People arrive at Grok Bot Twin from different places: a coding agent that was pointed at this repository, a
Hermes chat, the Grok app itself. Every way in ends in the same command:

```sh
hermes twin setup --check     # where this computer stands and what is left; changes nothing
hermes twin setup --plan      # exactly what a run with the same options would do; changes nothing
hermes twin setup             # does what is left; asks once, or takes --yes
```

The command is safe to run again: a step that is done is skipped. It prints no bot name the caller did not
type (twins are counted, never listed), and it never repeats a link code that was typed. So its output can be shown in a
chat with an agent.

| Way in | Who drives | Status |
|---|---|---|
| 1. A coding agent pointed at this repository (`AGENTS.md`; `CLAUDE.md` points to it) | The agent, in your terminal, after you said yes to the printed plan | Built. Tested in a sandbox. |
| 2. Hermes pointed at this repository: the skill `grok-bot-twin:set-up-grok-bot-twin` (in a chat: `/grok-bot-twin:set-up-grok-bot-twin`) | Hermes, in a chat with you | Built. Tested in a sandbox. |
| 3a. Hermes does the Grok clicks for you | Hermes' own computer-use tool, one approved step at a time | DESIGN ONLY (this document) |
| 3b. Your Grok bot sets itself up as the sync bot | A Grok bot, following [MIRROR-SELF-SETUP.md](MIRROR-SELF-SETUP.md) | DESIGN ONLY |

Ways 1 and 2 never touch the Grok app. They install nothing in Grok, change nothing in Grok and read no chat.
The only part of setup that happens inside the Grok app is skill sync, which is optional: it needs a small
sync bot in your own Grok account (botport's own documents call it the Mirror bot). Today you make that bot by
hand, with the guided sheet on the Twins page (a twin's … menu, Skill sync…). Ways 3a and 3b are two ideas for
taking some of that clicking off you.

## 3a. Hermes does the Grok clicks for you (design)

### What it would be

In a chat, you ask Hermes to make the sync bot for you. Hermes then uses its own computer-use tool to do the
few clicks in the Grok app that you would otherwise do by hand. You watch the Grok window. Before every click
Hermes says in the chat what it is about to do, and Hermes' own approval question appears. Nothing happens
until you answer it.

### What Hermes' computer-use tool is (facts read from Hermes 0.22.7)

- The tool is called `computer_use`. Its actions are: capture, click, double_click, right_click, middle_click,
  drag, scroll, type, key, set_value, wait, list_apps, list_windows and focus_app.
- It needs a separate driver program, `cua-driver`. Without it Hermes hides the tool. On macOS the driver app
  needs the Accessibility and Screen Recording permissions; they are granted to the driver app, not to Hermes
  (`hermes computer-use permissions grant`).
- Actions that only look (capture, wait, list_apps, list_windows) run without a question. Every action that
  does something (the clicks, drag, scroll, type, key, set_value, focus_app) asks for approval first.
- Each approval question offers four answers: once, session, always, deny. "Once" stores nothing. "Session"
  and "always" let later actions of the same kind run without a question.
- With approvals switched off (or in "yolo" mode) every action runs without a question.
- A few key combinations are always refused by the tool (the ones that lock the screen, log out or force a
  restart), and a few dangerous command-like texts cannot be typed.
- The tool has no rule of its own against typing a password or a key, no list of apps it must stay out of, and
  no limit on the number of steps.
- You can interrupt at any time: answer deny, use Watch / Take over in Hermes Desktop (while you hold control
  every action is refused, captures too), or type `/stop`.
- A plugin can register a `pre_tool_call` hook. For any tool call the hook can answer "block" (the call is
  refused, with a sentence) or "approve" (the human question is forced for this call).

The last three points are why this design adds a guard of its own: the tool alone does not promise what this
flow has to promise.

### What the design promises

1. **Opt-in.** The flow is off. It starts only when you start it, in a chat, and only for that chat session.
   The model never starts it on its own. A request sent from another of your computers never starts it. A
   routine never starts it. When the session ends, it is off again.
2. **You approve each step.** The flow tells you to answer "once" every time. The guard also forces the human
   question on every action that does something, through the hook's "approve" answer, so that an earlier
   "session" or "always" answer cannot let a click through unasked. That is the intent of the design; whether
   the forced question really overrides an earlier grant is UNVERIFIED and is test T3 below. If approvals are
   switched off, the flow refuses to start.
3. **A fixed list of steps.** The guard knows the few steps in the table below and nothing else. Each step
   has: the sentence Hermes says in the chat before it, the screen it expects before, the one action, and the
   screen it expects after. An action that is not the next step on the list is blocked.
4. **Stop on anything unexpected.** A sign-in window, an update prompt, a payment or credit dialog, a
   permission dialog, a different bot open, an element that is not where the step expects it: Hermes stops,
   does nothing more, and hands back with one sentence that says what it saw and which step is left for you.
   It does not try to close the dialog, and it does not try again by itself.
5. **It never types a secret.** The only texts it may type are texts the plugin generated and already shows
   you on the Twins page: the sync bot's instructions, the routine prompt and the first message. The guard
   refuses a `type` or `set_value` whose text is not exactly, byte for byte, one of those texts, and it
   refuses any text that the plugin's own password-and-key check takes for a key. It never types a password.
   It never types, reads or handles the pass: the pass is a file that YOU attach by hand.
6. **It looks at one bot only.** Hermes opens the new sync bot and nothing else. It never opens, reads or
   captures another bot's chat. Before it starts, it asks you to close every other Grok window.
7. **The privacy cost, plainly.** To know where to click, the tool takes pictures of the Grok window. The
   picture files stay on your computer, but each picture is sent to the model provider of that Hermes chat as
   an image, like any other message. Whatever is visible in the Grok window at that moment is in the picture.
   This is the main privacy cost of the flow, and the reason for point 6.
8. **Credit.** Every run of the sync bot's routine spends Grok credit, and so does the Test run. The flow
   says so in the chat before it creates the routine, and again before it presses Test.

### In scope, and not

In scope are only the steps of skill sync setup that you click by hand today:

- create a new bot, with the name you chose;
- paste its instructions;
- create its routine, with the generated prompt and the schedule you picked;
- press Test on that routine.

Never in scope: deleting anything; opening or editing a bot that already exists; connecting Google Drive or
any other connection (signing in is yours); attaching files (the pass and the other files are attached by
you); anything about payment or plans; anything outside the Grok app.

### The steps

"Sync bot" below is the name you chose for it. The exact labels of the Grok app's buttons are not written
down here on purpose: they change between versions of the app, and the guard would carry them per version.

| Step | What Hermes says first | Action | Expected after | On anything else |
|---|---|---|---|---|
| 0. Look | "I will look at the Grok window. Close other Grok windows first." | capture (no question) | The Grok app's list of bots, signed in, no dialog open | Stop: "The Grok app is not showing your list of bots. Nothing was clicked." |
| 1. New bot | "I will press the button that makes a new bot." | one click | An empty new-bot form | Stop: "A window I did not expect opened. Nothing more was clicked; close it or carry on by hand." |
| 2. Name it | "I will type the name you chose: NAME." | one set_value into the name field | The name field shows NAME | Stop: "The name field did not take the name." |
| 3. Instructions | "I will paste the sync bot's instructions, the same text the Twins page shows you." | one set_value into the instructions field | The field holds that text, same length | Stop: "The instructions did not go in whole. Paste them by hand from the Twins page." |
| 4. Save | "I will save the new bot." | one click | The new bot is open, under NAME | Stop: "The bot was not saved, or another bot is open." |
| 5. Hand over | "Your turn: attach the pass and the other files in this bot's chat. Tell me when that is done." | none | You say so in the chat | Hermes waits. It never attaches a file. |
| 6. New routine | "I will open this bot's routines and press the button for a new one. Each run of this routine spends Grok credit." | two clicks, each with its own question | An empty routine form of the sync bot | Stop: "This is not the sync bot's routine form." |
| 7. Fill it | "I will paste the routine prompt and set the schedule you picked: SCHEDULE." | one set_value, then the clicks the schedule needs, each asked | The form shows the prompt and SCHEDULE | Stop: "The schedule on screen is not the one you picked." |
| 8. Save | "I will save the routine." | one click | The routine is listed for the sync bot | Stop: "The routine was not saved." |
| 9. Test | "I will press Test. This run spends Grok credit." | one click | The routine shows a run in progress | Stop: "I could not press Test. Press it yourself, then press Check on the Skill sync… sheet." |
| 10. Done | "Done. The Skill sync… sheet on the Twins page now checks the connection." | capture (no question) | Nothing further | None |

After every action the guard looks again (a capture) and compares with "Expected after" before it allows the
next step. If the look fails, the flow ends there.

### What is built today, and what this needs

| | Today | What 3a would add |
|---|---|---|
| Who clicks in the Grok app | You, guided by the Skill sync… sheet | Hermes, one approved step at a time |
| Does the plugin drive the Grok app? | No. Nothing in the plugin drives the Grok app. | Still no: Hermes' own tool drives it; the plugin only guards |
| Hooks the plugin registers | `pre_llm_call`, `on_session_start`, `on_session_end` | One more, `pre_tool_call`, listed in `provides_hooks` and in the disclosure |
| What the guard does | Not there | Blocks every `computer_use` action outside the list while the flow is on; forces the question on each allowed one; checks typed text |
| When the flow is off | n/a | The guard does nothing at all; `computer_use` behaves as Hermes ships it |
| Texts typed | n/a | Only the three generated texts the Twins page already shows |

### Why this is not built yet

- **The Grok app changes.** Its screens and labels differ between versions. A guard that expects a certain
  screen must be kept up to date per version, and must stop, not guess, on a version it does not know.
- **Test may not be reachable.** In one earlier trial, on one version of the Grok app, the routine's Test
  button could not be reached through automation. That is one observation on one version and is UNVERIFIED for
  the current app. If it still holds, step 9 becomes "your turn".
- **Nothing runs against the real Grok app without the owner's OK.** That rule holds for everything in this document.
  Until the test plan below was run with that OK, this stays a design.
- **The forced question is unproven.** Point 2 above rests on the hook's "approve" answer overriding earlier
  grants. Until T3 shows it does, the flow could not honestly promise "you approve each step".

## 3b. Point your Grok bot at the repo (design)

The other idea: you make an empty bot in Grok, point it at this repository, and it follows
[MIRROR-SELF-SETUP.md](MIRROR-SELF-SETUP.md) to set itself up as the sync bot. No tool clicks in the Grok app.
The bot does what a bot can do from the inside, and tells you exactly what is left for you.

A Grok bot cannot do all of it. The honest table:

| Step | Who | How sure |
|---|---|---|
| Make the bot | Owner only | Documented; a bot cannot make itself |
| Attach files in its chat (the guide, botport's release file and checksum file, the pass) | Owner only | Documented |
| Connect the shared folder (Google Drive or similar; signing in) | Owner only | Documented |
| Check the checksum of botport's release file and install it in its own workspace | The bot | Seen working once in an earlier trial |
| Write a skill into its workspace | The bot | Seen once in an earlier trial; the new skill did not show in the open chat's list |
| Set its own description when asked | The bot | Seen in an earlier trial; the wording was changed or left out, so not reliable |
| Set its own instructions | Unknown | UNVERIFIED |
| Create or edit its own routine | Unknown | UNVERIFIED |
| Press Test on its routine | Unknown | UNVERIFIED |

So the document tells the bot to do the first group itself, to ask you for the second, and never to claim a
step from the UNVERIFIED group: it may try one only when you ask, and must then tell you what to look at in
the Grok app, because a bot saying "done" is not proof.

Costs to know before trying it: every chat turn with the bot and every run of its routine spends Grok credit.
The pass still reaches the bot as a file you attach, never as pasted text, and a copy of it then lives in that
bot's chat ([DISCLOSURE.md](DISCLOSURE.md#known-residual-the-pass-in-the-sync-bots-chat) says the same of today's setup).

## Test plan (needs the owner's OK before any step runs on the real Grok app)

Nothing below has been run. T1 needs no Grok app and could run today. Every other step needs the owner's OK
first, and the owner at the computer.

### What is used

- ONE invented bot, "Test Scout" (description: "Finds trails."), made by the owner for this test.
- ONE invented sync bot, "Test Sync", made during the test.
- A Grok account the owner chooses: best a second, fresh account with no bots and no connections of its own,
  on a computer other than the one that holds the owner's real bots. The owner signs in; nobody else does. No
  existing bot is opened, changed or read.
- A throwaway Hermes home, not the owner's own, with Grok Bot Twin installed in it.

### Before anything starts

1. The owner is present and watches the Grok window the whole time.
2. Every other Grok window is closed, and no other bot's chat is open.
3. Hermes' approvals are in manual mode. No question is ever answered "session" or "always".
4. The owner knows where Take over and `/stop` are, and has tried both once on a harmless action.
5. The credit shown in the Grok app is written down (for M3 and the cost table below).

### 3a: Hermes does the clicks

| Test | What is done | Needs the Grok app | Passes when |
|---|---|---|---|
| T1 | Unit tests of the guard against a stand-in screen (recorded element lists, no app): every listed step in order is allowed; every other action, an out-of-order step and a typed text that differs are blocked | No | All tests pass; no path lets an unlisted action through |
| T2 | Dry run on the real app: every step is said in the chat, the only tool calls sent are captures | Yes (looks only) | The Grok app is unchanged; each sentence matches the table |
| T3 | Live run of steps 0 to 10, each answered "once". Then, on purpose, one earlier grant is given ("session" for clicks, on a harmless click outside the flow) and the flow is started again | Yes | "Test Sync" exists with the generated instructions and one routine with the picked schedule; every action had its own question, also after the earlier grant |
| T4a | A dialog the flow does not expect is open on purpose (for example the app's settings window) | Yes | Hermes stops with its one sentence and clicks nothing |
| T4b | The guard is handed a text that differs from the generated instructions by one character | No (stand-in) | The `set_value` is blocked: "That is not the text the Twins page shows." |
| T4c | The guard is handed a harmless, obviously fake key-shaped text (the plugin's own sample fake key) | No (stand-in) | Blocked: "That looks like a password or key. I never type those." |
| T4d | The owner answers deny at step 4 | Yes | Nothing is saved; Hermes says which step is left and stops |
| T4e | The owner presses Take over during step 7 | Yes | Every further action is refused until the owner hands back; the flow does not start again by itself |
| T5 | Clean-up, by the owner, by hand: delete the routine of "Test Sync", then "Test Sync", then "Test Scout" | Yes | None of the three is left; the owner confirms by looking |

Recorded for each test: the chat transcript of the throwaway Hermes home, the guard's own log (step, action,
allowed or blocked, the reason), the Grok app version, and the time each step took. Captures are looked at on
the computer and then deleted by the owner; none is kept in the record.

### 3b: the bot sets itself up

| Test | What is done | Passes when |
|---|---|---|
| M1 | The owner makes "Test Sync", attaches the files and points it at MIRROR-SELF-SETUP.md. For each step of that document the outcome is written down as one of: did it (seen in the Grok app), refused, or said it did without effect | Every step has an outcome that was checked in the Grok app's own screens, not taken from the bot's word |
| M2 | The routine question on its own: the owner asks "Test Sync" to create its routine, then to change its schedule. The exact outcome is written down, with what the Routines screen shows afterwards | There is a clear answer (can, cannot, or only says so) for both creating and editing |
| M3 | The credit shown in the Grok app is read again and compared with the number written down before | The difference is recorded per part of the plan |

M1 and M2 turn the three UNVERIFIED rows of the 3b table into facts. Whatever the answers are, they are a
result: "it cannot" is as useful as "it can".

### Credit

Every chat turn in Grok and every routine run spends Grok credit. A rough count for the whole plan, in turns
and runs, not in money:

- 3a: no chat turns in Grok; 1 Test run in T3, and at most 1 more if T3 is repeated after the earlier grant.
- 3b: about 10 to 15 chat turns with "Test Sync" for M1, about 4 for M2, and 1 or 2 routine runs.
- Total: about 15 to 20 chat turns and 2 to 4 routine runs.

### Cost table (filled in during the run)

The owner reads the credit in the Grok app before and after each line, so that the README can say what skill
sync costs instead of "spends Grok credit":

| What | Credit before | Credit after | Spent |
|---|---|---|---|
| Making "Test Sync" and its first message (the setup itself) | | | |
| One routine run with nothing to do | | | |
| One routine run that carries one skill to Hermes | | | |
| One routine run that installs one skill from Hermes | | | |
| M1 and M2 together (the bot setting itself up) | | | |

### Stop the whole plan when

- anything of the owner's own shows in a capture (another bot, another chat): stop, close it, start again
  only from the preconditions;
- an action runs without a question having been asked;
- Hermes clicks, types or scrolls anywhere outside the step it announced;
- a payment, plan or sign-in window appears at any time;
- the Grok app asks to update itself;
- the owner says stop, for any reason.

After a stop the owner does clean-up T5 by hand. Nothing is retried without a new OK.

## Decisions the owner has to make

1. Is it OK to run this test plan on the real Grok app, with "Test Scout" and "Test Sync" only?
2. In which Grok account?
3. Should a guard hook (`pre_tool_call`) ship in the plugin at all? Decided: no. It widens what the plugin
   registers and must be disclosed, for a flow that only some owners would use; the plugin registers no such hook.
4. If T3 shows that an earlier "session" or "always" answer does let an action through unasked: drop 3a, or
   ship it only with a start-up check that refuses while such a grant exists?
5. If M2 shows that a bot cannot create its own routine: keep 3b as "the bot does the inside, you do three
   clicks", or drop it in favour of the guided sheet alone?
