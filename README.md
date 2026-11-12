# Grok Bot Twin

**Try Hermes without leaving Grok. Move over when your twin is ready.**

For people who built bots in the Grok desktop app and want to try Hermes without giving them up. Grok Bot
Twin gives each Grok bot a **twin**: a second home for the bot in Hermes that keeps its name and description
in step with Grok. You paste its instructions once. Nothing changes in Grok. A **Twins** page in Hermes
Desktop shows each twin in one line, asks you to choose when something changed in both places, keeps earlier
versions for undo, and walks you through the day you move its routines.

> **Needs Hermes 0.22.7 or newer.** Check with `hermes --version`; `hermes update` brings it up to date.

> **Which version you get.** The **Plugins** button in Hermes Desktop, and installing by the catalog name
> `grok-bot-twin`, give the Hermes catalog version, 0.2.1, which is older than what this README describes. To get
> this version, install it from the `next` branch in a terminal (Hermes warns "custom (unreviewed) source", because
> it is not the catalog):
>
> ```sh
> hermes plugins install hudsonwa/twin --ref "$(git ls-remote https://github.com/hudsonwa/twin refs/heads/next | cut -f1)" --enable
> ```
>
> `--ref` takes only a full 40-character commit, not a branch name; the `git ls-remote` part prints the newest one.
> In Windows PowerShell, run `git ls-remote https://github.com/hudsonwa/twin refs/heads/next` alone and put the
> first column after `--ref`.

## Install

- **In a terminal:** paste the line above. Plain `hermes twin` then prints your twins and the one next step.
- **In Hermes Desktop:** after that line, open **Capabilities → Plugins** and check that **Grok Bot Twin** is on
  (if it shows two switches, turn on both). Installing it from that page instead gives the catalog version.
- **On your other computers:** the same line; they need no Grok app and no botport.

