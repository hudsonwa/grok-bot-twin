# Grok Bot Twin in the terminal

Everything on the Twins page has a command. Plain `hermes twin` prints your twins and the one next step.

Install with the `next` line at the top of the [README](../README.md) (the catalog name gives the older 0.2.1).

```sh
hermes twin            # your twins and what to do next
hermes twin demo       # a sample twin with invented data; hermes twin demo remove ends it
```

On a computer without the Grok app, plain `hermes twin` names the one next step there, the link:
`hermes twin link CODE` (see [SHARING.md](SHARING.md)).

To update later: `hermes plugins update grok-bot-twin`. Your twins, links and settings stay as they are. (An
install pinned with `--ref` is moved with the line Hermes prints then: the same install line with `--force` and
the new `--ref`.)

Without `--enable` Hermes asks whether to switch the plugin on; over a remote shell it cannot ask and leaves it
off, and `hermes plugins enable grok-bot-twin` switches it on.

## One setup command

`hermes twin setup` sets up the computer it runs on, from wherever that computer stands.

```sh
hermes twin setup --check                        # where this computer stands and who does what is left; changes nothing
hermes twin setup --plan --bot "Acme Support"    # exactly what a run with these options would do; changes nothing
hermes twin setup --bot "Acme Support"           # does it; asks once first (--yes: do not ask)
hermes twin setup --all --share --yes            # every Grok bot gets a twin, and sharing is switched on
hermes twin setup --link 7KQ4-M2XD-P9RA --yes    # on a computer without the Grok app: link it
```

- `--check` ends with exit status 0 when nothing is left on this computer, and 1 when something is; every line
  that is left says who does it. `--json` gives the same answer to a program.
- A plain run verifies botport, as it always did. It asks once before anything more: before it uses a botport
  download it found in your Downloads folder, twins a bot, switches sharing on, or links this computer.
- Run it as often as you like. A bot that has a twin is left alone, sharing that is on makes no new code, and
  a computer that is linked stays linked.
- It counts your twins and never prints the name of a bot you did not type, and it never repeats a link code
  you typed. So the answer can be shown in a chat with an agent.
- `--check` and `--plan` read the plugin's own records and look whether files are there. They do not run
  botport and do not ask Grok for anything.

## The sample tour

The sample has invented bots: Acme Support (skills greet-visitors and fun-fact, routines "Morning summary" and
"Water reminder", connection example-files) and Recipe Planner. It needs no Grok account, no botport and no
network, it never writes to your Hermes profiles and it stays out of your chats: the sample lives in the
plugin's own data folder.

```sh
hermes twin demo                 # the sample twin of "Acme Support", up to date
hermes twin demo grok-edit       # change the sample bot's description in Grok
hermes twin check                # the twin follows (the background check would do it within a minute)
hermes twin demo secret          # paste an obviously fake password into the sample bot's description
hermes twin check                # "1 thing that looks like a password or key was not copied"
hermes twin demo grok-edit       # changes the description again, so the fake password is gone at the next check
hermes twin demo conflict        # change the skill greet-visitors in both places
hermes twin check                # "1 item changed in both places"
hermes twin conflicts            # both versions, line by line (commands pick the only twin when there is one)
hermes twin resolve skill:greet-visitors --keep grok   # the old Hermes version is kept as an earlier version
hermes twin versions             # earlier versions, numbered: " 1. greet-visitors (skill) · …"
hermes twin restore 1            # puts number 1 back
hermes twin approve "skill:greet-visitors"   # then hermes twin send-to-grok: the sample's Grok side takes it at once
hermes twin add --bot "Recipe Planner"   # a second sample twin (shows the summary first, then asks)
hermes twin demo remove          # "Sample removed. Nothing of yours was changed."
```

In a chat, `/twin` shows the same status.

## Get botport in the terminal (for your real bots)

The Twins page does this with two buttons. In the terminal, download botport for your computer from its
[release page](https://github.com/hudsonwa/botport/releases/tag/v0.2.1) (plain `hermes twin setup` prints the
name of the file for your computer), then hand the download to the plugin:

```sh
hermes twin setup --file ~/Downloads/botport-aarch64-apple-darwin.tar.gz   # the file you downloaded
hermes twin doctor
```

Hermes keeps its own copy of botport and verifies it before it is ever run. A botport you already have on your
`PATH` (or in `~/.local/bin`) works too: just run `hermes twin setup`.

`hermes twin setup` checks that botport and its folders are owned by you (or root) and not writable by
others, keeps a private copy and verifies that copy: its SHA-256 must be on the list of release files that
ships with this plugin version, so no other program and no network is needed. Only then does it run botport
once to check its features, and it records the SHA-256. Before every run the plugin checks the digest again
and refuses to run a changed program. The plugin never downloads or updates botport; it only copies the file
you chose. `hermes twin setup --with-github` verifies botport against the publisher's signed build record on
GitHub instead (this needs GitHub's command-line tool, [`gh`](https://cli.github.com/), signed in once with
`gh auth login`) and prints the exact check it runs (and, while botport is not on this computer yet, the `gh`
command that downloads the release); `hermes twin verify` runs that check again later. If a file is not on the list and `gh` is already installed,
the plugin tries that check by itself. For a botport you built yourself there is a loud opt-out,
`hermes twin setup --allow-unattested` (REST: `{"allow_unattested": true}`): it is recorded, and
`hermes twin doctor` and `hermes twin status` show "botport is in use WITHOUT being verified (local build)"
until you verify a released build. The `gh` and `hermes` commands the plugin runs are resolved to their real
paths and get the same owner/permission check; doctor shows both paths.

