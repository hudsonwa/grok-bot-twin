# Grok Bot Twin

**Try Hermes without leaving Grok. Switch when your twin is ready.**

For people who built bots in the Grok desktop app and want to try Hermes without giving them up. Grok Bot
Twin gives each Grok bot a Hermes "twin": a Hermes profile with the bot's name and description, and what
else can travel. It adds a **Twins** page to Hermes Desktop that shows what is in step, lets you pick a side
when both changed, keeps restore points, and gives you a checklist for the day you switch.

**Try it in 60 seconds, no Grok account needed** (invented demo bots; nothing is written to your Hermes profiles):

```sh
hermes plugins install grok-bot-twin && hermes plugins enable grok-bot-twin
hermes twin demo
```

Then open **Twins** in Hermes Desktop and press **Simulate Grok edit**, **Simulate conflict** or
**Simulate leaked key**. (Before the catalog entry is merged, install from GitHub instead:
`hermes plugins install hudsonwa/grok-bot-twin --ref <sha>`, using the 40-character `sha` pinned in the catalog
entry `plugin-catalog/grok-bot-twin.yaml`. The catalog name works, at that same pin, once it is listed.)

Honest scope: name and description changes reach the twin in seconds; instructions stay on the by-hand list;
skills travel through an optional Mirror bot you run; routines and connectors are on a by-hand checklist.

