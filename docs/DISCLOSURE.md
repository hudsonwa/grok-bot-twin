# Grok Bot Twin: disclosure

What the plugin does on your machine, for the Hermes plugin catalog's rule 13 and for you. Unofficial; not
affiliated with xAI.

## Processes it starts

All with an argument list (never a shell), stdin closed, a timeout, capped output and a minimal environment
(no tokens or keys are passed on).

| Command | When | Why |
|---|---|---|
| `botport ...` (a private, digest-checked copy of the binary you trusted) | status, sync, the background check, every Twins page action | the sync engine |
| `botport version --json` | `hermes twin setup`, run on the private copy only after its SHA-256 and its GitHub build attestation checked out (or after your explicit `--allow-unattested` opt-out) | check schema and features before trusting it |
| `gh attestation verify <botport> --repo hudsonwa/botport --signer-workflow hudsonwa/botport/.github/workflows/release.yml` | when you run `hermes twin setup` (mandatory unless you pass `--allow-unattested`) or `hermes twin attest` (or call the matching `engine/trust` / `engine/attest` route of the plugin's local API) | check botport's GitHub build attestation |
| `hermes [-p PROFILE] cron resume ID` | only when you press "Ready to switch" for a job of this twin (found by its job id, recorded when the twin was made, or by the importer's mark on the job; never by its name), and only when Hermes offers no tool-approval step to the plugin (when it does, the resume goes through that step instead, and a refusal there stops the switch) | resume the Hermes copy of a routine |

botport is never downloaded, bundled or updated by the plugin. Before every run the plugin re-checks the
binary's SHA-256 against the record made at setup, that it and its folders are owned by you or root and not
writable by others, and that no link was swapped in; it fails closed and says why on the Twins page. A trust
record without a verified attestation is used only when you opted out explicitly with `hermes twin setup --allow-unattested`
(a loud, recorded opt-out for local builds: a build of your own); doctor and status then show "botport trusted WITHOUT attestation (local build)".

`gh` and `hermes` are found once on `PATH` and resolved to their absolute real paths (inside a Hermes process
the Hermes CLI is Hermes' own Python with `-m hermes_cli.main`). Each must pass the same check as botport (the
file and every folder above it owned by you or root, not writable by group or others) every time before it
runs; otherwise it is refused with a message saying why. `hermes twin doctor` shows both paths and the result.

## Files it reads

- Its own data folder (below).
- Through botport, from the Grok desktop app's local files: the bot list (id, name, description) plus the
  selected bots' local transcript files, capped (200 files, 64 MiB) and parsed only for routine-change events
  (nothing else is kept or shown). botport never opens login or credential files (no other file in the app's
  folder: not its secrets file, cookies or any login file), and refuses a cache folder that is a link.
- For "Ready to switch": the twin profile's cron job list (Hermes' cron store, read only).
- Hermes' own `config.yaml` plugin settings (through Hermes' public config reader).

It never reads Hermes' `.env` or other credential files, keychains, browser profiles or any credential store.

## Files it writes

Under `plugin-data/grok-bot-twin/` in the Hermes root (shared by all profiles) (folders 0700, files 0600, atomic writes: temp file in the
same folder, fsync, rename; links are not followed):