Your real bots also need [botport](https://github.com/hudsonwa/botport) (MIT), a separate small, free app by the
same author that reads your bot list from the Grok desktop app. You download it yourself (step 2 shows how); the
plugin never downloads it, and verifies it before it ever runs. The sample does not need it. Unofficial; not
affiliated with xAI.

## First steps

Open **Twins** in the Hermes Desktop sidebar.

1. **See a sample first.** One sample twin with invented data: no Grok account, no botport, nothing of yours
   touched. Press **Change the bot in Grok** and watch the twin follow; **End sample** removes it. In a
   terminal: `hermes twin demo` ([the sample tour](docs/TERMINAL.md#the-sample-tour)).
2. **Connect my Grok bots.** Two rows tick themselves, once, in about a minute: the Grok desktop app (signed
   in on this computer) and botport (**Download botport**; Hermes finds the download and checks it). Then
   choose your bots and press **Twin this bot**. Nothing changes in Grok.
3. **Finish each twin.** Paste its instructions, or let the bot write its setup export and drop that file on
   the sheet, and choose a model. The row then says "Follows Grok · ready", and **Open in Hermes** starts a
   chat with the twin.

On a computer without the Grok app, press **Link this computer** instead ([docs/SHARING.md](docs/SHARING.md)).
The same setup from a terminal, a Hermes chat or a coding agent: [docs/GUIDE.md](docs/GUIDE.md#start-from-wherever-you-are).

![The Twins page: one line per twin, and one button only when something needs you](docs/screenshots/twins-page.png)

## What is ready and what is experimental

**Ready** means it ships in this version, its tests run on every change, and it waits on no part that is not
built or not yet run on the real Grok app. **Experimental** means it ships and is tested too, but it is off until
you switch it on and is newer or has more moving parts (some of it needs the sync bot in your Grok account, which the
tests here can run only in a pretend box): watch its first runs.

| Feature | State | Read more |
|---|---|---|
| The Twins page, the sample, connecting your Grok bots | ready | [Guide](docs/GUIDE.md#connect-your-grok-bots) |
| Name and description follow Grok, in seconds | ready | [How fast](docs/GUIDE.md#how-fast-is-it) |
| Instructions by paste or setup export; routines arrive switched off | ready | [Import](docs/GUIDE.md#import-setup-export) |
| Changed in both places, earlier versions (undo), password-and-key check | ready | [Everyday use](docs/GUIDE.md#everyday-use) |
| Move routines to Hermes, Uninstall | ready | [Guide](docs/GUIDE.md#uninstall) |
| Twins on your other computers: names, descriptions, settings (Mac shares; Mac, Linux, Windows link) | ready | [Sharing](docs/SHARING.md) |
| Skills, routines and memory between your computers | experimental | [Sharing](docs/SHARING.md#skills-between-your-computers) |
| Ask a twin on another computer | experimental | [Sharing](docs/SHARING.md#ask-a-twin-on-another-computer) |
| Up to date while Hermes is closed (terminal only) | experimental | [Sharing](docs/SHARING.md#in-the-terminal) |
| Skill sync through a sync bot in your Grok account | experimental | [Skill sync](docs/GUIDE.md#skill-sync-optional) |
| Hermes → Grok, memory facts with Grok (need the sync bot) | experimental | [What syncs](docs/GUIDE.md#what-syncs-and-hermes--grok) |
| A twin's whole conversations (archive and search) | experimental | [Archive](docs/GUIDE.md#a-twins-whole-conversations-archive-and-search) |
| Hermes clicking in the Grok app for you; a Grok bot setting itself up | design only, not built | [SETUP-ORIGINS](docs/SETUP-ORIGINS.md) |

## What copies over, honestly

The Twins page shows the same table (a twin's **…** menu, **Details…**, **The whole table**), and so does
`hermes twin copies` in a terminal.

<!-- what-copies:begin (written by twin/copies.py: change the table there) -->

| What | How |
|---|---|
| **From Grok to Hermes** | |
| Name and description | By itself, in seconds, while Hermes is open. |
| Instructions, routines and connections | With Import setup export: the bot writes a file about itself when you ask it in Grok. Instructions are filled in, routines arrive switched off, and connections arrive as a list of names. Logins are never copied. With skill sync set up, instructions also follow Grok's every 15 minutes by themselves, unless you switch that off. When it is unsure which to keep, you are asked. |
| Skills | With skill sync, which is optional: a sync bot in your own Grok account carries them. Without it you copy them yourself. |
| Conversations | Off until you switch full transcripts on for a twin. Then conversations from a Grok export can become Hermes chats of the twin; full transcripts are set up in the terminal. |
| Grok memory | Does not copy: memory is the most private thing a bot holds, so none is taken unless you switch on memory facts, which need skill sync. |
| Grok model setting | Does not copy: nothing Hermes reads from Grok names the bot's model, so the twin uses the model you choose in Hermes. |
| **From Hermes back to Grok** | |
| Name, description and instructions | Off for every twin until you allow it. Then, with skill sync set up, you see a preview of each change and apply it. A bot in your own Grok account makes it there, and you can undo it. Otherwise you make the same change in Grok yourself. |
| Skills | With skill sync, after you press Send to Grok: a skill Grok does not have yet is installed there. A skill Grok already has under that name is not replaced; paste the change into Grok yourself. Without skill sync you paste them all yourself. |
| Routines and connections | Routines go like the name, once you allow them for a twin. Each routine runs in one place that you choose, never in both. Connections you change in Grok yourself. |
| **Between your own computers** | |
| Name and description | By itself, in seconds, once the other computer is linked and Hermes is open on both. |
| Skills | Off until you switch it on. Then the text of each twin's skills follows the Mac with the Grok app. A skill changed on both computers asks you which one to keep. |
| Routines | Off until you switch it on. Then each twin's routines copy to every linked computer, and each routine runs on one computer that you choose, never on two. |
| Memory | Off until you switch it on for a twin. Then it travels encrypted, between your linked computers only. |
| Settings | By itself, item by item: tools, preferences and instructions follow the Mac with the Grok app. Each computer keeps its own model unless you switch that on. |
| Instructions | By itself, as one of the twin's settings. You can switch that off on each computer. |
| Chats, files and logins | Not copied. A request you send to another computer and its answer are the only chat text that travels, and only with requests switched on. |

<!-- what-copies:end -->

## More

- [docs/GUIDE.md](docs/GUIDE.md): everyday use, setup exports, skill sync, how fast it is, settings, uninstall.
- [docs/SHARING.md](docs/SHARING.md): your twins on your other computers, their memory, and asking them.
- [docs/TERMINAL.md](docs/TERMINAL.md): every command, the setup command and the sample tour.
- [docs/ROADMAP.md](docs/ROADMAP.md): what comes next.
- [AGENTS.md](AGENTS.md): for a coding agent that sets it up for you.

## Disclosure

Everything the plugin reads, writes, runs or sends is in [docs/DISCLOSURE.md](docs/DISCLOSURE.md).

## Credits

- [botport](https://github.com/hudsonwa/botport) (MIT): the sync engine this plugin runs.
- Moving a bot once and for all? Nous Research's official **agent-importers** plugin
  (`hermes import-agent grok-bot`) does a one-time import from a bot export. Grok Bot Twin is for the time in
  between: it keeps the twin up to date while you try Hermes. Reading the Grok app's local files is botport's own code.
- Hermes Agent by Nous Research: the plugin API, skill scanner and Desktop SDK.

## License

MIT, see [LICENSE](LICENSE).
