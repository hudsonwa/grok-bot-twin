# Grok Bot Twin: disclosure

What the plugin does on your machine, for the Hermes plugin catalog's rule 13 and for you. Unofficial; not
affiliated with xAI.

## Processes it starts

All with an argument list (never a shell), stdin closed, a timeout and capped output. botport, `gh`,
`hermes cron`, `systemctl` and `launchctl` get a minimal environment (no tokens or keys are passed on). The
system's own tools that only look or manage the helper's task (`lsof`, `ss`, `netstat.exe`, `tasklist.exe`,
`schtasks.exe`, `whoami.exe`, `taskkill.exe`) are started from fixed system folders with the environment this
Hermes process has. A request's chat turn is Hermes itself and gets the environment described in its row.

| Command | When | Why |
|---|---|---|
| `botport ...` (a private, digest-checked copy of the botport you had verified) | status, checks, the background check, every Twins page action | the sync engine |
| `botport version --json` | "Verify botport" on the Twins page or `hermes twin setup`, run on the private copy only after it was verified (its SHA-256 is on the list of release files that ships with this plugin version, or its GitHub build attestation passed), or after your explicit `--allow-unattested` opt-out | check schema and features before using it |
| `gh attestation verify <botport> --repo hudsonwa/botport --signer-workflow hudsonwa/botport/.github/workflows/release.yml --source-ref refs/tags/<release tag>` (the last two words only when the release tag is known) | only when you ask for the GitHub check (on the Twins page, with `hermes twin setup --with-github` or with `hermes twin verify`), or, if `gh` is already installed, for a botport file that is not on the plugin's list of release files (these call the `engine/trust` / `engine/attest` routes of the plugin's local API) | verify botport against its GitHub build attestation |
| `hermes [-p PROFILE] cron resume ID` | only when you press "Move routines to Hermes" for a routine of this twin (found by its job id, recorded when the twin was made, or by the importer's mark on the job; never by its name), and only when Hermes offers no tool-approval step to the plugin (when it does, the resume goes through that step instead, and a refusal there stops the move) | turn on the Hermes copy of a routine |
| `hermes -p PROFILE chat -Q --query-file -` (this computer's own Hermes: the program this process runs in, else `hermes` from PATH after the same ownership check as botport) | only on a computer where "Let my other computers ask this one to do things" is on, once per request another of your linked computers sends | run the request as one ordinary chat turn of the twin's own Hermes bot. This one is different from the rows above in two ways: the request is passed on standard input, and the turn gets this process's environment (it is Hermes itself and needs your model login), minus the variables through which a running Hermes session could pass on a looser approval state (`HERMES_YOLO_MODE`, `HERMES_INTERACTIVE`, `HERMES_GATEWAY_SESSION`, `HERMES_EXEC_ASK`, `HERMES_SESSION_PLATFORM`, `HERMES_CRON_SESSION`, `HERMES_SINGLE_QUERY_SESSION`, `HERMES_ACCEPT_HOOKS`, `HERMES_SESSION_KEY`, `HERMES_SESSION_ID`). No `--yolo`, no `-z`, no toolset or approval option is ever added. Stopped after 180 seconds, or at once when you switch the switch off. Used when the process that works on requests is not Hermes itself (the standalone runner), or when the interface in the next row did not start |
| `python -P <plugin>/twin/turn_bridge.py` (a 110-line program of the plugin's own, started with the Python this Hermes process runs on, `HERMES_HOME` set to the twin's own Hermes bot, in your home folder, same cleaned environment as the row above; `-P` keeps the plugin's folder off its import path) | the same switch, the same occasion, when the process that works on requests is Hermes itself: tried first | attach to the Hermes that is already running on this computer (its Desktop is open, a chat was just used), the way `hermes chat` does: through Hermes' own client (`hermes_cli.gateway_client.connect_gateway`), over a local socket that only your own user can open and a one-time ticket, then a loopback connection. If no Hermes is running, that client starts one that ends by itself when idle. The program only copies messages between the plugin and Hermes; it holds no answer of its own. The plugin sends the same four kinds of message as in the next row; an approval question arrives as an event and is answered with Hermes' own `approval.respond`, only with what you chose on this computer ("once" or "deny") |
| `python -P -m tui_gateway.entry` (Hermes' own interface for programs, the one its terminal UI and Desktop speak; started with the Python this Hermes process runs on, `HERMES_HOME` set to the twin's own Hermes bot, in your home folder, same cleaned environment as the row above) | second, when attaching (the row above) is not possible: a Hermes without that client, or one that cannot be attached to | run the request as one chat turn in which Hermes can ask before a risky step. The plugin sends four messages (what it can answer, open a chat, the request, and at most one answer per approval question) and reads the chat's events until the answer is complete. To an approval question it sends only what you chose on this computer, "once" or "deny"; it never answers by itself, never "for this session" or "always"; a request for a password or a secret is declined at once, and a question the twin puts in words ("which one?") gets an empty answer, so nothing is made up for you. Stopped after 180 seconds of work (waiting for you is not counted; Hermes' own approval time limit, 300 seconds by default, ends the wait), or at once when you switch the switch off |
| `hermes [-p PROFILE] cron pause ID` | only when you press "Pause routines" after "Move routines to Hermes" (or run `hermes twin switch --undo`), for exactly the routines that move turned on; same id rules and the same approval step | pause those routines in Hermes again |
| `hermes -p PROFILE cron create --help` | only when you press **Import** on a setup export that holds a routine to add (or confirm `hermes twin import`); asked once per Hermes program while it runs | see whether this Hermes can make a routine switched off (`--paused`) and keep its time zone (`--timezone`); when it cannot make one switched off, no routine is made |
| `hermes -p PROFILE cron create --name=NAME --deliver=local [--timezone=ZONE] --paused --paused-reason=TEXT -- SCHEDULE PROMPT` | the same moment, once per routine of the file that has a five-field schedule and passed the password-and-key check | add that routine to the twin's own Hermes bot, switched off. Every value is one `--option=value` argument and the two free texts come after `--`, so nothing in the file can be read as an option. This is Hermes' own command, not its tool-approval step: a switched-off routine does nothing, and turning it on ("Move routines to Hermes") does go through that step. Afterwards the plugin reads the job list: every routine that is there and was not there before, whatever its name, must be switched off; one that is not is paused at once (`cron pause`), and counts as not added when that fails. While the command runs (seconds; at most 60), the routine's name and prompt are in its argument list, which other users of this computer can read. One import spends at most 90 seconds adding routines; the rest is added by importing the same file again. Only the command's exit code is used; what it prints is not kept |
| `systemctl --user daemon-reload`, `enable`, `restart`, `disable --now`, `reset-failed`, `show` (Linux; `systemctl` from `/usr/bin`, `/bin` or `/usr/local/bin` only, never looked up on PATH) | only when you turn the background helper on or off (`hermes twin background on` / `off`, unlinking this computer, Remove Grok Bot Twin), and `show` when its state is asked for | set up, start, stop and remove the one systemd user unit described under "The background helper"; never as root, never `sudo` |
| `lsof +c 0 -nP -iTCP:<share port> -sTCP:LISTEN -Fc` (macOS, Linux; `/usr/sbin/lsof` or `/usr/bin/lsof` only), else `ss -H -ltnp "sport = :<share port>"` (Linux; from `/usr/bin`, `/usr/sbin`, `/sbin` or `/bin` only); on Windows `netstat.exe -ano -p TCP` and `tasklist.exe /FI "PID eq <number>" /FO CSV /NH` from the Windows system folder | only on the computer that shares, only while sharing cannot start because another program holds its port, at most once a minute, 3 seconds at most each | to name that program in the sentence ("Music Box on this computer is in the way of sharing. Quit Music Box; …"). Only the program's name is read from the answer; it is shown on this computer's Twins page and in its terminal, kept for a minute in memory, never stored and never sent anywhere. A name that is not plain letters, digits, spaces, dots, dashes, plus signs and underscores (40 at most), or that is a language's runner or a part of the system, is not shown. The system only names programs of your own account. |
| `launchctl bootstrap`, `enable`, `bootout`, `print` (macOS; `/bin/launchctl` only) | the same moments | set up, start, stop and remove the one launchd agent described under "The background helper" |
| `hermes twin fleet run` (started by systemd or launchd, not by the plugin's own process) | while the background helper is on | follow the computer with the Grok app while Hermes is closed: outgoing connections only on a linked computer. It is the same loop an open Hermes runs, so with requests switched on it also works on requests, and on the computer that shares (where the helper is not meant to be used, but is not refused) it keeps sharing going and so listens on the share address |
| `hermes -p PROFILE sessions import --from claude FILE`, then `hermes -p PROFILE sessions rename ID TITLE` (the same Hermes command and checks as the cron rows above; FILE is a file of the plugin's own, 0600, in `transcripts/outbox/`, deleted right after) | only when full transcripts are on for that twin and you run `hermes twin transcripts to-hermes`, once per kept Grok conversation not imported before | make a Grok conversation an ordinary Hermes chat of the twin, through Hermes' own documented importer (it reads a JSONL chat log); the new chat is renamed "From Grok: " plus its first line, cleaned to one line of at most 100 characters (control characters dropped, links, paths and password-like text masked) |

The repository also has a `tools/` folder with four programs for releases and tests (`pin_botport.py`,
`fleet.py`, `fleet_fixture.py`, `routines_fixture.py`). The plugin never runs them, and they are not in the copy
it puts into a twin's Hermes bot. Run by hand, `pin_botport.py` runs `gh attestation verify` on release files
you downloaded yourself; the other three work only in a Hermes folder named on their command line.

botport is never downloaded, bundled or updated by the plugin. The one thing the plugin does with a download is
copy it: when you press "Choose the file…" or "Use it" on the Twins page (or run `hermes twin setup --file
FILE`), it reads the file you picked (a regular file only, no links, at most 64 MB; the file is opened first
and judged as opened, so nothing can be swapped in between; from the release archive, a gzip or plain tar
archive only, the one member named `botport` is read, and the archive is never unpacked to disk), compares its
SHA-256 with the list of release files that ships with this plugin version, and on a match copies it into
`helper/botport` in its own data folder. A file that does not match is not kept. Nothing is run before a match.
On macOS the copy is a new file, so it does not carry the download mark (quarantine) your browser put on the
original: Gatekeeper and Apple notarization do not vet botport. What stands in for them is this check: the copy
is run only if its SHA-256 is on the list of release files that ships with this plugin version, or has a GitHub
build attestation from botport's release workflow at the pinned release (or after your explicit opt-out, below).
For a release the list does not cover (another release named in the `botport_release_tag` setting, or a plugin
build whose list has no entry for its release), the file is copied and stays "not verified yet": it is not run
until the GitHub check passed (or you opted out, below).
"Download botport" hands the address of botport's release file (or of its release page) to your browser; the
plugin itself fetches nothing.

Before every run the plugin re-checks the
binary's SHA-256 against the record made at setup, that it and its folders are owned by you or root and not
writable by others, and that no link was swapped in; it fails closed and says why on the Twins page. A check
record that is neither on the plugin's list nor has a verified attestation is used only when you opted out explicitly with `hermes twin setup --allow-unattested`
(a loud, recorded opt-out for local builds: a build of your own); doctor and status then show "botport is in use WITHOUT being verified (local build)".

`gh` and `hermes` are found on `PATH` and resolved to their absolute real paths (inside a Hermes process
the Hermes CLI is Hermes' own Python with `-P -m hermes_cli.main`: the folder of the Hermes that is running is
named to it and checked like botport's, and the working folder is kept off its import path, so a `hermes_cli`
folder lying wherever Hermes was started is never run). Desktop apps start with a short `PATH`, so
`gh` is also looked for in `/opt/homebrew/bin` and `/usr/local/bin`, and botport in the copy you chose
(`helper/botport`), then on `PATH` and in `~/.local/bin`. Each must pass the same check as botport (the
file and every folder above it owned by you or root, not writable by group or others) every time before it
runs; otherwise it is refused with a message saying why. `hermes twin doctor` shows both paths and the result.

## Files it reads

- Its own data folder (below).
- Only for a twin whose session briefs go Hermes → Grok (off for every twin until you choose it): that twin's
  session database `state.db` in its Hermes bot, opened read-only (nothing in it is changed), for chats that ended
  after you turned briefs on, and only chats on this computer (Hermes' terminal, terminal window or Desktop; never a
  chat that came through a messaging app, a webhook or the API server): the words of you and the twin only, never
  tool output, the system prompt or the model's reasoning. Briefs from Grok are read only through the transport you set up (the Mirror), at most 16 KiB each.
- Only for a twin whose memory you switched on: that twin's `memories/MEMORY.md` and `memories/USER.md` in its
  Hermes bot, and two numbers from that bot's `config.yaml` (how many characters Hermes lets each file hold).
- Only for a twin whose full transcripts you switched on: that twin's Hermes session database (`state.db` in its Hermes bot), opened read-only, user and assistant messages only (no tool output, no hidden reasoning; reasoning a model wrote into its reply between think or reasoning tags is dropped too), when you run `hermes twin transcripts refresh`; and a Grok export with conversations that you hand over (`hermes twin transcripts add-grok --file FILE`, a plain file of at most 64 MB) or that the Mirror reads for you once it is set up.
- A setup export, only when you hand one over: the file you drop on the import sheet of the Twins page (the page
  reads it and sends its text to the plugin's own local route) or name to `hermes twin import FILE` (a regular
  file, read once, at most 8 MB; the text at most 2,000,000 characters). From the bot it names the plugin takes
  `name`, `instructions`, `routines` (`name`, `instruction`, `trigger`, `schedule_cron`, `schedule_text`,
  `timezone`) and the `name` of each entry in `apps`. `memories` and `user_facts` are decoded with the rest of
  the file, measured (how many entries, when they are lists) and never looked into; `skills` are counted; every
  other key is ignored. A name, a schedule or a time zone is cut to 2,000 characters before anything looks at it. The text is used for the length of that one call:
  it is not written to the data folder, to a temporary file, to the event list or to a log, and it is sent
  nowhere. The page holds it in the open sheet only (not in its storage) and lets go of it when the import is
  done or the sheet closes.
- Its own installed folder: the names, sizes and dates of its files, at each timed check in every Hermes process
  and once a minute in a loop that keeps running (`hermes twin fleet run`, the background helper), to notice
  that you updated the plugin; and, on Linux, that loop's own `/proc/self/cgroup` once at its start, to know
  whether the helper started it (see "The background helper").
- Whether the Grok desktop app's folder exists (`grok_app_dir` setting, else `~/Library/Application Support/Grok Bot`
  on macOS, `~/.config/Grok Bot` on Linux): an existence check, for the first row of the setup checklist.
- In that folder's `sand-client-persistence` sub-folder, and only while you have a real twin and
  `watch_grok_changes` is on: the file names, and the date, size and file number of the bot-list file(s)
  (see "Background work"). The plugin itself opens no file of the Grok app for reading.
- The one file you pick with "Choose the file…" or "Use it" (read, and copied on a match, see above).
- While botport is not verified yet: whether botport's download for this computer is in your Downloads folder,
  looked up by its exact name only (the release file, the same name with " (1)" to " (9)", or the folder a
  browser makes when it unpacks it). No folder is listed and no file is opened for this.
  `hermes twin setup` looks in the same way, and a run of it takes that file only after you said yes (or gave
  `--yes`) to the line that names it; `hermes twin setup --check` and `--plan` only say that it is there.
- While the sheet for new twins watches for setup exports (after you pressed **Do all ticked now**, while that
  sheet is open, at most 15 minutes, every 4 seconds): the names, dates and sizes of the files in your Downloads
  folder (that one folder, not its sub-folders; links are not followed), and the text of each regular file
  that changed since the watching began, ends in `.json`, `.txt` or `.md` and is at most 2 MB (at most 40 files).
  With skill sync set up, the same watch also reads `<shared folder>/exports/` (files named
  `grok-bot-export-<bot id>.json` the sync bot copied up from the box, at most 2 MB, never moved or deleted; only
  digests are remembered).
  A file whose text is not a `grok-bot-export` document is not used. What is taken from an export is the same as
  for **Import setup export** below; the text is not stored. `hermes twin import-exports DIR` reads the folder
  you name in the same way, once.
- Grok's instructions kept in step (`twin/instr_sync.py`; every 15 minutes with the background check, and when
  you press Check now or run `hermes twin instructions check`), for each twin that follows Grok's instructions
  (on unless you switch it off for that twin): the names, dates and sizes of the files in
  `<shared folder>/exports/` while skill sync is set up, and the text of each file named
  `grok-bot-export-<bot id>.json`, at most 2 MB, at most 40 files (never moved or deleted). Downloads only after
  you turned on "also look in Downloads" (`hermes twin instructions downloads on`; off by default), and then only
  files saved after that moment and named `grok-bot-export-<bot id>.json`, with the same limits as the sheet above.
  Of a file only the bot's name, id and instructions are used (the instructions screened as an import screens
  them); memories, facts about you, routines and apps are not read into anything. It also reads each such twin's
  `SOUL.md`. Grok's instructions are saved in a twin by themselves only from a file in the shared folder named for
  the twin's own Grok bot id that holds that one bot with that same id, and at most 4 times a day per twin. Every
  other file that names the bot (by name only, among other bots, or under another bot's file name), every file
  from Downloads, and a fifth change in a day only show a card on the Twins page and in
  `hermes twin instructions`: you read Grok's text and choose to take it, keep Hermes' or save your own.
- Through botport, from the Grok desktop app's local files: the bot list (id, name, description) plus the
  selected bots' local transcript files, capped (200 files, 64 MiB) and parsed only for routine-change events
  (nothing else is kept or shown). botport never opens login or credential files (no other file in the app's
  folder: not its secrets file, cookies or any login file), and refuses a cache folder that is a link.
- The twin profile's cron job list (Hermes' cron store): for "Move routines to Hermes" and, with Routines
  switched on, its date and size every 30 seconds and its content when it changed (with Routines on it is
  changed too, see "Routines on your other computers").
- In a twin's own Hermes profile folder (opened without following any link, capped): `SOUL.md`, for the name
  and description botport keeps there and for whether the Hermes bot has instructions and, while sharing is on
  and Persona is switched on, the whole of its instructions; `config.yaml`, for whether a model is chosen and,
  while sharing is on, for the listed settings that follow a twin (see "Settings that follow a twin"); and,
  when you press "Copy the skill's text" for one skill, that skill's `SKILL.md`
  (refused if it holds something that looks like a password or key).
- With "Share my twins' skills with my other computers" on: the text files in each twin's `skills/` folder (see
  "Skills between your computers"). With memory switched on for a twin: that twin's two memory files (see "A
  twin's memory between your computers").