| Path | What |
|---|---|
| `pairs.json` | the twins this plugin knows (ids, bot name, profile, and the ids of the cron jobs made from the bot's routines, recorded when the twin was made) |
| `status-cache.json` | last status, so the per-turn note never waits for botport |
| `waiting.json` | per twin, the item ids botport last listed as waiting for your approval (so the Approve buttons survive a refresh) |
| `events.jsonl` | last 500 events, metadata only (type, time, twin id, bot name, counts, item ids; never item text or secret values) |
| `activity.json` | per twin: time of the last Hermes chat turn and a count (no content) |
| `digest.json` | per-twin digest switches (off unless you turn them on) |
| `botport-state/` | botport's own state for your twins (passed as `--state-dir`) |
| `botport-trust.json`, `botport-bin/` | the trust record (path, SHA-256, version, attestation result, and whether you opted out of the attestation) and the verified private copy of botport |
| `demo/` | the demo world (invented bots); `hermes twin demo remove` deletes it |
| `.lock` | an empty file used as an advisory lock so CLI, gateway and dashboard processes do not write at once |

botport, when you add a real twin, creates or adopts that twin's Hermes profile and keeps its own records in
it (`botport.mirror.json`, `botport/`, `backups/botport/`); `hermes twin remove` removes them and keeps the
profile.

### The pair key

When you add a real twin (`twin create`, for every twin), botport creates a per-twin key in its state folder (`botport-state/pairs/`, an
owner-only 0600 file). It signs the Grok-side snapshots botport composes on this machine (HMAC-SHA256), and,
when you connect a Mirror bot (`hermes twin mirror`), it authenticates the packs between your Hermes and your
Mirror bot: you attach the key file in the Mirror bot's chat (never paste it). Rotate it with
`hermes twin mirror --rotate-key` (which runs `botport twin mirror --rotate-key`; the old key then verifies
nothing, and a connected Mirror bot needs the new file); disconnecting with `hermes twin mirror --off` also
replaces it. `hermes twin remove` deletes it. It is not an account credential: it never leaves your machine
except as the file you attach yourself.

## Background work

One thread per Hermes process, started lazily (first chat turn, session start or Twins page request, never in
`register()`), stopped when the plugin unloads. Every `sync_interval_seconds` (default 60, minimum 5) it runs
`botport twin check` (file metadata only: time, size, inode) and syncs only the twins whose Grok side
changed. It backs off on repeated errors. The per-turn `on_session_end` hook writes only a debounced
timestamp marker.

## Network

The plugin makes no network calls and uploads nothing itself. botport makes none for twins: the Mirror bot route is a folder that your
own sync app keeps in step. `gh attestation verify` talks to GitHub only when you run it.

## What reaches the model

- `pre_llm_call`, only in a twinned profile and only if `inject_context` is on: at most 10 lines of fixed
  wording, "your Grok twin", an opaque twin ref (`t-` plus 8 hex characters), the state label and counts.
  No bot name, description, skill, routine or any other free text from Grok.
- `twin_status` / `twin_recent` tool results: allowlisted fields in plain language, labelled as data. By
  default the same fixed label "your Grok twin" and opaque twin id (`t-` plus 8 hex characters), with counts
  instead of names ("Turn off 2 routines in Grok when you switch (names on the Twins page)"): no bot, profile,
  routine, skill or connector names. Only with the `share_names_with_model` setting on do they carry names,
  and then only names of 1-40 characters of `A-Z a-z 0-9 space . _ -` (else fixed words such as "one skill").
  Engine free text (state reasons, by-hand sentences) is never passed through.

Everything botport returns is rebuilt from an allowlist of known fields (type-checked, cleaned and capped)
before it reaches the Twins page, the local API, the tools, events or `status-cache.json`; unknown fields are
dropped.

## Suspected secrets

An item that looks like it holds a key is held back whole (not copied, not redacted-and-copied) in both
directions. Only the item, its kind and a one-sentence location are shown, never the value. Restore points,
conflicts (both copies and the diff, masked by botport and again by the plugin), previews, events and UI state are masked the same way. This is a check, not a guarantee.

## Telemetry and credentials

No telemetry. The optional activity digest (counts and times; short titles a separate opt-in; off by default,
per twin) goes to your own Grok bot, which runs on xAI's service, through your own folder. In this
release real (botport) twins do not send it at all; only the demo shows it, to its pretend Grok side.

No account credentials are stored or read, and no API key is needed. botport stores one local pair key per twin
(owner-only file, described above), rotated with `hermes twin mirror --rotate-key` and deleted by
`hermes twin remove`; when the Mirror bot link is on, a copy lives in that Mirror bot's chat (known residual,
below).


## Known residual: the pair key in the Mirror bot's chat

Connecting a Mirror bot means attaching the pair key file in that bot's Grok chat, so a copy of the key lives in
that chat on the Grok side. Only connect a Mirror bot you own; rotate the key with `hermes twin mirror --rotate-key`
if the chat is shared. A better hand-over is planned for botport v0.2.1.