Unofficial; not affiliated with xAI. Real twins need [botport](https://github.com/hudsonwa/botport) (MIT), a
separate command-line tool by the same author that you install yourself; the demo does not need it.

| Pick one when both sides changed | Ready to switch |
|---|---|
| ![Conflict card: Hermes and Grok copies side by side](docs/screenshots/conflict-card.png) | ![Ready to switch checklist](docs/screenshots/ready-to-switch.png) |
| Both sides changed? See both copies and pick one; nothing is overwritten silently. | Turn off each Grok routine, then turn on its paused Hermes copy, so nothing runs twice. |
| ![Twins page with two demo twins](docs/screenshots/twins-page.png) | ![Preview of a new twin](docs/screenshots/add-twins-preview.png) |
| One card per twin: its state, what syncs, and what is still yours to do. | Add twins: a preview of what each twin brings, before anything is written. |

All screenshots use the built-in demo with invented bots. The demo twin started from a bot export and has a
Mirror bot connected. A twin made only from the Grok app's cache starts with its name and description.

## The full demo tour

The demo has invented bots: Acme Helper (skills greet-visitors and fun-fact, routines "Morning summary" and
"Water reminder", connector example-files) and Recipe Planner. It needs no Grok account, no botport and no
network, and it never writes to your Hermes profiles: the demo lives in the plugin's own data folder.

```sh
hermes twin demo                 # a demo twin of "Acme Helper", in step
hermes twin demo grok-edit       # change the bot's description on the pretend Grok side
hermes twin sync                 # the twin follows (the background check would do it within a minute)
hermes twin demo secret          # put an obviously fake key into the pretend Grok description
hermes twin sync                 # "1 suspected secret held back (a check, not a guarantee)"
hermes twin demo conflict        # edit the skill greet-visitors on both sides
hermes twin sync                 # "1 conflict"
hermes twin conflicts            # both copies and a diff (commands pick the only twin when there is one)
hermes twin resolve skill:greet-visitors --keep grok   # the old Hermes copy becomes a restore point
hermes twin restore-points       # skill:greet-visitors@<12 hex>
hermes twin restore skill:greet-visitors@<12 hex>      # puts the Hermes copy back
hermes twin add --bot "Recipe Planner"   # a second twin, from the pretend cache (preview first)
hermes twin demo remove          # clean up
```

In Hermes Desktop, turn on the plugin's desktop half (Capabilities, then Plugins) and open **Twins**, or
press **Try demo** on the empty Twins page. In a chat, `/twin` shows the same status.

## What syncs, honestly

From the Grok desktop app's local files, botport opens only the bot list (id, name, description) and the
selected bots' transcript files, read only for routine-change events. Those files hold no instructions, skills,
schedules or connectors, so the plugin does not pretend otherwise.

The **Mirror bot** is optional: a Grok bot you run yourself, whose routine carries skills between Grok and
Hermes. botport talks to it only through a folder your own sync app (for example Google Drive for desktop)
keeps in step.

| Item | Grok -> Hermes | Hermes -> Grok |
|---|---|---|
| name, description | syncs in seconds, from the cache | by hand (in the Grok app) |
| instructions | by hand (on the by-hand list) | by hand |
| skills | through your Mirror bot routine (optional); else by hand | through your Mirror bot (`hermes twin mirror`, after your approval); else by hand |
| routines | by hand: the Ready to switch list names each one to turn off in Grok; a twin that started from a bot export also has paused Hermes copies for you to turn on | by hand |
| connectors | by hand | by hand |

The Twins page shows these as chips: **Syncs**, **Via Mirror routine**, **By hand**, **Not carried**,
plus a by-hand checklist and a "Ready to switch" list (Grok routines to turn off, paused Hermes copies to
turn on).

What else you get:
- **Conflicts**: when one item changed on both sides, you see both copies and a diff, and pick
  "Keep Hermes" or "Keep Grok". Nothing is overwritten silently. Keep Grok replaces the Hermes copy; Keep
  Hermes keeps it and puts the item on the list for Grok (by hand, or through your Mirror bot).
- **Restore points**: when a Hermes copy is replaced (by a sync from Grok or by Keep Grok), the old copy is
  kept as a restore point named after the item (`skill:greet-visitors@3f2a9c1b7e40`), and you can put it
  back. A restore changes only that item in Hermes; anything else (conflicts, secrets held back) stays as it is.
- **Suspected secrets held back**: an item that looks like it holds a key (known key formats, private key
  blocks, long random-looking words) is not copied at all. You see which item and where, never the value.
  Suspected secret held back — a check, not a guarantee: keep secrets out of a bot's text.
- **Approvals**: a Hermes skill bound for Grok waits for your approval first.
- **Setup parity**: a checklist of what is the same on both sides (no model is called).
- **Agent awareness**: in a twinned profile the model gets a short, data-only note each turn ("your Grok
  twin", an opaque id such as `t-1a2b3c4d` and its state). The `twin_status` and `twin_recent` tools use the
  same label and id: by default no bot, routine, skill or connector names reach the model (the to-do list says
  "Turn off 2 routines in Grok when you switch (names on the Twins page)"). Turn on `share_names_with_model` to
  let the tools name them, reduced to plain characters; the per-turn note never carries names.

### Example: the agent answers from `twin_status`

An example chat in the demo twin's profile (the reply is the model's wording; the facts and counts come
from the `twin_status` tool result for the demo, with names off):

> **You:** What do I still need to set up in Grok?
>
> **Assistant** (called `twin_status`): Your Grok twin is in step, with no changes waiting and no conflicts.
> In Grok: turn off 2 routines when you switch, so nothing runs twice (their names are on the Twins page),
> and if you change the name, description or instructions here in Hermes, make the same change in Grok by
> hand. In Hermes: turn on the 2 paused routines when you switch and connect 1 connector yourself. Skills
> travel through your Mirror bot's routine.

## Install botport (for real twins)

The demo works without it. For your real Grok bots, install botport from a pinned release tag and check its
build attestation before you trust it (needs the [GitHub CLI](https://cli.github.com/), signed in with `gh auth login`):

```sh
# 1. Download release tag v0.2.0 for your platform
TAG=v0.2.0 T=aarch64-apple-darwin   # or x86_64-apple-darwin, x86_64-unknown-linux-musl, aarch64-unknown-linux-musl
gh release download "$TAG" --repo hudsonwa/botport --pattern "botport-$T.tar.gz"
# 2. Verify it was built from that tag by botport's own release workflow
gh attestation verify "botport-$T.tar.gz" --repo hudsonwa/botport \
  --signer-workflow hudsonwa/botport/.github/workflows/release.yml --source-ref refs/tags/v0.2.0
# 3. Unpack, put botport on your PATH (or set botport_path in the plugin settings)
tar -xzf "botport-$T.tar.gz" && mkdir -p ~/.local/bin && cp "botport-$T/botport" ~/.local/bin/
# 4. Let the plugin check and trust it (it runs gh attestation verify on the binary itself; this must pass)
hermes twin setup
hermes twin doctor
```

`hermes twin setup` checks that botport and its folders are owned by you (or root) and not writable by
others, keeps a private copy, verifies the copy's GitHub build attestation with `gh attestation verify`
(mandatory), and only then runs it once to check its features and records its SHA-256. Before every run the
plugin checks the digest again and refuses to run a changed binary. The plugin never downloads or updates
botport. For a botport you built yourself there is a loud opt-out, `hermes twin setup --allow-unattested`
(REST: `{"allow_unattested": true}`): it is recorded, and `hermes twin doctor` and `hermes twin status` show
"botport trusted WITHOUT attestation (local build)" until you trust an attested build. The `gh` and `hermes`
commands the plugin runs are resolved to their real paths and get the same owner/permission check; doctor
shows both paths.

`hermes twin setup` pins the release it verifies (`--source-ref refs/tags/v0.2.0`); to trust another release set
`botport_release_tag` or run `hermes twin setup --tag vX.Y.Z`. The plugin also refuses to run if any folder from
the Hermes root down to its data folder is a symlink, owned by someone else or writable by others. The model sees a
twin only as an opaque id (`t-` plus a keyed hash with a random per-install secret), never by name unless you turn
on `share_names_with_model`.

Then:

```sh
hermes twin list-grok            # bots in your Grok app (filter with the `bots` setting, e.g. "Acme*")
hermes twin add --bot "Acme Helper"   # shows a preview first (suspected secrets masked), then asks
hermes twin add --bot "Recipe Planner" --hermes-profile recipe-planner   # pick the profile (made when missing)
hermes twin status
```

## Commands

`hermes twin status | sync | list-grok | add | conflicts | resolve | restore-points | restore | recent |
parity | checklist | switch | send-to-grok | approve | mirror | remove | digest | setup | attest | doctor | demo`.
Each twin gets its own Hermes profile (default: a slug of the bot name); a profile that already backs a twin is
refused. The option is `--hermes-profile`, because `hermes` itself reads `--profile`/`-p` anywhere on the command line.
With exactly one twin, commands that take a twin id use it when you leave the id out (`remove` always asks for it).
`hermes twin mirror [PAIR] --pair-folder DIR | --off | --rotate-key` connects the Mirror bot, disconnects it, or
replaces the twin's pair key.
Slash command: `/twin [status | sync | conflicts [PAIR] | recent | demo ...]`.
Agent tools: `twin_status` (plain language: state, last sync, what to do by hand in Grok and in Hermes,
changes waiting, conflicts, Mirror bot), `twin_recent`. Both say "your Grok twin" and an opaque id unless
`share_names_with_model` is on.

## Settings

Desktop, Settings, Plugins, Grok Bot Twin (or `plugins.entries.grok-bot-twin.settings` in `config.yaml`):

| Setting | Default | What it does |
|---|---|---|
| `sync_interval_seconds` | 60 (min 5) | how often the background thread checks the Grok cache (file metadata only) |
| `bots` | `[]` | name globs of bots to list for twinning; nothing is twinned until you pick it |
| `botport_path` | `""` | botport binary when not on PATH |
| `botport_release_tag` | `""` (uses v0.2.0) | the botport release tag `hermes twin setup` verifies the attestation against; empty means the release this plugin version expects, v0.2.0 |
| `grok_app_dir` | `""` | override the Grok app folder |
| `grok_account` | `""` | only when the Grok app holds more than one account: the 8-character label botport lists for yours (an opaque label, never the account) |
| `inject_context` | `true` | the short per-turn note in a twinned profile |
| `digest_to_grok` | `false` | allows a per-twin activity digest (counts and times) with "Send to Grok"; each twin still starts off |
| `digest_titles` | `false` | separate opt-in for short, masked titles in that digest |
| `share_names_with_model` | `false` | let the `twin_status` / `twin_recent` tool results name the bot, its routines, skills and connectors (plain characters only); off, they say "your Grok twin" and an opaque id. Never affects the per-turn note |
| `demo_profile` | `""` | profile the demo twin is shown in |

## Disclosure

Full details: [docs/DISCLOSURE.md](docs/DISCLOSURE.md).

- **Subprocesses**: runs the external `botport` CLI (argument list, no shell, closed stdin, timeouts, capped
  output, minimal environment). Setup requires botport's GitHub build attestation;
  `hermes twin setup --allow-unattested` is a loud, recorded opt-out for local builds (doctor and status then show "botport trusted
  WITHOUT attestation (local build)"). `gh attestation verify` when you run `hermes twin setup` or `hermes twin attest`,
  and `hermes cron resume` (only when you press "Ready to switch"). `gh` and `hermes` are resolved to their real
  paths and refused unless only you (or root) can change them.
- **Files read**: through botport, from the Grok desktop app's local files: the bot list (id, name,
  description) plus the selected bots' local transcript files, capped (200 files, 64 MiB) and parsed only for
  routine-change events (nothing else is kept or shown). botport never opens login or credential files (no other
  file in the app's folder: not its secrets file, cookies or any login file). The
  plugin reads its own data folder and, for "Ready to switch", the twin profile's cron job list.
- **Files written**: `plugin-data/grok-bot-twin/` under the Hermes root, shared by all profiles (owner-only
  0700/0600, atomic writes). botport writes the twin profiles it manages and its own state under that folder.
- **Mirror bot folder** (only if you connect a Mirror bot): botport reads and writes files in a folder you choose;
  your own sync app (for example a cloud drive client) may upload them to your own account. The plugin and
  botport upload nothing themselves.
- **Background thread**: one thread, started on the first chat or Twins page visit, checks file metadata
  every `sync_interval_seconds` (default 60) and syncs a twin when its Grok side changed.
- **Desktop page**: while open, it asks the plugin's own local API for new events every 5 s and for status
  every 30 s (nothing leaves your computer).
- **Network**: the plugin itself makes no network calls. botport makes none for twins. `gh attestation
  verify` contacts GitHub when you run it.
- **No telemetry.** The optional activity digest (counts and times; titles a separate opt-in; off by default,
  per twin) goes to your own Grok bot, which runs on xAI's service, through your own folder. In this
  release real (botport) twins do not send it at all; only the demo shows it, to its pretend Grok side.
- **No account credentials** are stored or read. botport stores one local pair key per twin (below).
- **Pair key**: when you add a real twin (`twin create`, every twin), botport creates a per-twin key in its
  state folder (an owner-only 0600 file). It signs the Grok-side snapshots botport writes, and when you connect a Mirror bot it authenticates the
  packs between your Hermes and your Mirror bot (you attach the key file in the Mirror bot's chat). Rotate it
  with `hermes twin mirror --rotate-key` (`botport twin mirror --rotate-key`; disconnecting with `--off` also
  replaces it). `hermes twin remove` deletes it.
  Known residual: when the Mirror bot link is on, you have attached the key file in that bot's Grok chat, so a copy of the key lives
  in that chat on the Grok side. Only connect a Mirror bot you own, and rotate the key if you share the chat. A
  better hand-over is planned for botport v0.2.1.

## Known limitations

- Under `plugins.isolation: host`, Hermes skips plugin CLI commands, so `hermes twin` is unavailable there; `/twin`
  and the Twins page still work.
- botport opens only the Grok app's bot list (id, name, description) and the selected bots' local transcript
  files (capped, parsed only for routine-change events); those files hold no instructions, skills, schedules or
  connectors, so everything else is on the by-hand list (or travels through your Mirror bot, for skills).
- When the Mirror bot link is on, a copy of the twin's pair key lives in that Mirror bot's Grok chat; rotate it
  with `hermes twin mirror --rotate-key` if the chat is shared.

## Credits

- [botport](https://github.com/hudsonwa/botport) (MIT): the sync engine this plugin runs.
- Moving a bot once and for all? Nous Research's official **agent-importers** plugin
  (`hermes import-agent grok-bot`) does a one-time import from a bot export. Grok Bot Twin is for the time in
  between: it keeps the twin in step while you try Hermes. Reading the Grok app's cache is botport's own code.
- Hermes Agent by Nous Research: the plugin API, skill scanner and Desktop SDK.

## License

MIT, see [LICENSE](LICENSE).