- When a twin is made: the model settings in the default Hermes profile's `config.yaml` (model ids and provider
  names only; see "Files it writes").
- Hermes' own `config.yaml` plugin settings (through Hermes' public config reader). A copy of the plugin that runs
  inside a twin's own Hermes bot reads the same settings from the main Hermes folder's `config.yaml` (the plugin's
  own entry only), so every copy on a computer knows it by the same name and uses the same share port.
- When a kept twin comes back and a folder sits at its old name: that folder's file names, its `SOUL.md` (compared
  with Hermes' own starting text), `cron/jobs.json` and, opened read-only, its session database (only whether it
  holds a chat), to tell a folder Hermes made again by itself from a Hermes bot of yours.

It never reads Hermes' `.env` or other credential files, keychains, browser profiles or any credential store.

## Files it writes

Under `plugin-data/grok-bot-twin/` in the Hermes root (shared by all profiles) (folders 0700, files 0600, atomic writes: temp file in the
same folder, fsync, rename; links are not followed):

| Path | What |
|---|---|
| `pairs.json` | the twins this plugin knows (ids, bot name, the bot's description as last read from Grok with password-like text hidden, profile, the ids of the cron jobs made from the bot's routines, recorded when the twin was made, after "Move routines to Hermes" when you moved them and which routines it turned on, whether you brought the instructions over or said there are none, and the model a new twin started with) |
| `status-cache.json` | last status, so the per-turn note never waits for botport |
| `waiting.json` | per twin, the item ids botport last listed as waiting for your go-ahead (so the "Send to Grok" boxes survive a refresh) |
| `skipped.json` | per twin, the item ids you chose to keep in Hermes only (so they stop asking; "offer again" removes the entry) |
| `copied.json` | per twin, the skills you said you pasted into Grok by hand: the item id and a short digest of the pasted version (no text), so the skill asks again when it changes. Also, for each routine an imported setup export added: its item id, which holds the routine's name, and a short digest of its schedule and prompt (no other text), so its Hermes copy does not ask to be carried back to Grok, where it came from, until you change it in Hermes |
| `kept.json` | per twin, the earlier versions of the instructions you pasted on the Twins page or imported from a setup export (at most 20; this is text you pasted in yourself or that an import saved, so after a second import it holds what the first one brought, kept so "Put back" works), and the versions an automatic change from Grok replaced (for a twin that follows Grok's instructions; then this also holds Grok's earlier instructions). A version Grok's automatic change wrote is marked so and goes first when there are more than 20, so changes from Grok never push out one of your own |
| `former.json` | which Hermes bot (profile name) a Grok bot's twin lived in before you stopped twinning it, so twinning that bot again picks the same Hermes bot up; ids and profile names only. After "Remove Grok Bot Twin" this record is gone with everything else. A bot twinned again after a new setup still gets its old Hermes bot instead of a second one ("name-2") when that Hermes bot's prompt file carries botport's own name block with exactly the Grok bot's name and no twin uses it; to see that, the plugin reads that one `SOUL.md` (read only). A Hermes bot without that block is never taken over |
| `stuck.json` | per twin, when botport refused to update the twin's Hermes bot because a line in one of its files looks like a password or key to botport: the file name and the line number only, so the Twins page can say which line |
| `points.json` | per twin, what took an earlier version's place (your paste, a change from Grok, your choice, a put-back, a send, an import): ids and one word, no text |
| `setup-export.json` | per twin, when you last imported a setup export and the connector names it listed to set up on this computer (names only, at most 50, links, addresses and anything password-like taken out; nothing else of the file). Stop twinning forgets it |
| `instructions-sync.json` | per twin that follows Grok's instructions: on or off, when its newest setup export was read and from where (shared folder or Downloads), a SHA-256 digest of that file's name, size and date, a 12-character digest of the instructions both sides last agreed on, when Grok's last change arrived and the id of the earlier version it left, and the line numbers left out because they looked like a password or key. the times of the automatic changes of the last day (at most 4 a day), and a 12-character digest of the text the last one wrote. While a card waits for your answer (the instructions changed both in Grok and in Hermes, or a file that is not taken by itself, see "Files it reads"): Grok's instructions as screened (no password-like line), their 12-character digest and why the card asks, so you can still choose them after the file changed; your answer "take Grok's" names that digest and is refused when a later export changed the card. Stop twinning forgets it |
| `instructions-sync-settings.json` | whether Downloads is looked in too, and since when |
| `batch-seen.json` | the setup export files the new-twins sheet already imported: a SHA-256 digest of each file's name, size and date (no name, no text), at most 500, so the same file is not imported twice |
| `events.jsonl` | last 500 events, metadata only (type, time, twin id, bot name, counts, item ids; never item text or secret values) |
| `activity.json` | per twin: time of the last Hermes chat turn and a count (no content) |
| `digest.json` | per-twin digest switches (off unless you turn them on) |
| `botport-state/` | botport's own state for your twins (passed as `--state-dir`) |
| `helper/botport` | the copy of the botport file you chose with "Choose the file…" (only if you did) |
| `botport-trust.json`, `botport-bin/` | the check record (path, SHA-256, version, how it was verified: the plugin's list or the attestation result, and whether you opted out) and the verified private copy of botport that is actually run |
| `demo/` | the sample (invented bots; it also remembers "Hide my real bots while the sample is on"); "End sample" or `hermes twin demo remove` deletes it, and so does removing the last sample twin |
| `sync-panel.json` | only once you changed a row of a twin's "What syncs" panel (Twins page or `hermes twin syncs`): per twin and item the direction you chose and when, and when each item last moved and which way. Never the content of anything. A removed twin's entries go with it |
| `.lock`, `fleet.lock` | empty files used as advisory locks so CLI, gateway and dashboard processes do not write at once |
| `ref-salt` | 32 random bytes made on this computer, from which the internal ids shown to the model (`t-` plus 16 hex characters) are made, so they cannot be matched to a bot by anyone else |
| `.write-test` | written and deleted at once by `hermes twin doctor`, to see that the folder can be written |
| `fleet.json` | only once you share twins with, or link to, another of your computers: this computer's id (random, made here) and name; on the computer that shares: the switch, the current link code until it is used or runs out (10 minutes), the keys of the last few used codes (so a second try says "already used"), the shared list (per twin: an id made for this purpose, name, description, version, date), removals (90 days), and per linked computer its id, name, kind, the key made from its pass (never the pass), when it last checked in, which list version it holds and the highest call number it has sent, plus for 90 days the key of a computer you unlinked, and, while sharing runs, a record of the Hermes process that holds the share port (address, port, process id, start time and a random word made for that run: other Hermes processes on the same computer show that word when they hand it something to pass on, it is accepted only from this computer's own address, and it never goes to another computer); on a linked computer: the sharing computer's id, name and address, this computer's pass for it, and per mirrored twin its name, description, version and Hermes bot |
| `fleet-kept/` | on a linked computer: the Hermes bots (whole profile folders, chats included) of twins that were removed or unlinked, kept 30 days so they can come back, then deleted |
| `fleet-follow.lock` | an empty lock file so only one Hermes process on a linked computer follows at a time |
| `briefs.json` | only once session briefs were turned on for a twin: per twin the two switches, since when, digests of the chats already briefed (with their message count), which briefs were handed to Grok and when, digests of briefs that came from Grok. Never a brief's or a chat's text |
| `briefs/<twin>/out/b-….md`, `briefs/<twin>/in/b-….md` | the session briefs themselves (a few lines: who, decided, open threads, next step; every line passed through the password-and-key check, links, e-mail addresses, paths, IP addresses and phone numbers replaced), made here for Grok (`out`) or received from Grok and checked again (`in`), at most 50 of each per twin, owner-only. `hermes twin briefs TWIN --forget` deletes them |
| `xs-memory.json` | only once memory was switched on for a twin on this computer: per twin the switch, when, digests (24 hex characters each) of the notes last agreed with the other computer and of the notes that came from elsewhere, which notes were removed where (digests, computer ids and names, times), open conflicts (digests), counts, and on the sharing computer the ids of the linked computers you let take part and the times of each one's last changes (for the daily limit). Also the ids of twins that left this computer, with the date. Never a note's text |
| `xs-memory-points/<twin id>/p-….json` | the earlier versions: that twin's two memory files as they were before the plugin changed one of them (the text of the notes): the newest 30 and the first of each day for 30 days, plus the one from before memory was first switched on. Owner-only. "Forget on this computer" deletes them for that twin; for a twin that is no longer on this computer they are deleted 30 days after it left |
| `xs-memory.lock`, `xs-memory-follow.lock` | empty lock files (one change of the notes above at a time; one Hermes process per computer asks) |
| `mem-facts/<twin>.json` | only once memory facts with Grok were switched on for a twin (0.3.0, off by default): per fact id a digest (24 hex characters) of the text Grok and Hermes last agreed on, which file it belongs to and Grok's own id for it; removed fact ids with the date (60 days); counts; and, only while a fact differs on both sides, the two versions (so you can choose; deleted when you do) |
| `mem-facts-points/<twin>/` | the twin's `MEMORY.md` or `USER.md` as it was before a facts sync changed it (the text of the notes), the newest 10 per file. Owner-only. Stop twinning deletes this folder, the twin's `mem-facts/<twin>.json` and its `mem-grok-stub/<twin>/` (the memory files stay) |
| `mem-grok-stub/<twin>/` | until the Mirror can carry memory to and from Grok: what would be sent to the Grok bot (`facts.json` and the list `outbox.jsonl`), on this computer only. Nothing in it leaves the computer |
| `mem-layers.json` | only in a build without the "What syncs" panel: the per-twin switches of the memory layers |
| `fleet-skills.json` | on the sharing computer only once the skills switch was touched; on a linked computer from its first contact with the sharing one (it then holds what that computer said about skills, which may be nothing): the switches (the whole one and one per skill), and per twin and skill its version number, a digest of its text, sizes and dates, and which version this computer holds. No skill text |
| `fleet-skills-kept/` | the versions of a skill that were replaced or taken away: on the sharing computer the text it sent as each version; on a linked computer the whole skill folder as it was before a change from the sharing computer, your choice or a switch replaced it. Kept 30 days, then deleted; of the copies of what the sharing computer sent only the newest 10 per skill, of folders that held your own text 40 per skill, 400 folders in all. Owner-only like the rest of this folder |
| `xs-config.json` | only once settings follow a twin or you touched one of their switches: this computer's switch per item (tools, preferences, persona, model), and on a linked computer what the sharing computer last said (a rev, its switches) and per twin a digest, a time, which items changed, the names of what is still to set up here, and, while the model follows, the names of the model and provider this computer had for that twin (so they can be put back when the model is switched off). No other setting's value is kept in it |
| `xs-config-points/<twin id>.json` | on a linked computer: what that twin's Hermes bot had before settings arrived (the listed `config.yaml` keys and the persona block of its `SOUL.md`), the last ten times, so "Put back" can return it. Never a key or sign-in: only the keys on the list are noted |
| `routines.json` | only once Routines was switched on, or a linked computer heard of routines: the two switches ("Routines", "Routines may run on this computer"); on the computer that shares: per routine its made-up id, the twin's id, Hermes' id of the job, name, prompt (after the password-and-key check), schedule, skill names, where it runs, and what each linked computer last reported (switches, applied list, ids switched on); on a linked computer: the list it holds and the id of each copy's job; and up to 30 routines as they were before the plugin changed or removed them there (30 days), so they can be put back |
| `tasks.json` | only once you touched the switch "Let my other computers ask this one to do things": whether it is on, and since when, and your choice for a step that needs approval when it is not the default ("safe"; a "linked" kept by an older version reads as the default). A missing or unreadable file means off and "Ask on this computer". Written only by this computer's own page and commands; nothing another computer sends reaches it. When this computer is unlinked the switch goes off and "linked" goes back to the default; "safe" stays |
| `tasks-seen.json` | only on the sharing computer, once requests are used: the numbers of the requests it accepted in the last 30 minutes (a computer's id, a random number, a time), so that a recorded request cannot run a second time after Hermes was started again (if this file cannot be read or written, only the memory of the Hermes that is running guards against that) |
| `tasks-turns.json` | only while a request's chat turn runs on this computer: the number Hermes gave that chat, so that nothing the chat starts can answer an approval question through the plugin or pass the request on (forgotten when the turn ends, or after an hour). When the turn has to run in Hermes' single-question mode, which gives no such number beforehand, the file holds the time that turn started instead, and until it ends (four minutes at most) no chat on this computer can do either; the Twins page and your own terminal still can |
| `tasks-log.json` | the newest 200 requests of this computer: asked from here (to which computer, which twin, the first 300 characters of the request and of the answer, how it ended, how long it took), asked of this computer (from which computer, the same fields), and on the sharing computer the ones it only passed along (who asked whom and how it ended; no text). Password-like text is masked before anything is written |
| `tasks-heard.json` | which of your other computers had their own requests switch on when this computer last heard (computer ids and true or false, nothing else): on the computer that shares from its own mailbox, on a linked computer from the sharing computer's signed answer (one optional key, `takes_requests`, on the "nothing changed" answer). Written only when it changed; emptied when this computer is unlinked. An unlink also writes this computer's own switch (`tasks.json`) off again, so a new link starts with requests off |
| `tasks-worker.lock` | an empty lock file so only one Hermes process on a computer works on requests at a time |
| `tasks-approvals.json` | only while a request's step waits for your approval on this computer: the request's id, the command and the short reason as Hermes shows them (password-like text masked, file paths shown, 400 and 300 characters at most), when it was asked, and your answer ("once" or "deny") until the waiting process has read it. Emptied when the request ends. Never sent anywhere. |
| `transcripts/` (0700) and `keys/` (0700) | only once full transcripts were switched on for a twin: `keys/transcripts.key` (32 random bytes, 0600, made here, never sent anywhere; kept outside `transcripts/`, so a copy of that folder alone cannot be read), `transcripts/archive.db` (0600: per kept conversation and message, the side (Grok or Hermes), role, time and the text sealed with the 0.2 memory box construction (SHAKE-256 stream + HMAC-SHA256, the tag binds twin, conversation and position: the plugin's own encrypt-then-MAC construction from standard primitives, not a packaged AEAD); a full-text index whose entries are keyed hashes of words, per twin, never the words), `settings.json` (per twin: the two switches, since when, and which kept Grok conversation became which Hermes chat: the Hermes session id of each imported chat, as Hermes names it). `exports/` holds JSON exports you asked for (decrypted, each a new file made 0600 from the start; an existing file or a link is never written to). Asking whether transcripts are on, or how many are kept, does not make this folder. **Forget** (`hermes twin transcripts forget --yes`) deletes a twin's rows (SQLite secure delete), its settings, the exports the plugin wrote for that twin in `exports/` and any import file of that twin left in `outbox/` (an export you wrote elsewhere with `--file` is your own copy and stays). Stop twinning does the same Forget for that twin. When no twin keeps anything any more, `archive.db` and the key are deleted too. An import file older than ten minutes (left by a stopped run) is deleted at the next import or Forget, whichever twin it was for. This protects copies of the archive (a backup, a synced folder, a copy of `transcripts/`); it does not protect against someone who can already read your files, or a copy of the whole plugin data folder, the key included. What a copy of `archive.db` still shows without the key: the twin's id, which side each conversation is from, when conversations and messages were started and written, who spoke (user or bot), how many messages there are and roughly how long each is (in steps of 64 bytes), how often each (unnamed) word hash occurs and which messages share one (within one twin), and a keyed hash of each conversation's Grok id or Hermes session id, which cannot be checked against a guess without the key |

Not in this folder: the Twins page keeps a few notes of its own in Hermes Desktop's storage for this plugin
(kept by Hermes Desktop, not by the plugin, under keys that start with `hermes.plugin.grok-bot-twin.`). Per twin: the boxes
you ticked in "Move routines to Hermes" (by item id or routine name); the skill sync guide (the three texts
for the sync bot, up to 40,000 characters each; the instructions name the shared folder and, only when it is
in your home folder, where it is, written as `~/...` without your login name, and not at all when that path holds
an `@`, lies under `Library/CloudStorage` or has a folder whose name starts with OneDrive or Dropbox; the shared
folder's name, where the set-up folder is on this computer, whether it was complete, which release file was
still missing with its download address, whether the twin joined through the shared folder, and whether the
set-up was finished; never a pass) and the step of it you were at; that you chose to wait for a bot
that left Grok; and that you were sent to choose a model. Besides: the list of twins something is remembered
for, the Hermes profile your twins are looked after in, when you unlinked this computer on the page, which bots
you had ticked in "Connect my Grok bots" (their Grok ids only, until a twin is made or the ticks are cleared), and, after
"Remove Grok Bot Twin…", that everything was removed (so the last screen is still there after a look elsewhere;
it goes when you press "Set up Twins again" or twin again). On the Twins page, "Stop twinning" clears
that twin's notes, "End sample" the sample's, and "Remove Grok Bot Twin…" all of them except that last one.
`hermes twin remove` and `hermes twin uninstall` run outside Hermes Desktop and cannot reach its storage: what
they leave there is cleared by "Remove Grok Bot Twin…" on the Twins page, which the page keeps offering while
a twin's notes or the unlink time are left, also when the data folder is already empty. Two limits: the profile
your twins are looked after in is written again whenever the Twins page opens while the plugin is switched on
(not while the last screen shows); and notes that an earlier version of the plugin left for a twin that is no
longer listed cannot be found by this one (this storage cannot be listed), so they stay until Hermes Desktop's
own data is cleared.

botport, when you twin a real bot, creates or adopts that twin's Hermes profile and keeps its own records in
it (`botport.mirror.json`, `botport/`, `backups/botport/`); `hermes twin remove` ("Stop twinning") removes them
and keeps the profile.

The plugin reads two of those records, and never writes them: `botport.mirror.json` and the zip files in
`backups/botport/` (at most 60 of them, 8 MB each at most, opened without following a link). It does so only to
show the text of an earlier version of a twin's name or description under **Earlier versions** before you put
it back, and to leave out versions that are empty or the same as now. A text is shown only when its SHA-256
is the one botport names for that version. What is read stays on this computer and is not logged; a line that
looks like a password or key is hidden as everywhere else on the page.

On the computer with the Grok app the plugin itself writes these things into a twin's own Hermes profile folder
(atomic, 0600, only after every folder from the Hermes root down was checked to be a real folder of yours; a
link is never written through). What it writes outside the Hermes folder is listed under "Skill sync" and "The
background helper":

| Path | What | When |
|---|---|---|
| `SOUL.md` | the instructions you pasted on the Twins page or imported from a setup export (a line of an export that looks like a password or key is left out), in a block of the plugin's own (`<!-- grok-bot-twin:begin instructions -->` … `end`). botport's sections and every other line of the file stay byte for byte. The text is never sent to Grok and never logged. | when you press Save in "Paste its instructions", press Import on a setup export, or put an earlier version back; and, for a twin that follows Grok's instructions, when Grok's newest setup export changed them and Hermes did not (only from a file bound to the twin's own Grok bot, at most 4 times a day), or when you chose Grok's on a card |
| `cron/jobs.json` | written by Hermes' own `cron create --paused` at an import: one switched-off routine per routine of an imported setup export (its name, schedule, time zone and prompt, and a reason that begins "Imported by Grok Bot Twin from the bot's setup export"). Their job ids are noted in `pairs.json`, so "Move routines to Hermes" finds them. With Routines switched on the plugin also changes this file itself, under Hermes' own lock (`cron/.jobs.lock`, made if it is missing): it pauses a routine that moved to another computer and switches it on again when it comes back (see "Routines on your other computers") | when you press Import on a setup export that holds routines; with Routines switched on, when a routine moves |
| `memories/MEMORY.md`, `memories/USER.md` (and Hermes' own `.lock` file beside each) | the merged notes of that twin (see "A twin's memory between your computers"); with memory facts with Grok on, also the Grok bot's facts, and a short trailer at the end of each shared note: `(fact h1a2b3c4, from Hermes, 2000-01-02)` | only for a twin whose memory you switched on, or whose memory facts with Grok you switched on |
| `config.yaml` | `model`, `fallback_providers`, `fallback_model`, copied from your default Hermes profile and kept to model ids and provider names (keys, addresses and headers are dropped; `.env` is never read). Added to the end of the file; nothing else in it changes. | once, when a twin is made and its Hermes bot has no model while you have a default one |
| `SOUL.md` | the fleet note, in a block of the plugin's own (`<!-- grok-bot-twin:begin fleet -->` … `end`): four lines of fixed wording saying which computer this copy is on, on which other computers copies run, and which computer is the source. Computer names are limited to `A-Z a-z 0-9 space . _ -`; the twin's name is used only when it is plain text (else "this twin"). | when a twin is made, and when the set of your linked computers or one of their names changes; also when you never share ("No other computer holds a copy yet.") |
| `config.yaml` | `model` set to the model id you chose on the new-twins sheet or with `hermes twin finish --model` (a plain model id only; a `model` mapping keeps its other keys, such as the provider). | when you choose a model for a twin there |
| `config.yaml` | `grok-bot-twin` in `plugins.enabled`, so the plugin is on inside the twin's own Hermes bot (its chats then know they are a twin). Three lines added at the end of a file that has no `plugins` section; a file that has one is rewritten only when it reads back exactly as intended. A bot where you listed the plugin under `plugins.disabled` is left alone. | when a twin is made |
| `plugins/grok-bot-twin/` | a copy of the plugin's own installed folder (its code, page and manifest; no tests), with a marker file. Hermes loads a bot's plugins from that bot's own folder, so without the copy the line above would switch nothing on. Refreshed when the installed plugin changed. A folder of that name without the marker (your own install) is never touched. | with the line above |

"Stop twinning" and "Remove Grok Bot Twin" leave the instructions, the imported routines, the memory files and
the model settings in place: they are your Hermes bot's own. They take the other three out again (the note, the
line in `plugins.enabled`, the marked copy).

On a computer that you **linked** to the one with the Grok app there is no botport; there the plugin itself
makes each mirrored twin's Hermes profile (`profiles/<name>/`, a name made from the twin's name; an existing
Hermes bot of that name is never taken over, the twin gets `-2`; a folder of that name that holds no Hermes bot,
only what a running Hermes makes by itself (its empty working folders, logs, caches, the scheduler's heartbeat
files and a session database with no chat in it, which the plugin opens read-only to check exactly that), counts
as free and is removed first): `SOUL.md` with the twin's name and
description as they were shared (a block `<!-- grok-bot-twin:begin twin -->` … `end`) and the fleet note,
`config.yaml` with your default model on that computer (ids and provider names only) and the plugin switched
on, `profile.yaml` with the display name and description, and the marked copy of the plugin. With skills
between your computers switched on it also writes the text files of the twin's skills into that profile's
`skills/` folder (only folders that hold a skill it was sent; a skill of your own there is never touched, and
a folder it replaces is first moved to `fleet-skills-kept/`). It never writes a
`.env` or any credential. Settings that follow a twin (see "Network") are written there too: in `config.yaml`
only `toolsets`, `platform_toolsets`, `agent.disabled_toolsets`, `display.personality`, `display.compact`,
`agent.max_turns`, `agent.reasoning_effort`, `timezone`, `memory.memory_enabled`, `memory.user_profile_enabled`
and, with the model switched on on both computers, `model.default` and `model.provider`; in `SOUL.md` the
twin's instructions in a block of their own (`<!-- grok-bot-twin:begin persona -->` … `end`). The file is
rewritten only when it reads back exactly as intended, and what it held before is noted first. `mcp_servers`
and every other key are never written. When the twin is removed on the sharing computer, or the computer is unlinked, the
whole profile folder is moved (not deleted) to `fleet-kept/` in the plugin's data folder and deleted after 30
days; it is moved back when the twin returns. With the move the plugin leaves one small file of 29 bytes,
`profiles/.deleted/<name>` ("moved aside by grok-bot-twin"): that folder is where Hermes itself notes a bot it
deleted or renamed, and a bot noted there is not listed, not served and not made again by a Hermes that is still
running (before, Hermes went on listing a bot that could not be opened). The plugin writes it only for a twin's
bot it has just moved, never over a note of Hermes' own and never while a folder with anything of yours sits at
that name; it takes its own note away when the twin returns, when you press "Keep this bot", when the 30 days are
over, when a bot of yours has that name, and with "Remove Grok Bot Twin". A note of Hermes' own is never touched.
When you unlink a computer while the sharing computer cannot be reached (off, asleep, sharing switched off), the
plugin keeps what it needs to tell it later (its address, the port and this computer's own pass for that link,
in the plugin's state file) and says the goodbye again at the background check, less and less often (at most
hourly), for at most 30 days; it is deleted the moment the goodbye is heard, when the computer is linked again,
or with the plugin's data. Before, the sharing computer went on listing a computer that had left. If Hermes was open when the folder was moved it makes a folder of
that name again within seconds (logs, an empty session database, sometimes its own starting prompt file). When the
twin returns, such a folder is moved to `fleet-kept/` as well, not deleted, and goes after the same 30 days, so the
twin's bot is back under its own name. A folder with anything of yours in it (a `config.yaml`, `profile.yaml`,
`.env`, a prompt you wrote, a memory, a scheduled job, a file in its workspace, a link, a single chat) is never
moved: the twin then gets a numbered folder and is shown in Hermes as "Acme Support 2", and its row says so.
"Remove Grok Bot Twin" on any computer takes the fleet note, the plugin's line and its copy out of every twin's
Hermes bot, also on a computer that never shared. "Remove Grok Bot Twin" on a linked computer leaves the mirrored
twins in place as ordinary Hermes bots (the plugin's marks, its line and its copy are taken out).

"Remove Grok Bot Twin…" on the Twins page (`hermes twin uninstall`) stops twinning every bot, ends the sample and
deletes everything in this folder; pressed on the Twins page it also clears what the page itself remembered in
Hermes Desktop's storage (see "Not in this folder" above). Your Hermes bots (their profiles) and chats stay, and nothing in Grok changes. One exception, because it lives in this folder: `fleet-kept/`, the Hermes bots of twins that were removed or unlinked in the last 30 days, is deleted with the rest (the confirmation counts them; "Keep this bot" beforehand puts one back among your bots). Removing
the plugin with `hermes plugins remove` alone leaves this folder on disk.

### The pass (a twin's key file)

When you twin a real bot (`twin create`, for every twin), botport creates a per-twin key in its state folder (`botport-state/pairs/`, an
owner-only 0600 file). The Twins page calls this small file the twin's pass. It signs the Grok-side snapshots
botport composes on this machine (HMAC-SHA256), and, when you turn on skill sync (`hermes twin skill-sync`), it
authenticates what travels between your Hermes and the sync bot in your Grok account: you attach the pass in the
sync bot's chat (never paste what is in it; the Twins page shows the set-up folder that holds the file in your
file manager, never its content). For skill sync the plugin copies that file, byte for byte, into its set-up
folder and, for a twin added later or after `--new-pass`, into the shared folder's `inbox/keys/` until the sync
bot has taken it; it also works out the file's SHA-256 for the signed list of twins. It never shows, logs or
sends what is in it. Replace it with `hermes twin skill-sync --new-pass` (which runs `botport twin mirror --rotate-key`;
the old pass then verifies nothing, and the sync bot needs the new file); turning skill sync off with
`hermes twin skill-sync --off` also replaces it. `hermes twin remove` deletes it. It is not an account
credential: it never leaves your machine except as the file you attach yourself and, for a later twin, the
copy in the shared folder you chose (which your own sync app may carry to your own cloud account).

### Skill sync: what the plugin writes and reads

Only when you turn skill sync on. In `skill-sync/` in its data folder the plugin keeps: `state.json` (the
shared folder's path, which twins are in skill sync with each bot's id in Grok and how each joined, a counter,
and when the set-up folder was last made), `inbox.key` (an
inbox pass it makes itself: 32 random bytes, owner-only; it signs the list of twins and is not an account
credential), `texts/` (the three texts to paste) and `skill-sync-kit/`, the set-up folder you attach in the sync
bot's chat: `INSTRUCTIONS.md`, botport's `AGENT-MIRROR.md`, `skill-sync-run.sh`, `SHA256SUMS`, `pairs.txt` with
`pairs.txt.mac`, `inbox.key`, one `<twin>.key` per twin, and botport for the sync bot's box
(`botport-x86_64-unknown-linux-musl.tar.gz`, and the `aarch64` one when you have it). That archive is your own
download: the plugin looks in your Downloads folder for those exact file names, and the same names with " (1)" to
" (9)" before `.tar.gz` (what a browser calls a second download), nothing else and no folder listing; each file
of such a name is read once, whenever the set-up folder is made, to work out its SHA-256, and copied only when
that is the one in the plugin's list of release files. Files of an earlier set-up that no longer belong in the
set-up folder (a twin that left, a half-written file) are deleted from it, one by one.

In the shared folder you chose it writes `inbox/pairs.txt`, `inbox/pairs.txt.mac`, sometimes
`inbox/keys/<twin>.key` (above), and makes one folder per twin for botport. It reads `status/heartbeat.json`
there (16 kB at most, never through a link), and uses only a date, a version number, a SHA-256, twin ids, a counter, one of six
fixed words (what the script made of the list of twins), per twin, the first 16 characters of the SHA-256 of
the pass the box holds (whether the box has the current pass; it says nothing about the pass), and the folder names
of the skills in your Grok account's shared skills library (names only, up to 200, so that it can tell you which
skills made in Grok do not come back to a twin), each of a known shape. When you choose skills made in Grok for a
twin (`hermes twin skill-sync TWIN --take NAME`, `--take-all`, `--untake NAME`, `--take-none`), `state.json` keeps
those names and the signed list carries them to the sync bot as a line `# skills <twin> <names>`. It deletes its own `inbox/keys/<twin>.key` once the heartbeat says the box holds
that very pass (never through a folder that is a link; this happens when the skill sync sheet is open or
`hermes twin skill-sync --status` runs, not by itself). When skill sync is turned off for a twin or twinning
stops, it takes the twin off the list and deletes that twin's pass copies in `inbox/keys/` and in the set-up
folder. `hermes twin uninstall` (or Remove Grok Bot Twin) first leaves a signed list that names no twin and
deletes the passes still waiting in `inbox/keys/`; the list, its signature and the twins' folders stay in the
shared folder, which is yours to delete.

`skill-sync-run.sh` is a shell script for the sync bot's box in your Grok account (under 300 lines,
readable in the set-up folder). The plugin never runs it. In the box it checks the botport archive against `SHA256SUMS`
before unpacking it, checks the list of twins with the inbox pass (HMAC-SHA256 through `python3`, and never an
older list again; the list that comes with the set-up folder in your own chat message is taken as yours),
moves pass files by path without printing them (a twin whose pass you replaced is not served with the old one), runs `botport --version` (also once on the unpacked program at install, to see that it runs in the box),
`botport mirror apply-pack` (also with `--check`) and `botport mirror digest-items` (per twin, on a copy of the box's
files it makes in its own folder and replaces on each run: the bot's `profile.json` plus only the skills that twin
installed, by name, also when a bot changed them in Grok since, and the skills you chose for it; never the rest of
the shared skills library), installs a skill only with `botport mirror apply-pack --precheck ... --install-to` (botport
checks the pack against the twin's pass and installs the copy it checked), sets a twin that left the list aside
without deleting anything, marks botport's own report lines as data, and writes the heartbeat (with the names
of the skills in the shared library). What comes back is handled by botport on this computer like any change from
Grok: a skill changed only in Grok is applied in Hermes, one
changed in both places is a conflict for you to settle, and a skill made in Grok that you chose waits for your
approval (`hermes twin approve`) before Hermes has it the first time. The skills library in Grok is shared by every
bot in your account, so any of your bots can change a skill your twin sent, and that change comes back like yours.
The heartbeat also says how many skills the library holds. It makes no network calls; the sync bot copies files
between the box and the shared folder. Every run of the sync bot's routine spends Grok credit.