`hermes twin setup` verifies against one release (v0.2.1); to accept another release
set "botport release to accept" (`botport_release_tag`) or run `hermes twin setup --tag vX.Y.Z` (a release this
plugin version has no list for is verified with `--with-github`). The plugin also
refuses to run if any folder from the Hermes root down to its data folder is a symlink, owned by someone else
or writable by others. The model sees a twin only as an internal id (`t-` plus a keyed hash with a random
per-install secret), never by name unless you turn on "Let the model see names" (`share_names_with_model`).

Then:

```sh
hermes twin list-grok            # the bots in your Grok app (narrow the list with the "Bots to list" setting)
hermes twin add --bot "Acme Support"   # shows what copies and what you do yourself, then asks
hermes twin add --bot "Recipe Planner" --hermes-profile recipe-planner   # name the twin's Hermes profile yourself
hermes twin
```

## Commands

`hermes twin --help` lists them in five groups:

- **Everyday**: `status` (also plain `hermes twin`), `add`, `finish`, `import-exports`, `stop` (also `remove`).
- **Your other computers**: `share on|off|code`, `link CODE`, `unlink`, `fleet` (also `fleet remove NAME`,
  `fleet name NAME`, `fleet once`, `fleet run`, `fleet settings`, `fleet skills`, `fleet memory`), `routines`
  (`on|off`, `move ROUTINE COMPUTER`, `run-here on|off`, `restore`), `background on|off`; each takes `--json`.
- **Ask another computer**: `ask COMPUTER "TEXT"`, `requests` (the log), `requests on|off`,
  `requests approve|deny ID`, `requests mode [here|safe]`.
- **Fix things**: `conflicts`, `resolve`, `versions`, `restore`, `doctor`, `setup`.
- **Advanced**: `check`, `list-grok`, `recent`, `compare`, `copies`, `checklist`, `syncs`, `import`, `switch`, `approve`,
  `skip`, `send-to-grok`, `skill-sync`, `digest`, `verify`, `demo`, `uninstall`.

Also: `check` (sync) and `move` (switch). The older names still work: `restore-points` (versions), `parity`
(compare), `mirror` (skill-sync), `attest` (verify).

Each twin's Hermes bot is its own Hermes profile (default: named after the bot; a name that is taken gets a
number). The option is `--hermes-profile`, because `hermes` itself reads `--profile`/`-p` anywhere on the
command line. Commands take the twin as `[TWIN]` (its name in quotes, such as "Acme Support", or its short name `acme-support`); with exactly one twin you
can leave it out. `hermes twin skill-sync [TWIN] --folder DIR | --off | --new-pass | --status` turns skill sync on (a later
twin needs no folder: it uses the same one; `--again` makes the set-up folder once more), turns it off (asks first), replaces the twin's pass (the small
file the sync bot needs; the new one goes to the sync bot through the shared folder), or says when the sync
bot last ran. `hermes twin switch
[TWIN]` turns on the switched-off routine copies a twin holds, if it holds any; `--undo` pauses them again.
`hermes twin import [TWIN] FILE`
imports the setup export a bot wrote about itself (it shows the plan and asks first; `--yes` skips the
question, `--show` prints the instructions and prompts whole before it, `--request` prints the text to paste
into the bot's chat, `--bot NAME` picks one bot of a file that holds several). The command refuses when it is
started from the chat turn of a request another computer sent.
`hermes twin finish [TWIN ...] [--all] [--model MODEL]` gives the named twins (or, with `--all`, every twin
with something left) one model, the model your other Hermes chats use unless you name one, and prints the
request to paste into each bot's chat in Grok. `hermes twin import-exports DIR` matches every setup export
file in DIR to its twin by the bot's name (or id) and imports them all after one question (`--yes` skips it,
`--show` prints the text first, `--twin NAME` limits it); it refuses in a request's chat turn too.

Slash command: `/twin [status | check [TWIN] | conflicts [TWIN] | recent [TWIN] | ask COMPUTER: TEXT | requests | demo ...]`.
Anything but status and recent works only in a chat that Hermes names as one of its own local surfaces (its
terminal, terminal UI or Desktop); a messaging app, or a caller that names no surface, gets status only.
Agent tools: `twin_status` (plain language: state, last check, what to do yourself in Grok and in Hermes,
changes waiting, items changed in both places, skill sync, and the two things never copied from Grok),
`twin_recent`. Both say "your Grok twin" and an
internal id unless "Let the model see names" is on, and both say so when the twin is the sample. No tool asks
another computer: only you do (see [Ask a twin on another computer](SHARING.md#ask-a-twin-on-another-computer)).