### Hermes → Grok through the Mirror bot (off for every twin until you allow it)

Hermes → Grok is off for every twin until you allow that twin; there is no switch for the whole plugin (the old
`hermes twin to-grok beta` only says so and changes nothing). For a twin you never allowed, nothing is sent, the
timer sends nothing, and no request for it is written to the skill sync folder's `to-grok/`. Allowing, applying,
undoing, settling a difference, moving a routine and giving up on the Mirror are refused, also on the Twins page's
own routes, inside a request from another computer, and a command a chat started cannot do them either. Stopping
(`disallow`) is always accepted: it only takes changes back.

Changes go only for a twin and the items you allow (`hermes twin to-grok TWIN allow name description instructions routines`)
and only while that item is set to "Hermes → Grok" or "Both ways". In its data folder the plugin keeps `h2g.json`:
what you allowed, the last values it knows Grok held (to find what changed and to undo), where each routine runs,
and a record of the changes (200 at most). Nothing is sent before you saw the preview and applied that very
preview. A change goes as two files in your skill sync folder: `to-grok/<change id>.json` (the bot's id and name,
the new values, and the exact message for the bot) and `to-grok/<change id>.json.mac` (its signature with the inbox
pass; the request also carries, signed, the ids of the bots you allowed). Next to them the plugin keeps
`to-grok/allowed.json` and `to-grok/allowed.json.mac`: the bots you allow right now and the ids of the requests
Hermes still waits for, with a number that only grows, signed with the same pass and rewritten each time you allow or
stop a twin, make a change, stop waiting, or an answer comes back (and emptied when you remove Grok Bot Twin). The bot's name goes into a request only when it is a plain short name; otherwise the id alone. Every text,
routine name and prompt sits in the message between its own BEGIN and END lines, as data. The Mirror bot checks
the signature with the script in its box, which refuses a request whose bot is not in both lists (the one signed
into the request and the current `allowed.json`), a missing or unsigned `allowed.json`, one older than the request
was signed with or than one the box saw before, a request Hermes no longer waits for (answered, stopped or given
up, even when a saved copy is put back), a request older than 14 days, and a request it showed already (the
Mirror's own `status/approved.json` is never trusted for this). It sends that message to that one bot only, and writes
back `status/to-grok/<change id>.json` (what was done) and `status/bots/<bot id>.json` (the bot's name and
description now); the plugin reads only those small files, as data of known shapes, and takes only the name and
description from the read-back (botport's own read on this computer wins; instructions or routines in it are
ignored). The Mirror's box keeps, in its skill sync folder (`/workspace/skill-sync`), the copies of requests it took
down (with their instruction texts and routine prompts), the list of the request ids it showed (`.work/to-grok-answered`)
and the newest allowlist number it saw. The new values reach Grok:
that is the point, so instructions you write in Hermes for such a twin are read by Grok. Never sent: a text that
looks like a password or key, an empty text, or the removal of every routine at once. Every Mirror run spends Grok
credit, and saving a new routine asks for your Allow in that bot's chat in Grok.

The Auto-Review rule the plugin shows you to add in Grok (Settings > Permissions > Auto-Review > Allow without
Asking) names the bot by its stable id (with its display name in quotes only when that name has no comma, quote,
line break or linking word such as "and") and the Mirror by the name in the `mirror_name` setting. It covers only
edits that the Mirror asks that one bot to make to its own name, description, instructions and existing routines;
saving a new routine is not covered and keeps Grok's own Allow question in that bot's chat. The plugin keeps the
exact rule text it showed you in `h2g.json`. To stop: `hermes twin to-grok TWIN disallow` (or **Stop changes in
Grok** on the Twins page). That takes the bot out of `allowed.json`, takes that twin's requests still waiting out of
the skill sync folder, and shows the exact rule to delete in Grok (Settings > Permissions > Auto-Review > Allow
without Asking). Delete it there: until you do, Grok would still let the Mirror ask that bot for such edits.

### The background helper (the one file outside the Hermes folder and your skill sync folder)

Off unless you turn it on, on a computer that follows another one (`hermes twin background on`; the Twins page
has no switch for it in this version, though the plugin's local API has a route for the same switch, `POST
fleet/background`). It keeps that computer's twins following while Hermes is closed. Turning
it on adds exactly one thing outside the Hermes folder (a file, or on Windows a scheduled task); turning it off
(`hermes twin background off`, unlinking on that computer, or Remove Grok Bot Twin) stops the helper and deletes
it. A computer that was unlinked from the sharing one keeps its helper, waiting, until you turn it off:

| System | The file | What it is |
|---|---|---|
| Linux | `$XDG_CONFIG_HOME/systemd/user/grok-bot-twin-follower.service` (`~/.config/systemd/user/…` when `XDG_CONFIG_HOME` is not set) | a systemd user unit that runs `hermes twin fleet run` with `HERMES_HOME` set, restarts it 30 s after a crash, at low priority, and does not restart it when the command is gone (exit status 2, 126, 127 or 203: the plugin or Hermes was removed without turning the helper off). It runs while you are logged in; after logout only if "lingering" is already on for your account (the plugin reads that, never changes it) |
| macOS | `~/Library/LaunchAgents/com.grok-bot-twin.follower.plist` | a launchd agent that runs the same command, restarts it after a crash (not after a clean stop), as a background-priority process. launchd cannot be told "do not restart when the command is gone", so the agent starts through `/bin/sh -c` with one fixed line: it first asks `hermes twin --help`, ends quietly when that fails (the plugin or Hermes was removed without turning the helper off) and otherwise becomes `hermes twin fleet run`. The path of `hermes` and the words are passed to that line as separate arguments, never as part of the shell text. Its output goes to `follower-agent.log` in the plugin's data folder, deleted with the agent. macOS shows its own "background item added" notice once |
| Windows | no file: one scheduled task of your own account, `grok-bot-twin-follower` | see "On Windows" below: no administrator rights, least privilege in your own session, started when you sign in |
| Anything else (also Linux without systemd user services) | none | not offered; twins follow while Hermes is open |

**After an update of the plugin.** A process that keeps running would go on with the version it started with.
The helper's loop therefore looks once a minute at the names, sizes and dates of the plugin's own installed
files (it reads none of them). When they changed and stayed the same for two looks, the loop ends with exit
status 75 and systemd or launchd starts it again with the new version (30 s later); on Windows the task's next
look does (within 15 minutes). To know that it was started by the helper and not typed into a terminal, the
loop reads its own `/proc/self/cgroup` on Linux and the `XPC_SERVICE_NAME` variable on macOS; in a terminal it
only prints that the plugin was updated and carries on. At its first check every Hermes process also brings each
twin's own marked copy of the plugin (see "Files it writes") up to the installed version; a plugin folder you
put into a twin's Hermes bot yourself carries no marker file and is never touched.

A Hermes folder other than `~/.hermes` gets its own file name with a suffix made from that folder
(`grok-bot-twin-follower-<folder>-<6 characters>.service`), so a second install never shares a helper with the
first. The file is written atomically, mode 0644, and never through a link. The helper runs as you, never as
root, opens no listening socket on a linked computer (on the computer that shares it would hold the share
port, see "Processes it starts"), and the command it starts is the `hermes` on this computer (the one the plugin
was started with), refused when anyone but you or root could change that file.

## Background work

Up to two threads per Hermes process (plus the ones under 3 to 8 while you use them), started lazily (first chat turn, session start or Twins page request, never in
`register()`) and only once there is a twin or this computer shares or is linked (none at all before that); they end
when nothing is left to do, and when the plugin unloads.

1. **The check** (`grok-bot-twin-sync`). Every `sync_interval_seconds` (default 60, minimum 5) it runs
   `botport twin check` (file metadata only: time, size, inode) and updates only the twins whose Grok side
   changed. Each part of a round stands alone: one that fails is tried again next round and never holds up the others. At most every 15 minutes the same thread also reads the newest setup
   export of each twin that follows Grok's instructions (see "Files it reads") and acts on it: Grok's instructions
   are saved in the twin only when Hermes did not change them since the two last agreed, the file is bound to the
   twin's own Grok bot, and fewer than 4 such changes were made that day (the version before is kept under Earlier
   versions); when both changed, or the file is not taken by itself, nothing is changed and the Twins page and
   `hermes twin instructions` show both for you to choose. With no shared folder and Downloads off it reads nothing.
   At most every 15 minutes it also runs one memory facts sync per twin whose memory facts with Grok you switched on,
   on a computer that is not linked or that shares its twins (a computer that follows another gets the facts with
   the twin's memory instead), and only while the Mirror is plugged in (without it, nothing).
2. **The watch** (`grok-bot-twin-watch`, `twin/watch.py`). It makes a changed name or description arrive in
   seconds instead of at the next timed check. It runs nothing itself: when the Grok app saves its bot list,
   it wakes thread 1, which then does exactly what a timed check does.
   - What it looks at: the file names in the Grok app's `sand-client-persistence` folder, and the date, size
     and file number of the bot-list file(s) there (the files whose name decodes to `…roster.last-roster`).
     Links are not followed. **No file is opened for reading**; the bot list's content is read only by
     botport, as before.
   - How it waits: on macOS (and BSD) it asks the system to wake it when that folder or file changes
     (`kqueue`, descriptors opened event-only), so it uses no processor time while nothing changes and has no
     timer of its own beyond a look every 30 s. On Linux it compares the same dates and sizes every 2 s.
     While you have twins and that folder is missing (the Grok app was moved or removed), it looks for the
     folder every 3 s instead, so the page's notice clears within seconds once Grok is back.
   - Pacing: the first change is passed on at once; if the bot list keeps being rewritten, wake-ups spread out
     to 2, 4, 8 ... at most 30 s apart.
   - When it is off: with no real twin (the sample alone never makes the plugin look at the Grok app), when
     the Grok app's folder is missing, and with the setting `watch_grok_changes: false`.

3. **Sharing** (`grok-bot-twin-share`, `twin/fleet_server.py`), only on the computer that shares and only
   while "Share my twins with my other computers" is on. One thread waits for connections: it sleeps in the
   operating system until one arrives (no timer). Each request is served by a short-lived thread; a linked
   computer's request sleeps there until the list changes or 50 seconds passed. At most 32 at a time. Only one
   Hermes process per computer holds the port; another one leaves it to that one and takes over when it ends.
   The list is published the moment the plugin records a new, renamed or removed twin, and checked again with
   every timed check (thread 1) as a safety net.
4. **Following** (`grok-bot-twin-follow`, `twin/fleet_follow.py`), only on a computer that is linked. One
   thread keeps one request open with the sharing computer and applies what comes back. When that computer
   cannot be reached it tries again after 5 s, then 10 and from then on every 15 s (one refused connection
   each time); nothing else happens meanwhile.

5. **Requests** (`grok-bot-twin-requests`, `twin/tasks_run.py`), only on a computer that is linked or shares,
   and only once you touched the switch "Let my other computers ask this one to do things" there. While the
   switch is on, one thread keeps one request open with the sharing computer (the sharing computer's own thread
   waits inside its process) and runs what arrives, one request at a time. While the switch is off it looks at
   the switch file every 5 seconds and does nothing else; a computer that never used the switch runs no such
   thread. While a request's chat turn runs, a second short-lived thread asks the sharing computer every 20
   seconds whether the request was withdrawn.
6. **Routines** (`grok-bot-twin-routines`, `twin/routines.py`), only on a computer that shares or is linked,
   and only once Routines was switched on (or heard of) there. Every 30 seconds it looks at the date and size
   of its twins' Hermes job files and reads them only when one changed: on the computer that shares, to notice
   a new or changed routine; on a linked computer, to pause a copy that was resumed by hand. No network call
   of its own.

7. **Twin memory** (`grok-bot-twin-memory`, `twin/xs_memory.py`), only on a linked computer and only while
   memory is switched on for at least one twin there. One thread looks at the dates of those twins' two memory
   files every 3 seconds and, when one changed or 15 seconds passed, makes one short request per such twin to
   the sharing computer. Only one Hermes process per computer does this (a lock file). With every switch off
   the thread is not started; once started and everything is switched off again it reads one small file every
   30 seconds and does nothing else. The sharing computer runs no thread for memory: it answers.
8. **Settings watch** (`grok-bot-twin-settings-watch`, `twin/xs_config.py`), only on the computer that shares,
   and only for 3 minutes after a linked computer that takes settings last looked in. Every 3 seconds it looks
   at the date and size of each twin's `config.yaml` and `SOUL.md` and reads them only when one changed. No
   network call of its own.

The per-turn `on_session_end` hook writes only a debounced timestamp marker.

Outside the Hermes process there is at most one more: the background helper above, only after you turned it on.
It is the same follow loop in a process of its own (`hermes twin fleet run`), near-zero processor time while
nothing changes (a look at four file dates every 5 s and one waiting request to the computer it follows).

## Network

Unless you use "Share my twins with my other computers" (below), the plugin makes no network calls and uploads
nothing itself, with one exception: the "finish several twins" sheet sends `GET <base_url>/models` to each model
server already set up in the default Hermes profile's `config.yaml` (at most 6, 2 s timeout, no key or
Authorization header sent) to list the models it serves. The plugin never downloads a model. botport makes none for twins: skill sync works through a folder that your
own sync app keeps on both sides (or that your sync bot reaches on this computer). "Download it" in the skill
sync sheet hands the address of botport's release file for the sync bot's box to your browser. `gh attestation verify` talks to GitHub only when that check runs (see
"Processes it starts"); verifying botport against the plugin's own list needs no network. "Download botport" hands the address of botport's
release file or page to your browser; the Twins page itself only talks to the plugin's own local API (status every 30 s, events every
5 s, the setup checklist every 3 s while it is open; once this computer shares or is linked, the list of
computers every 5 s, or 2 s while a link code shows, and requests every 10 s, or 2 s while one is open).

### Sharing twins with your other computers (off until you switch it on)

- **Listening.** The computer that shares listens on TCP port 18777 (setting `share_port`) on exactly one
  address: its Tailscale address (100.64.0.0/10) when Tailscale is running, else its private home-network
  address (10/8, 172.16/12, 192.168/16); `share_address` can name another of its own private addresses
  (127.0.0.1 for a tunnel). The address it first shared on is kept and tried first for as long as it works
  (your linked computers know it), also after sharing was switched off and on again: a computer that began on
  the home network stays there when Tailscale is started later, until you name the Tailscale address in
  `share_address` and link again. It never listens on every interface or on a public address, and it drops a
  connection from any address outside those ranges before reading it. To a caller it does not know it answers
  one thing, `/v1/hello`: the plugin's name, the version of the exchange and whether sharing is on (that is how
  a computer about to link finds it); everything else needs the link code or a linked computer's signature. One
  more route, `/v1/poke`, is for other Hermes processes on this same computer (accepted only from this
  computer's own address, with a random word made for that run of the sharing process): it tells the sharing
  process that the list changed, or asks whether it is there, and carries nothing else. Standard library
  `http.server`; no framework, no dependency. To find this computer's own address the plugin asks the system
  which interface would carry traffic to four fixed addresses (100.100.100.100, 192.0.2.1, 10.255.255.255 and
  192.168.255.255; a UDP socket that is "connected" and closed again, which sends nothing).
- **Who may ask.** A computer becomes "linked" by proving it knows the link code shown on the Twins page: 12
  characters, valid 10 minutes, for one computer, void after 5 tries. (One exception to "used once": the computer
  that used a code may prove the same code once more while its 10 minutes last, no newer code was made and that
  computer has not synced yet. That is a link whose last answer was lost on the way; before, pressing Try again
  said "already used". The 5 tries are counted over both, and every other computer still gets "already used".) Neither the code nor anything it could be
  worked out from is sent. The two computers run the SPEKE exchange (ISO/IEC 11770-4) in the standard 2048-bit
  group 14 of RFC 3526, computed with Python's own whole numbers: each turns the code into the same starting
  number (through scrypt, 16 MB), sends one value, and both arrive at a secret nobody else can; then each
  proves to the other, with a keyed digest (HMAC-SHA256) over everything both sent, that it arrived there.
  Whoever does not know the code, at either end or in between, gets one guess per attempt, and the sharing
  computer counts the attempts. The pass is made from that secret; the linked computer keeps the pass,
  the sharing computer keeps only a key made from it. Every later request is signed with that key and counts
  once (a recording of it sent again is refused; see the next point), and so is
  every answer, bound to the request it answers. A linked computer acts only on a signed answer: another device
  at the sharing computer's address cannot read the list, cannot tell it "you are unlinked" and cannot hand it
  a list. You can remove a linked computer at any time; its key is then worth nothing (it is remembered for 90
  days only to sign the one answer that tells that computer so).
- **A recorded call is worth nothing later.** Each signed call carries a random number used only once, which
  the sharing computer remembers while its Hermes runs. A call for the twin list (and "unlink me") also carries
  a call number that only rises, inside the signed text; the sharing computer keeps the highest one it has had
  from each linked computer in `fleet.json` and refuses anything at or below it, also after Hermes was started
  again and however old the recording is. These calls carry no time on purpose: a linked computer whose clock is
  wrong must still be able to follow. (A request to do something carries a time as well; see further on.)
- **What the sharing computer sends** (the complete list; built field by field, tested in
  `tests/test_fleet_privacy.py`): per real twin an id made for this purpose (not the Grok id), name,
  description (both after the plugin's password-and-key check), a version number and a date; the ids of removed
  twins with a date; its own name, id and kind of computer; the name, id and kind of each linked computer. The
  sample twin is never shared. No chats, Hermes bot names, file paths or anything from botport's state. A
  twin's instructions, its tool set and connector names and its plain preferences are not in this list either:
  they travel on a route of their own, each item on unless you switch it off, and the model's name only when
  you switch it on (see "Settings that follow a twin"). No skills while the skills switch is off:
  with it on, the text files of each twin's skills are sent too (see "Skills between your computers"). No
  memory in this list either: a twin's memory travels only once you switch it on, on a route of its own (see
  "A twin's memory between your computers"). (Routines travel only when you switch Routines
  on, and then beside this list, not in it: see "Routines on your other computers" below.)
- **What a linked computer sends**: once, its value of the exchange and its proof of knowing the link code
  (never the code) with its own id (random), name and kind; afterwards its
  name and kind, how long it will wait for an answer, the list version it holds and when it applied it, per
  twin id the version number it made or one of two fixed codes
  ("could not make it", "could not save it"), and a call number that only rises (the time by its own clock, in
  milliseconds), which is what makes a recorded call worthless later. Also in that call: which skills list and
  which settings it was last told (`"xs": {"skills": …, "config": …}`, two short version marks), and its
  routines report (see "Routines on your other computers"; with Routines off it holds the two switches and
  empty lists).
- **Outgoing only on linked computers.** A linked computer connects to the one address its link code carried
  (or one you typed or picked), and only if that is a Tailscale, private home-network or loopback address. A
  name you typed or picked is turned into an address with the system's own name lookup.
- **Picking a computer Hermes Desktop already knows.** Only after a link code could not reach the sharing
  computer, the link sheet on the Twins page reads Hermes Desktop's own list of connections (each one's name
  and address; never a pass or a key) and offers them by name. The address of the one you pick goes to this
  plugin's own local backend as the place to try the same code, and nowhere else; nothing of that list is
  stored.
- **Signed, not encrypted, by the plugin.** It is plain HTTP. On Tailscale, Tailscale encrypts it between your
  computers. The plugin tells Tailscale by the address alone: anything in 100.64.0.0/10 counts. On a network
  that hands out such addresses without Tailscale (some providers' own networks do), the page would say
  "Tailscale (encrypted)" for a link that is not. On a plain home network someone on that network could read the twins' names and
  descriptions, their settings and instructions (unless you switched those items off), their skills and routines
  if you switched those on, and requests and their answers (not change them). A twin's memory is the exception:
  the plugin itself encrypts it (see "A twin's memory between your computers"). A recording of the linking gives nothing away, neither the key nor the code: the code has only
  24 random bits, but nothing that travels can be checked against a guessed code, and the key needs the secret
  that never travelled. What is left to someone on that network is guessing the code while it is valid: five
  attempts in 16 million, after which the code is void and you make a new one (someone who keeps doing that can
  stop you from linking, not get in). A computer that did get linked shows up in your list of linked computers,
  where you can remove it. Python's standard library has no authenticated encryption, and the plugin does not
  bring a cryptography package: the exchange is a published standard written with the library's HMAC-SHA256,
  SHAKE-256, scrypt and whole-number arithmetic (twin/fleet.py, "keys and signatures").
- **Limits.** A whole request must arrive within 20 seconds, is at most 16 KiB, and at most 64 connections are
  held (8 from one address, 32 being served).
- **What is done with received text.** Everything a computer receives is type-checked, cleaned and capped
  again before it is stored, shown or written (names 100 characters, descriptions 2,000, at most 500 twins);
  unknown fields are dropped; errors travel as codes and are put into the receiving computer's own words.

#### Settings that follow a twin (on with sharing; one switch per item on each computer)

Over the same link, signed and counted like every other call (`POST /v1/xs/config`), a linked computer asks the
sharing computer for its twins' settings. The share protocol's version is unchanged: a linked computer says in
its ordinary look which settings it last heard of, and only then is it told anything about them, so a computer
on an older build is sent exactly what it was sent before.

What is read from a twin's own Hermes bot on the sharing computer is a list of names, never the settings file
"minus secrets":

| Item | Read and sent | Starts |
|---|---|---|
| Tools | `toolsets`, `platform_toolsets`, `agent.disabled_toolsets` (names), and the NAMES of the keys under `mcp_servers` (a connector's command, address, headers and environment are never read into what is sent) | on |
| Preferences | `display.personality`, `display.compact`, `agent.max_turns`, `agent.reasoning_effort`, `timezone`, `memory.memory_enabled`, `memory.user_profile_enabled`; each must be a plain word, a number or true/false | on |
| Persona | the instructions in `SOUL.md` (botport's `persona` section, the block you pasted on the Twins page, and text written by hand), with control characters dropped and password-like text replaced by "[not shown: looks like a password or key]"; a line that holds an address with a name and password in it, an `Authorization` or `Bearer` value, or a short `password: …` / `token = …` is replaced whole. At most 64,000 characters. This is a check on free text, not a proof: it can miss things | on |
| Model | `model.default` and `model.provider` as plain names | off: each computer keeps its own model |

Never read for sending by this route, whatever its switches say: `.env`, `auth.json`, `providers`, `custom_providers`, any
key, token, header or address, `approvals`, `security`, `terminal`, `hooks`, `plugins`, memory, skills,
routines, chats. The linked computer cleans what arrives by the same list before it writes anything, takes
only the items its own switches allow, and never writes a connector's set-up: a connector, tool set or provider
it lacks is shown as "Set up on this computer". Of the model only `model.default` and `model.provider` are
written, and not at all when that twin's model on the linked computer has an address or key of its own and the
provider differs. A settings file on the sharing computer that cannot be read is never taken for "no settings":
the linked computers keep what they have. The persona is instructions: a linked computer's twin acts on
it. It is accepted only in an answer signed by the computer you linked, cannot open or close a block of the
prompt file, and stays readable on a plain home network like everything else that is shared.

### A twin's memory between your computers (off for every twin until you switch it on, on both computers)

- **What it is.** The notes in a twin's two Hermes memory files, `memories/MEMORY.md` and `memories/USER.md`
  in its Hermes bot. Nothing of them is read, sent or written by the plugin until memory is switched on for
  that twin on the sharing computer AND on a linked computer; then only between those.
- **Where.** One more route on the sharing computer's same port, `POST /v1/xs/memory`, signed and counted like
  every other call of a linked computer (a recording sent again is refused; a computer that was removed is
  told so and gets nothing). An older Grok Bot Twin does not know the route and is not affected; a linked
  computer that meets an older sharing computer says "needs a newer Grok Bot Twin" and carries on.
- **Encrypted from end to end.** The request and the answer each carry one sealed box. Keys: two, made with
  HMAC-SHA256 from the link's key (the one the two computers worked out when they were linked and never sent).
  The text is padded to a multiple of 1,024 bytes, encrypted with SHAKE-256 as a keyed stream under a fresh
  24-byte random value, and authenticated with HMAC-SHA256 over the direction, the linked computer's id, the
  question an answer belongs to, the random value and the encrypted bytes; the check comes before anything is
  decrypted. Python's standard library has no ready-made authenticated cipher and the plugin installs
  nothing, so this is built from those two standard parts. Readable to someone on the network: that a memory
  call was made, between which two addresses, when, and its size rounded up. Not readable: which twin, any
  note, how many notes, whether anything changed. The plugin's signing keys and this encryption both rest on
  the link's key in `fleet.json` (owner-only); whoever can read that file on one of your computers can read
  what that computer exchanges.
- **The sharing computer is the meeting point.** It is one of your computers and holds the merged notes in
  that twin's own memory files, in the clear, as Hermes keeps memory on every computer. A linked computer sends
  what it added (text) and removed (digests) since the two last agreed and receives the whole list. A note
  added anywhere is kept, a note removed anywhere is removed, a note changed in two places is kept in both
  versions and shown as a conflict. A removal is believed only from a computer that has agreed with the
  sharing computer since memory was last switched on there.
- **What never travels.** A note that looks like it holds a password, key, token, door or recovery code or card
  number (the plugin's password-and-key check plus stricter rules for memory), a note that reads like an
  instruction to the model, a note that looks like one of the plugin's or Hermes' own marked notes, a note with
  control or invisible characters, a note over 4,000 characters. Checked on the computer that would send it
  and again on the computer that receives it; such a note stays where it is and is counted on the page, never
  quoted. The check can miss things.
- **Which computers.** A twin's notes leave the sharing computer only while its switch is on there, and then
  only for the linked computers you let take part there, per twin: the ones linked when you first switched the
  twin on, each of which you can keep out, and none that was linked later or linked anew until you let it in.
  Any other computer is answered exactly as if the switch were off. The switch on a linked computer is that
  computer's own: it decides whether that computer asks. So "off on the Surface" keeps memory off the Surface
  as long as the Surface runs this plugin unchanged; "kept out on the Mac" holds whatever the Surface runs.
- **What a linked computer of yours can do to memory.** Add notes that pass the check (up to the limit Hermes
  sets for the file; beyond it they are turned away and the sender is told), and remove notes that travel: in
  24 hours at most five of a file or half of it, whichever is more; a larger removal is not believed and the
  notes go back to that computer. The sharing computer takes 24 rounds of changes a day from one linked
  computer; further ones wait there. It cannot remove, or learn anything about, a note the sharing computer holds
  back. A linked computer that was taken over could still remove notes a few at a time or add misleading ones
  that read like ordinary notes; every computer keeps earlier versions to put back (the newest 30, more than
  one computer's rounds of a day, and how each day began, which no flood of changes can push out), and Hermes itself checks memory for instruction-like text
  again when it loads it. A linked computer leaves out arriving notes that would take a file over its own limit.
- **Not hidden from someone who later gets a computer's files.** The keys come from the link's key, which
  stays the same for as long as the link does: whoever recorded the traffic and later reads `fleet.json` on
  one of the two computers can decrypt what was recorded. And a listener can tell that memory is switched on
  (a call every 15 seconds per twin that takes part) and roughly how large a twin's list is.
- **Files.** Reads and writes those two memory files (atomic, owner-only, never through a link, holding
  Hermes' own `.lock` file beside each so the two do not write at once; a file Hermes changed in between is
  left alone for that round). Its own notes: `xs-memory.json` and `xs-memory-points/` above.
- **Nothing of a note** is logged, put in an event, an error or a terminal line; events carry a count. The
  local routes `GET /fleet/memory` and `POST /fleet/memory`, `/forget`, `/restore`, `/resolve` answer the
  Twins page on this computer only; the text of a note appears there in one case, the versions of a conflict.

### Skills between your computers (off until you switch it on)

- **The switch.** "Share my twins' skills with my other computers" on the computer that shares (terminal:
  `hermes twin fleet skills on`). Off, which is how it starts and how an update leaves it, nothing of a skill
  is read for sharing and nothing of a skill is sent. Each skill has its own switch as well.
- **What is read, on the sharing computer.** Each real twin's own `skills/` folder in its Hermes profile:
  folders that hold a `SKILL.md`, directly or one folder down. Names, sizes and dates at each background check;
  the text only when one of those changed. Links are never followed. At most 200 skills a twin, 40 files and
  600 KB a skill, 200 KB a file.
- **What is sent.** The text files of each shared skill, to linked computers only, over the link described
  above: the same listening address, the same per-computer signature on every request and answer, each request
  counted once. One more route, `/v1/xs/skills`, and one more key in the ordinary request for the list:
  `"xs": {"skills": "<the list version this computer was last told>"}`, sent by a linked computer unless
  skills are switched off there. Before a file leaves, anything in it that looks like a password
  or key is replaced by the words "[left out: looks like a password or key]": the plugin's existing check
  for keys of known makes and long random values, and for skills also any value after a name that says what
  it is (`password`, `secret`, `token`, `api_key` ... in a settings line, a JSON object or a shell export), a
  pass inside an address and a bearer value. It can still miss things, and it can hide a harmless word that
  follows such a name; the file on the sharing computer is not changed. Never sent: a file that is not
  UTF-8 text, a file or folder whose name starts with a dot (`.env`), a file named like a key or credential
  file (`token.pem`, `id_rsa`, `credentials.json`, anything ending in `.pem`, `.key`, `.p12`, `.env`), a file
  over the size limit. The page says how many files of a skill were left out.
- **What comes back.** From a linked computer: which skills it wants the text of (twin id, skill name,
  version), and for the skills that differ there the twin's id and the skill's name under one of three words
  (changed there, changed in both places, only on that computer; 40 at most), plus two counts: how many
  skills it holds and how many it could not write. Never a skill's text: a change made to a skill on a linked
  computer is not sent anywhere.
- **What a linked computer does with it.** Every path is checked again on arrival (plain names only, no
  `..`, no absolute path, no Windows device name, nothing through a link), text is cleaned and capped again,
  and the files are written into that twin's `skills/` folder there. A skill there that was changed on that
  computer is not overwritten: the page shows "changed here and on" the sharing computer and waits for your
  choice. A skill switched to "only on this computer" is left alone. Whatever is replaced or taken away is
  first moved to `fleet-skills-kept/` (see "Files it writes") and can be put back.
- **Not encrypted by the plugin.** As for names and descriptions: encrypted by Tailscale on Tailscale,
  readable (not changeable) on a plain home network. A skill is instructions your twin follows, so think of a
  home network you do not trust before you switch this on there.
- **Older versions of the plugin.** The version of the exchange is unchanged (3). A linked computer on a build
  without this feature never names skills in its requests and is sent none: it gets exactly the answers it
  always got. A new build linked to an older sharing computer says that the other computer has an older Grok
  Bot Twin that does not send skills yet, and keeps following names and descriptions.
- **Trust.** Skills arrive only from the computer you linked with a link code, signed with the key made at
  linking. A sharing computer that was taken over could send a skill of its choosing to your linked computers;
  that is the same trust the link already carries for a twin's name and description, with more at stake,
  which is why this is its own switch, off by default, and why a linked computer can refuse with "Take skills
  from" switched off.

### Routines on your other computers (off until you switch it on)

- **No new socket, no new route, no new secret.** Routines ride inside the signed call for the twin list
  described above, as one optional part of the call and of the answer "nothing changed" (`twin/routines.py`).
  A computer that does not send that part is answered exactly as before; the twin list itself carries
  nothing new.
- **Off until you switch it on** on the computer that shares ("Routines"). While it is off nothing of a
  routine's content is read or sent (a linked computer's call still carries its two routines switches and
  empty lists).
- **What the sharing computer sends per routine** (the complete list; built field by field, tested in
  `tests/test_routines_sync.py`): an id made for this purpose (not Hermes' own id of the job), the twin's id,
  the routine's name, prompt and schedule, the names of the skills it uses, a time zone if the routine names
  one, whether you paused it, a version number and a date, and which computer runs it. Name and prompt go
  through the plugin's password-and-key check. Routines that run a script, need a folder or a program of
  that computer, or run once, are not sent; neither are outputs, run times, delivery targets, chat ids, model
  or address settings.
- **What a linked computer sends**: whether it keeps copies, whether you let routines run on it, which list
  of routines it has applied, the ids of the routines switched on there and of the ones you paused there, and,
  when you pressed "Run here" or "Run on …" there, which routine you want where (on that computer itself or
  back on the sharing one; nowhere else, and a few times a minute at most).
- **What the sharing computer writes**: in its own twins' job files it pauses a routine that you moved to
  another computer and switches it on again when it comes back, under the same lock. Nothing else in the job
  changes.
- **What a linked computer writes**: paused jobs in its twins' own Hermes job files
  (`profiles/<bot>/cron/jobs.json`), changed under Hermes' own lock for that file. It switches a job on only
  when your switch "Routines may run on this computer" is on there (off by default), a signed list names that
  computer for the routine, and that list says the computer that ran it before has stopped it. Everything
  received is type-checked, cleaned and capped again (prompt 8,000 characters, 200 routines); a routine that
  is merely missing from a list is paused and kept, never removed. A copy is taken out only when the sharing
  computer says its routine was removed there (the job as it was is first kept for 30 days in `routines.json`,
  under "Routines as they were before") or when copies were switched off.
- **One computer runs a routine.** The sharing computer hands a routine over only after the computer that
  ran it reported, in a signed call, that the job is paused there. A linked computer reads that from its job
  file at the moment it reports. Limits, said plainly: a job you resume by hand in Hermes on a computer that
  does not run it is paused again at the plugin's next look (half a minute); a run that had already started
  when a routine was moved finishes; a computer removed from the sharing computer's list while it is off can
  be overridden only by your own second step ("Start it here anyway").
- **A routine that runs on a linked computer runs with that computer's Hermes**: its model, its tools, its
  approval settings. What the sharing computer sends can make a linked computer hold paused copies; it can
  make it run them only where you switched that on.

### Asking a twin on another of your computers (off on every computer until you switch it on there)

- **No new listening socket, no new secret.** Requests use the link above: four more routes on the sharing
  computer's one address (`/v1/task/ask`, `wait`, `poll`, `update`). None of them answers a step: that is
  done on the asked computer only. Each call is signed with the linked
  computer's own key, exactly like the calls for the twin list (the key itself never travels), must carry a
  time within 15 minutes of the sharing computer's clock and a number used only once, so a recorded call cannot
  be sent again; each answer is signed back, so only the real sharing computer can hand a computer a request or
  an answer. The computer that is asked fetches its requests with an outgoing request, as it fetches the twin
  list. A request from one linked computer to another passes through the sharing computer, which must be on.
- **On a plain home network: signed, not hidden.** Requests and answers travel like the twin list. Nobody on
  that network can make, change or repeat a request (linking gives a watcher nothing to work a key out from,
  every call is signed and counts once), but what is asked and what comes back can be read there. The Twins
  page says so under the switch while your computers are linked that way. On Tailscale it is encrypted.
- **The switch.** "Let my other computers ask this one to do things" is one switch per computer, off by
  default, kept in a file on that computer; no other computer can change it. While it is off the computer
  fetches nothing, and a request that still reached it is refused before anything runs. Switching it off stops
  a request that is running: the chat turn's process and, on macOS and Linux, everything it started. On
  Windows the turn is ended with `taskkill.exe /PID <its process id> /T /F` from the Windows system folder; a
  program the turn had started in a way Windows does not count as its child ran on to its own end in our test
  (two minutes).
- **What a request can do.** It becomes one ordinary chat turn of the twin's own Hermes bot on the asked
  computer, with that computer's tools, model and approval rules. The plugin changes nothing about approval
  (see "Processes it starts"). Whether a step needs approval is decided by that computer's Hermes, by its own
  settings. With Hermes' default (`approvals.mode: smart`) Hermes first lets the model judge a flagged step and
  asks you only when that judge is unsure; set `approvals.mode: manual` in a twin's Hermes bot on a computer to
  be asked about every flagged step there. If you set `approvals.mode: off` (or, for the single-question mode,
  `approvals.single_query_mode: approve`) on a computer yourself, requests from your other computers run with
  that too: the switch is as strong as that computer's own Hermes rules.
- **A step that needs approval waits for you, on the computer where it would run.** The asking chat says
  "waiting for approval on Surface". On Surface the Twins page (and `hermes twin requests`) shows the request
  and the step exactly as Hermes describes it, with two answers: do it once, or refuse. By default only that
  computer can answer: the question itself never travels, an answer sent from elsewhere is refused, and "for
  this session" or "always" are never offered for a request from another computer. If nobody answers, Hermes
  withdraws the question after its own time limit (300 seconds by default) and the step is not done; the
  answer then says "a step was not approved there". When the process that works on requests is not Hermes
  itself, or neither way into Hermes' interface starts (this computer's Hermes log then has one line saying
  why), there is no way to ask, and such a step is simply not done. While one request waits for you, the next
  one for this computer waits its turn. Each question carries a mark of its own
  and an answer that names the mark counts for that question only (the terminal's command includes it), so an
  answer cannot land on a later step you have not seen. A step longer than the 400 characters that can be shown
  is refused without asking: you are never asked to approve a command whose end you cannot read. The chat turn
  of the request itself is refused when it
  tries to answer through the plugin (`hermes twin requests approve` from inside that turn). The honest limit:
  that turn runs with this computer's tools, and what your Hermes lets a chat do without asking, a request can
  do too; the plugin's question is as strong as your Hermes approval settings, not a sandbox.
- **Two choices for a step that needs approval, made on the computer that runs the request.** The choice
  is kept in that computer's own `tasks.json` and set only by its own Twins page or `hermes twin requests
  mode`: no route of the share server carries it, and the chat turn of a request is refused when it tries.
  "Ask on this computer" (the default) is the rule above. (Until 0.3.4 a third choice let the computer that
  asked answer the step; it was removed, and a stored "linked" now reads as "Ask on this computer".)
  "Only safe requests": every question Hermes asks about a step of a request is answered "deny" at once;
  nothing waits and the asker is told why; a step that already waits when you choose this is refused within a
  second. What Hermes does not ask about still runs, so this is as strict as
  that computer's Hermes rules for asking (with `approvals.mode: smart` its model may let a flagged step
  through without a question; `manual` asks every time).
- **Who may ask.** Only a computer linked with a link code (or the sharing computer itself). The sender is
  whoever signed the call, never what the request says. The asked computer checks again by itself: its switch, that the
  sender is on its own list of your computers, the size, its own rate limit, and that the twin exists there.
  A request is sent only by you, on the asking computer itself: `/twin ask` typed in a chat that Hermes names
  as one of its local surfaces (its terminal, terminal UI or Desktop; a messaging app, a webhook, the API
  server, or a caller that names no surface is refused: the check fails closed), `hermes twin ask` in a
  terminal no chat started (a command that carries a chat's number or origin is refused), or the Twins page.
  There is no model tool for it. The chat turn of a request that itself came from another computer is refused
  too, so a request is never passed on to a third.
  The plugin's local API has the same door for the Twins page (`POST /tasks/ask`, behind Hermes' own sign-in).
  `/twin requests`, which shows the switch and this computer's request log (who asked, and the first words of
  each request and answer), is answered in a chat on this computer only, like `/twin ask`. The terminal
  command `hermes twin requests` (show, the switch, answering a waiting step) is not refused when a messaging
  chat of this computer's own Hermes runs it through Hermes' terminal tool, under your Hermes approval
  rules: whoever may write to your Hermes there acts as you on this computer, as for every other command, and
  none of it reaches another computer.
  A computer you remove has nothing left in flight: its open requests end at once and no answer is handed to it.
- **Limits.** A request is at most 2,000 characters, an answer 6,000 (and 12,000 bytes on the wire); 6 requests
  a minute and 40 an hour per asking computer, counted on the asking computer, on the sharing computer and on
  the asked computer; one request at a time per computer, at most 4 waiting; a chat turn is stopped after 180
  seconds.
- **What travels.** To the asked computer: the text you typed (password-like text masked before it leaves),
  the twin's internal id, and the asking computer's id and name. Back: only the twin's final answer (control
  characters dropped, password-like text masked, capped), how it ended and how long it took. No transcript, no
  tool output, no file, no session id. The sharing computer keeps text passing through in memory only (at most
  15 minutes) and writes none of it to disk. On a plain home network this travels as plain HTTP, like the twin
  list: signed, so nobody on that network can forge or repeat a request or an answer, but readable there. On
  Tailscale it is encrypted by Tailscale.
- **The log.** Every request, done or refused, is in `tasks-log.json` on the computer that asked and on the
  computer that was asked, and the Twins page and `hermes twin requests` show it. A request that is refused
  because the asked computer's switch is off never travels there: the sharing computer only tells it, with its
  next look at the twin list, which computer asked and when, and its log shows that line without the request.

## What reaches the model

- With memory switched on for a twin (see "A twin's memory between your computers"): the notes that arrived
  from your other computers are in that twin's Hermes memory files, and Hermes puts a bot's memory into its
  chats as it always does. They were written by that twin on one of your own computers, pass the plugin's
  check for instruction-like text before they are written, and Hermes checks them again when it loads them.

- `pre_llm_call`, only in a twin's Hermes profile and only if "Let chats know this is a twin" (`inject_context`)
  is on: at most 10 lines of fixed wording, "your Grok twin", an opaque twin ref (`t-` plus 16 hex characters),
  the state label and counts. No bot name, description, skill, routine or any other free text from Grok.
  The sample twin sits on a reserved label, not in a profile, so it adds no note to any chat; only if you name
  a profile for it (`demo_profile`, or `hermes twin demo --hermes-profile NAME`) does that profile get the
  note, with the fixed line "This is a sample twin with invented data."
- `twin_status` / `twin_recent` tool results: allowlisted fields in plain language, labelled as data. By
  default the same fixed label "your Grok twin" and opaque twin id (`t-` plus 16 hex characters), with counts
  instead of names ("Turn off 2 routines in Grok when you move to Hermes (names on the Twins page)"): no bot,
  profile, routine, skill or connection names. Only with "Let the model see names" (`share_names_with_model`) on
  do they carry names, and then only names of 1-40 characters of `A-Z a-z 0-9 space . _ -` (else fixed words
  such as "one skill"). Engine free text (state reasons, to-do sentences) is never passed through. For the
  sample twin the result carries the fixed line "This is the built-in sample twin with invented data."
  Every twin's result also names, in fixed words, the two things that are never copied from Grok ("Grok
  memory", "Grok model setting"), so the model can say so when asked.

- The setup skill (`skills/set-up-grok-bot-twin/SKILL.md`, registered as `grok-bot-twin:set-up-grok-bot-twin`):
  a text file that ships with the plugin. While the plugin is on, Hermes lists its name and one line about it
  to the model, and hands over its text when you or the model ask for it. It holds the setup steps and the
  rules for them (no password, key or pass in a chat; nothing done in the Grok app); nothing of yours is in it.
  When a model follows it, it runs `hermes twin setup` with Hermes' own terminal tool, under this computer's
  approval rules. That command counts your twins and prints no bot name the caller did not type, and never
  prints a link code back; `--check` and `--plan` change nothing and do not run botport. A link code that a
  run with `--share` prints does reach the chat, and so the model's provider: it links one computer, lasts 10
  minutes and is of use only on your own private network.
- `twin_transcripts_search` (only answers inside a twin's own Hermes bot, the one the chat itself belongs to even when one Hermes process serves every bot, and only after you switched full transcripts on for that twin; a chat that arrives through a messaging app, a webhook or the API server is refused, and so is a chat whose origin Hermes does not name or the plugin cannot read (it fails closed), and so is the chat turn of a request from another computer; `hermes twin transcripts` there answers only `status`, never a search): the words the model looks for, and back at most 20 excerpts (240 characters each) of that twin's own kept conversations, with side, who spoke, when and a title, anything shaped like a password or key masked and any line that looks like it states a password, PIN, code or card number, or reads like an instruction to a bot, held back whole, labelled as quotes rather than instructions. Those excerpts reach the twin's model provider, like any message in its chat. Another twin, or a Hermes bot that is not a twin, gets no excerpt. What the Hermes twin said is never given to the Grok side unless you also switch "send to Grok" on for that twin (`hermes twin transcripts to-grok-on`, separate from the archive's switch, off by default and off again whenever the archive goes off); then only conversations held in a chat on this computer (Hermes' terminal, terminal window or Desktop, by the source Hermes recorded for the chat; never one that came through a messaging app, a webhook or the API server, and never one whose source is missing or unknown) are offered, and only once the Mirror is connected, with passwords and keys masked and the same lines held back before it leaves. Stop twinning deletes the twin's kept transcripts.
- In a twin's chat on a computer that is linked or shares (and only if `inject_context` is on): three more
  fixed lines with this computer's name, the names of your other computers (plain characters only) and the
  name of that tool. Nothing else.
- A twin's prompt file (`SOUL.md`), which Hermes reads into that bot's chats, gains the fleet note: four lines
  of fixed wording with the names of your own computers (plain characters only) and the twin's name when it is
  plain text. On a linked computer the same file holds the twin's name and description as shared from your own
  Grok bot (they are the bot's persona there, as botport writes them on the first computer); they pass the
  password-and-key check and cannot open or close the plugin's blocks. The description stands under a fixed
  line that says it is a description and not instructions, every line of it quoted, so no text in it can start
  a section of its own or pass for the fleet note; a computer's name in the fleet note cannot carry sentences.
  With "Persona" switched on (it is, unless you switch it off), the same file on a linked computer also holds
  the twin's instructions from the sharing computer, in a block of their own under a fixed line; unlike the
  description they ARE instructions, and the twin there acts on them.

- An imported setup export: its instructions become part of the twin's prompt file, exactly as pasted ones do,
  and so reach that twin's chats at once. A routine's prompt reaches the model only when that routine runs,
  which it does not until you turn it on. Both are what the bot wrote into the file; the sheet shows them whole
  (behind "Show the text"; `hermes twin import --show` prints them) before you press Import, and nothing is
  imported that is not shown: what is too long to show is not copied. Connector names are shown on the page,
  printed by `hermes twin import` and kept in `setup-export.json`; they do not reach the model. The file's two
  lists of memories and of facts about you reach nothing: not the model, not a file. Hermes cannot tell what
  the bot wrote into the other fields: text it remembers about you that it put into its instructions, a
  routine or a connector's name is taken like any other text.

Everything botport returns is rebuilt from an allowlist of known fields (type-checked, cleaned and capped)
before it reaches the Twins page, the local API, the tools, events or `status-cache.json`; unknown fields are
dropped.

## What copies over, and what does not

The README's table under "What copies over, honestly" is the whole list, and the Twins page shows the same one
(a twin's **…** menu, **Details…**, **The whole table**), as does `hermes twin copies` in a terminal. All are
written from one file, `twin/copies.py`, and a test compares them, so they cannot say different things. In short, for this version:

- **From Grok to Hermes, by itself**: a bot's name and description, nothing else.
- **Brought over by you**: instructions (pasted on the Twins page), routines and connections (made again in
  Hermes, with a list). Or handed over by the bot, with Import setup export: the bot writes a file about
  itself when you ask it in Grok; instructions are filled in, routines arrive switched off, and connections
  arrive as a list of names.
- **Only with skill sync, which you set up yourself**: skills, in both directions; per twin and item when you allow
  it, name, description, instructions and routines from Hermes to Grok (made by the Mirror bot); Grok's instructions, every 15
  minutes by themselves unless you switch that off (you are asked when it is unsure which to keep); and, only once
  you switch memory facts with Grok on for a twin, the facts of its Grok memory.
- **Only once you switch full transcripts on for a twin**: conversations from a Grok export, which
  `hermes twin transcripts to-hermes` makes into Hermes chats of the twin.
- **Never copied**: a bot's Grok memory unless you switch memory facts on (memory is the most private thing a bot
  holds, so Grok Bot Twin takes none of it by itself; the twin builds its own in Hermes) and its Grok model setting (nothing Hermes reads from Grok names
  the bot's model, so the twin uses the model you choose in Hermes). Logins are never copied, and an item that
  looks like it holds a password or key is left out (that check can miss things; see the next section).
- **Between your own computers**: each twin's name and description, and its settings item by item (tools,
  preferences and instructions follow the Mac with the Grok app; each computer keeps its own model unless you
  switch that on). Skills and routines are each copied only if you switch that on. Memory is copied only for a twin whose memory you switch on, on both
  computers, and then it travels encrypted between your linked computers only. Chats and files stay where they are; the one chat text that travels is a
  request you send to a twin on another computer and its answer, and only with requests switched on.

### Session briefs (off for every twin until you choose a direction)
A brief is a short Markdown note about one finished chat: who took part, what was decided, what is still open, the next
step. It is made without a model (the plugin picks the sentences itself), at most 2,400 characters. Hermes → Grok:
briefs of this twin's finished Hermes chats on this computer (its terminal, terminal window or Desktop; never a chat
from a messaging app, a webhook or the API server) are handed to the Mirror. Until the Mirror is set up, briefs stay
off. Grok → Hermes: briefs the Grok bot wrote are checked line by line and kept in `briefs/<twin>/in`; they are data,
never instructions. Every line goes through the same check as memory notes, on both sides: a line that may hold a
password, key, code, card or account number, reads like an instruction to a model, or holds characters nobody can see
is left out and only counted; links, e-mail addresses, file paths, IP addresses and phone numbers are replaced by a
placeholder. Saying "off the record" in a chat keeps that chat out. Stop twinning deletes the twin's briefs. The
background check runs a round at most every 5 minutes, and only while some twin has briefs on.

## Things that look like a password or key

An item that looks like it holds a password or key is not copied at all (not redacted-and-copied), in both
directions. Only the item, its kind and a one-sentence location are shown, never the value. Earlier versions,
items changed in both places (both versions and the line-by-line changes, masked by botport and again by the
plugin), previews, events and UI state are masked the same way. This check can miss things; it is a check, not a guarantee.

An imported setup export is the one place where a text is copied in part: every line of its instructions that
a password or key touches is left out (all the lines a match covers, and every line of a private key block of
any kind), the sheet names the line numbers, and what is left must pass the same check again or none of it is
copied; more than 200 such lines and none of it is copied either. The check reads each line in its plain form
(full-width and other look-alike letters as ordinary ones) after characters nobody can see were taken out; those
characters are not saved. A routine whose name, prompt, schedule or time zone trips the check is not made at
all, and a connector name that does is dropped. A name is checked whole before it is cut to its length.

## Telemetry and credentials

No telemetry. The optional activity digest (counts and times; short titles a separate opt-in; off by default,
per twin) goes to your own Grok bot, which runs on xAI's service, through your own folder. In this
release real (botport) twins do not send it at all; only the sample shows it, to its invented Grok side.

No account credentials are stored or read, and no API key is needed. Sharing twins between your computers uses
a pass per linked computer that the two computers work out when they are linked (kept in `fleet.json` on the
linked computer; on the sharing one only a key made from it); it is never sent, it is not an account, and it
opens nothing but what this page lists under "Sharing twins with your other computers": the list, and the
settings, skills, routines, memory and requests you left or switched on. botport stores one local pass per twin
(a small owner-only key file, described above), replaced with `hermes twin skill-sync --new-pass` and deleted by
`hermes twin remove`; with skill sync on, a copy lives in the sync bot's chat (known residual,
below).


## Known residual: the pass in the sync bot's chat

Turning on skill sync means attaching the twin's pass in the sync bot's Grok chat, so a copy of that key lives in
that chat on the Grok side. Only use a sync bot you own; replace the pass with `hermes twin skill-sync --new-pass`
if the chat is shared. A better hand-over is planned for botport.

For a twin added later (no chat), and for a pass you replaced, the pass waits as a file in the shared folder's
`inbox/keys/` until the sync bot's next run takes it; whoever can read that folder in that time could copy it.
Use a folder only you can read, and replace the pass (`--new-pass`) if you are unsure. A file someone else puts
into the shared folder cannot add a twin: the list is signed with the inbox pass, which is on this computer, in
the set-up folder you attach (so a copy lives in the sync bot's chat, like the twin's pass) and in the sync
bot's box. Taking a later twin or a replaced pass from the shared folder needs `python3` in that box. The script's box half is tested against botport in a pretend box with invented bots; it has not been run
by this plugin inside a Grok account.

## On Windows

A Windows computer follows the twins of the computer with the Grok app (see "Sharing twins with your other computers");
it never reads Grok itself. There is no botport for Windows, and nothing on this page about botport, `gh` or the
Grok app's folder happens there.

- **Processes.** With requests switched on, the chat-turn processes of "Processes it starts" (this computer's
  own Hermes), and `taskkill.exe /PID <the turn's process id> /T /F` from the Windows system folder when a
  running request is stopped. Besides that only for the background helper (`hermes twin background on | off |
  status`): `schtasks.exe`
  (add, start, look at, end and delete the plugin's one task), `whoami.exe /user` (your account's own id, so the
  task is yours and nobody else's) and, on `off`, `taskkill.exe /PID <the helper's process id> /T /F` when the
  helper did not stop by itself within 8 seconds. All three are run from the Windows system folder (asked from
  Windows itself, not read from a variable), never looked up on `PATH`, with an argument list, no shell and no
  window. The task starts the `hermes.exe` of this Hermes folder (its `bin`), else the one next to the Python
  Hermes runs on, else the first `hermes` on `PATH`; unlike on macOS and Linux, the plugin cannot check on
  Windows who else may change that program. The helper is ended by its process id, checked
  against its start time; never by name.
- **The scheduled task.** One task of your own account, `grok-bot-twin-follower` (with a suffix for a Hermes
  folder that is not the standard one). No administrator rights are asked for, it runs with least privilege in
  your own session, never as SYSTEM, and stores no password. It starts when you sign in and looks every
  15 minutes whether the helper still runs. It is the only thing the plugin adds outside the Hermes folder on Windows.
  `hermes twin background off` (or Remove Grok Bot Twin) deletes it.
- **Files.** In the plugin's data folder: `follower-agent.vbs` (the small launcher the task runs with Windows'
  own `wscript.exe`, so no window shows; the way Hermes starts its own gateway), `follower-agent.pid` (the
  helper's process id and start time), `follower-agent.log` (one line per event, at most 1 MB, one older file
  kept) and, for a moment on `off`, `follower-agent.stop`. All deleted by `off`. The helper has one thread
  that looks every 2 seconds whether `follower-agent.stop` is there.
- **Network.** The helper only connects out, to the one address and port of the computer that shares its twins.
  It never listens, so Windows Firewall has nothing to ask.
- **Who can open the files.** Windows has no owner-only file mode. The plugin's folders take the access list of
  your Hermes folder, which under your Windows profile admits you, SYSTEM and Administrators (the same as
  Hermes' own files there). Links and junctions are refused when the plugin opens its files; a file is looked
  at, opened, and compared with what was looked at; a file Windows gives no file number for is refused. The
  plugin does not set or check access lists itself: on a Hermes folder you moved somewhere that other accounts
  can write to, they could change the plugin's files as they could Hermes' own. Windows has no way to open a
  file "below this already-open folder", so each folder on the way is looked at and the file opened after;
  that check is against links and accidents, not against a program that can already write in your Hermes
  folder.
- **Unlinking while a twin's chat is open.** Windows cannot move a folder with an open file in it. Such a twin
  is not moved to the kept place: it stays where it is as an ordinary Hermes bot that follows nothing.
- **Installed from an administrator window.** Hermes makes an installed plugin's folder for its owner only.
  From an administrator window (or a remote shell) the owner is the Administrators group, and Windows then
  refuses your own ordinary programs: Hermes Desktop does not show the Twins page and the helper cannot start.
  The plugin only looks: on `hermes twin`, `link`, `fleet`, `doctor` and `background on` it asks Windows who
  owns its own installed folder and whether that folder's access list is its own (two read-only system calls,
  `GetNamedSecurityInfoW` and `GetSecurityDescriptorControl`; no program is started). When both are so it says
  it, prints the one `icacls` line that puts the folder back under your Hermes folder's access list, and
  `background on` adds nothing. The plugin never changes an access list itself.
- **Line ends.** The plugin writes plain `\n` line ends on every system. A prompt file (`SOUL.md`) or
  `config.yaml` that already uses Windows line ends, or starts with a byte-order mark, keeps them.

## Words used on the page and here

The Twins page says "botport" for the botport program, "verified" for a botport whose SHA-256 is on the
plugin's list of release files or whose GitHub build attestation passed,
"skill sync" and "the sync bot" for botport's Mirror bot route, "shared folder" for its pair folder, "pass" for
its pair key (a small file), "Hermes bot" for the Hermes profile a twin lives in, "connections" for connectors,
"earlier versions" for restore points, "changed in both places" for a conflict, and "sample" for the built-in
demo. In the terminal, `versions`, `compare`, `skill-sync` and `verify` are the commands that used to be called
`restore-points`, `parity`, `mirror` and `attest`; the old names still work.
