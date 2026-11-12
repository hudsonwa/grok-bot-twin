# Your twins on your other computers

Your Grok bots live in the Grok app on one Mac. Hermes may run on several computers. Switch sharing on, and
every twin on that Mac also appears as a Hermes bot on your other computers (Mac, Windows, Linux) and stays in
sync by itself. There is no account, no cloud service and no "main computer" to choose.

Every computer needs Hermes with Grok Bot Twin installed and turned on ([README: Install](../README.md#install)), and
the same private network as the Mac: the same home network, or [Tailscale](https://tailscale.com) on both.
Your other computers need no Grok app and no botport. Installing it there is the same `next` install line as
on the Mac, at the top of the [README](../README.md) (the catalog name gives the older 0.2.1).

## Mac

**The Mac with the Grok app shares** (2 steps):

1. Open **Twins** and switch on **Share my twins with my other computers**.
2. Read the **link code** it shows, like `7KQ4-M2XD-P9RA`, or press **Copy**.

A code links one computer and is good for 10 minutes; **New code** makes the next one. When a computer has
linked, the code goes away and that computer is listed under the switch, with "in sync now" next to its name.

**Another Mac links** (3 steps):

1. Open **Twins** and press **Link this computer**.
2. Type or paste the link code. Capitals, spaces and dashes do not matter.
3. Press **Link**. A pasted code links by itself.

The twins are there about a second later. The page says where they come from ("2 twins from My other Mac · in sync now"),
and each one is an ordinary Hermes bot: **Open in Hermes** starts a chat with it.

## Windows

A Windows computer links to your Mac. It cannot share, because botport has no Windows version.

1. Install Hermes and Grok Bot Twin from an ordinary window, not one opened with "Run as administrator".
2. Open **Twins** and press **Link this computer**.
3. Type or paste the link code and press **Link**.

Nothing else is asked: no administrator rights, and no firewall question, because a linked computer only
calls out to your Mac and never waits for calls.

## Linux

1. Open **Twins** and press **Link this computer**.
2. Type or paste the link code.
3. Press **Link**.

Nothing else to install, open or allow.

## What you get, and how fast

On each linked computer: one Hermes bot per twin, called after it, with its name and description, your default
model on that computer, and a short note in its prompt file that says which computer this copy is on, which
others hold a copy and which one has the Grok app; that is how a twin knows where it is. Of the model only its
name and provider are copied: if yours is reached at an address you typed in yourself, add that address in the
new bot's settings before it can answer. A twin's instructions, tools and preferences follow to a linked
computer as its settings, item by item (see [Settings that follow a twin](#settings-that-follow-a-twin); the last part of the table under
[What copies over, honestly](../README.md#what-copies-over-honestly) has the whole list). Skills are copied only if you
switch that on (see [Skills between your computers](#skills-between-your-computers)), and routines only if you switch Routines on (see
[Routines on your other computers](#routines-on-your-other-computers)). Memory is copied only for a
twin you switch it on for (see [A twin's memory on your other computers](#a-twins-memory-on-your-other-computers)).

Measured between four computers over Tailscale: a Mac that shared, and a second Mac, a Linux desktop and a
Windows 11 laptop that were linked. The twins were invented ones, changed with a test command, so the time the Grok app takes to
save a change (see [How fast is it?](GUIDE.md#how-fast-is-it)) comes on top:

| What | How long |
|---|---|
| Linking, the whole command | 0.5 to 1.8 s; about 5.5 s on Windows when Hermes had to start first |
| A new name or description reaches the other computers | 0.3 to 1.4 s |
| A new twin appears there | 0.6 to 2.5 s |
| A twin you stopped twinning leaves them | 0.4 to 0.9 s |
| With the Mac busy with other work | up to 6 s |

These times were taken with the terminal commands listed further down, on all three systems. The Twins page
does the same through its buttons and shows a change at its next look, every 5 s; it is the same page
everywhere and was clicked through on a Mac.

## What crosses your network, and what never does

| Travels between your computers | Never leaves a computer |
|---|---|
| Each twin's name and description, after the same password-and-key check as everywhere else. A count of its changes, a date, and a label made up for sharing (not the one Grok uses). Which twins were removed. The names of your computers. Each twin's settings, item by item as you switch them (see "Settings that follow a twin" below): its tools, preferences and persona, and its model only if you switch that on. Only if you switch it on: the text of each twin's skills (see [Skills between your computers](#skills-between-your-computers)). | Chats, files, model keys, logins, passwords, tokens, the `.env` file, how a connector is set up, approval and security settings. Routines, unless you switch Routines on (below). Memory, unless you switch it on for a twin (below). A twin's skills while that switch is off, and at all times every file that is not a text file of a skill. Grok's own labels for your bots. Everything else. |

Your computers talk to each other directly; nothing goes to us or to any service. On Tailscale all of it is
encrypted. On a plain home network it is not: someone on that network could read everything in the left
column (names, descriptions, each twin's settings with its instructions, and skills, routines and requests if
you switched them on), though not change it. A twin's memory is the exception: the plugin encrypts it on any
network. If that matters to you, use Tailscale.

Four more things can travel, each only if you switch it on: a twin's skills (next section), a twin's
routines ([Routines on your other computers](#routines-on-your-other-computers)), a request to a twin on another computer and its
answer ([Ask a twin on another computer](#ask-a-twin-on-another-computer)), and, twin by twin, a twin's memory
([A twin's memory on your other computers](#a-twins-memory-on-your-other-computers)).

## Skills between your computers

Off until you switch it on. On the Mac with the Grok app, open **Twins** and switch on **Share my twins'
skills with my other computers** (or run `hermes twin fleet skills on`). From then on each twin's skills go to
every linked computer, into the same twin's skills folder there, and a change on the Mac follows by itself.

- **What is sent.** The text files of each skill in the twin's own skills folder: `SKILL.md` and the other
  text files beside it, up to 200 KB a file, 40 files and 600 KB a skill. They travel over the same signed
  link as the names: encrypted on Tailscale, readable (not changeable) on a plain home network.
- **What is never sent.** Anything in the text that looks like a password or key is replaced by a marker
  before it leaves the Mac; your own file on the Mac is not changed. Files that are not text, files whose
  name starts with a dot (`.env`) and files named like key files (`token.pem`, `id_rsa`) are left out, and the
  page says how many.
- **One direction.** Skills go from the Mac to the linked computers. What you change in a skill on a linked
  computer stays on that computer and is never sent back.
- **Changed in both places.** If you changed a skill on a linked computer and it then changes on the Mac too,
  nothing is overwritten: that computer's page shows both and asks which to keep, this computer's or the
  Mac's.
- **Per skill.** On the Mac each skill has its own switch (off: it is no longer sent, and leaves the linked
  computers). On a linked computer each skill has its own switch too (off: only on this computer; that copy
  stops following the Mac until you switch it back), and **Take skills from** the Mac switches all of them.
- **Undo.** Whenever a skill here is replaced or taken away by a change from the Mac, by your choice or by a
  switch, the version it replaced is kept for 30 days (of your own text the newest 40 per skill, of the Mac's
  earlier versions the newest 10 per skill, 400 kept versions in all on one computer) in the plugin's data folder on that
  computer. **Earlier versions** on the page puts one back, as does `hermes twin fleet skills restore`.
- **Switching it off** on the Mac stops sending. Linked computers keep the skills they have, as they are.
- A linked computer on an older Grok Bot Twin keeps following names and descriptions as before; it gets skills
  once it is updated.

```sh
hermes twin fleet skills                         # what is sent or received here, skill by skill
hermes twin fleet skills on                      # on the Mac: send skills (off: stop)
hermes twin fleet skills set packing-list off    # the Mac: stop sending this one; a linked computer: only on this computer
hermes twin fleet skills keep packing-list mine  # after a change in both places: mine or theirs
hermes twin fleet skills restore packing-list ID # put an earlier version back (IDs are in the list)
```

## Settings that follow a twin

A twin's settings follow it from the Mac with the Grok app to your linked computers, over the same signed link.
On the Twins page, under your computers, one line says what follows; **Change** opens one switch per item:

| Item | What follows | Starts |
|---|---|---|
| **Tools** | Which tool sets the twin uses, and the names of its connectors. A connector's set-up and sign-in stay on each computer. | on |
| **Preferences** | Personality, reply style, turn limit, reasoning effort, time zone and the two memory switches. | on |
| **Persona** | The twin's instructions (its prompt file), after the same password-and-key check as everywhere else; a line that looks like a sign-in is left out whole. The check can miss things: do not keep passwords in instructions. | on |
| **Model** | Which model and provider the twin uses, as names only. | **off: each computer keeps its own model** |

- Each computer has its own switches. On the Mac a switch says what is sent; on a linked computer, what is
  taken. An item follows only where both are on. Switching one off takes nothing away: the twin keeps what it
  has and stops following changes to that item. The model is the one exception: switched off again on either
  computer, each twin gets back the model that computer had for it.
- **What is chosen is a list of named settings, not "everything except secrets".** A setting that is not on the
  list does not travel, whatever it holds. Never copied: passwords, keys, tokens, sign-ins, the `.env` file, how
  a connector is set up, and approval or security settings.
- **Set up on this computer.** When a twin's settings name a connector, a tool set or a provider that this
  computer does not have, the page says so under that twin and names it. Add it there; the rest already follows.
  A twin whose model on this computer has an address or key of its own keeps that model, and the page says so.
- **Put back.** Before settings arrive, the plugin notes what the twin's Hermes bot had (the last ten times,
  per twin). **Put back** returns the bot to that, and its settings then stay as they are on this computer
  until they next change on the Mac; **Follow My other Mac again** (the button names your Mac) ends that at once.
- If a twin's settings file on the Mac cannot be read for a moment (saved half-way), nothing changes on your
  other computers until it can.
- A linked computer on an older Grok Bot Twin keeps following names and descriptions as before; it is told
  nothing about settings until it is updated. A Mac on an older one: the linked computer says it needs a newer
  Grok Bot Twin there before settings can follow.

In the terminal: `hermes twin fleet settings`, `hermes twin fleet settings model on` (or `off`, for `tools`,
`preferences`, `persona`, `model`), `hermes twin fleet settings restore "Test Scout"`,
`hermes twin fleet settings follow "Test Scout"`.

## Routines on your other computers

A routine is something a twin does on a schedule. With **Routines** switched on (on the Twins page of the
Mac that shares, or `hermes twin routines on`; off until you do), each twin's routines copy to every linked
computer, and each routine **runs on one computer only**: the Mac, until you say otherwise. The others keep
their copy paused and say where it runs ("runs on My other Mac").

- **Move it with one press.** On the Mac: "Run on Laptop" (`hermes twin routines move "Daily summary"
  Laptop`). On a linked computer: "Run here", or "Run on My other Mac" to send it back. The computer that ran it
  pauses it first and says so; only then does the new one switch it on. It is never switched on on two
  computers. If the computer that runs it is off, the routine waits and the page says for which computer
  ("moving to Laptop: waiting for My other Mac to stop it").
- **A computer runs routines only if you let it.** On each linked computer, "Routines may run on this
  computer" is off until you switch it on there (`hermes twin routines run-here on`). Until then that
  computer holds paused copies and nothing more.
- **What copies:** each routine's name, schedule and prompt, the names of the skills it uses, which computer
  runs it, and whether you paused it. The prompt goes through the same password-and-key check as everything
  else. **What never copies:** routines that run a script and one-off reminders (they stay on their own
  computer), what a routine produced and when it ran, where it delivers, its model, passwords and keys.
- **Pause or resume a routine in Hermes on the computer where it runs**, as you always did. To change its
  prompt or schedule, change it on the Mac; a copy edited elsewhere is put back in line, and the version it
  had is kept for 30 days under "Routines as they were before" (`hermes twin routines restore`).
- **Undo.** Switch Routines off on the Mac: the copies leave the other computers and every routine runs on
  the Mac again. Switch it off on one linked computer: only that computer's copies leave. Unlinking a
  computer pauses what it ran before its twins leave.
- A linked computer whose Grok Bot Twin is older keeps following your twins as before and simply gets no
  routines; a newer one linked to an older Mac says "My other Mac has an older Grok Bot Twin, so routines do not
  copy yet".

`hermes twin fleet` lists what syncs, item by item, with every routine and where it runs.

Good to know: schedules are read in each computer's own time zone unless the routine names one. A computer
you remove from the Mac's list while it is switched off cannot confirm that it stopped a routine; the Mac
then keeps that routine waiting and offers "Start it here anyway", which says what that means. A copy you
resume by hand on a computer that does not run it is paused again within half a minute.

## When the Mac is off, asleep or away

The twins on your other computers stay as they are and still open and work. Their Twins page says since when
("My other Mac offline since 14:02"), and there is nothing for you to do: the twins catch up by themselves when the
Mac is back. In our runs, where Hermes on the Mac was stopped and started again, the other computers noticed
within about 2 seconds (7 s on Windows, where starting the command is part of that time) and caught up 8 to 14
seconds after it returned. A Mac whose Hermes was frozen, not stopped, was noticed after 55 s; a Mac that falls
asleep was not timed and can take as long. Sharing and following run while Hermes is open; on the Mac that is
also when a change in Grok is noticed at all, so keep Hermes open there.

## Undo everything

Each way out says what stays and what goes before you confirm, and each is undone by doing the opposite.

| You want | Do this | What happens | The way back |
|---|---|---|---|
| One twin gone everywhere | On the Mac: the twin's **…** menu, **Stop twinning…** | Its copies leave your other computers, within a second where they are on, otherwise the next time they are. Each is kept there for 30 days, chats included. The Hermes bot on the Mac stays. | Twin the bot again |
| One computer out | On that computer: its **…** menu in the list of computers, **Unlink this computer…**. Or on the Mac: that computer's **…** menu, **Unlink NAME…** | Only that computer's twins leave, within about 5 seconds, kept for 30 days, and its switch for requests goes back to off. The Mac and every other computer are untouched. Done from the Mac, that computer can fetch nothing more from that moment. | Link it again with a new code within 30 days: the same twins are back with their chats |
| Keep one of those as a plain bot | On that computer: **Hermes bots kept for 30 days**, **Keep this bot** | It is an ordinary Hermes bot again and follows nothing | None needed |
| Sharing off for now | On the Mac: switch **Share my twins with my other computers** off, **Stop sharing** | Nothing is removed anywhere. Your other computers keep their twins and say that sharing is off. | Switch it on again; no new code is needed |
| All of it gone from a computer | **Remove Grok Bot Twin…** at the bottom of its Twins page (see [Uninstall](GUIDE.md#uninstall)) | On a linked computer the link ends and its twins stay as ordinary Hermes bots; bots still listed under **Hermes bots kept for 30 days** are deleted with the rest, so press **Keep this bot** first on any you want. On the Mac sharing stops; the others keep their twins and say that Grok Bot Twin was removed there. | This cannot be undone; you can set up again |

## If something goes wrong

| The page says | Do this |
|---|---|
| "That is not a link code. A link code looks like ABCD-EFGH-JKMN." | Copy the code from the Twins page on your Mac. |
| "That code does not match the one on your Mac. Nothing was changed." | Correct it and press **Link** again. |
| "That code has expired. A code lasts 10 minutes.", "That code was already used. Each code links one computer." or "Too many wrong codes, so that code no longer works." | On your Mac press **New code** and type the new one. |
| "That code is from this computer." | Type it on your other computer. |
| "Hermes could not reach your Mac." | Check that the Mac is awake with sharing on, on the same network as this computer, then press **Try again**. If the sheet offers your Mac by name, pick it. If the Mac is on and sharing and it still says so, restart Hermes on this computer. |
| On the Mac, the switch goes back to off: "Hermes found no home network to share on." | Put the Mac on your home network or start Tailscale, then switch sharing on again. |
| On the Mac: "Another program on this computer is using the place Hermes shares from." | Quit the program it names. Hermes tries again by itself every minute; **Try again** only hurries it. |
| "My other Mac offline since 14:02" | Nothing. Twins here still work, and catch up by themselves; **Check My other Mac now** in a twin's **…** menu only hurries it. If the Mac is on with Hermes open and it stays this way, the Mac's home-network address may have changed: unlink and link again, and the twins come back with their chats. Tailscale addresses do not change. |
| "Sharing was switched off on My other Mac at 14:02" | Switch sharing on again on the Mac. No new code is needed; this computer follows again within about a minute. |
| "Grok Bot Twin was removed on My other Mac, so your twins here no longer follow it." | Nothing has to be done: the twins here stay and work. To follow the Mac again after it is set up again, unlink this computer and link it again with a new code. To keep the twins here as plain Hermes bots for good, press **Remove Grok Bot Twin…** here too. |
| "My other Mac no longer answers as a computer linked to this one." | If Grok Bot Twin was set up again on the Mac, unlink this computer and link it again with a new code. If it was not, check that the Mac is on your network with Hermes open; nothing changes here meanwhile. |
| "My other Mac stopped sharing with this computer" | It was unlinked from the Mac. Its twins are kept for 30 days: **Link this computer** with a new code brings them back. |
| "My other Mac and this computer run versions of Grok Bot Twin that cannot work together." | Update Grok Bot Twin on the computer with the older version (Capabilities → Plugins); when the message names a computer, that one. The twins stay as they are meanwhile. |
| "Recipe Planner could not be made on this computer" | Nothing: Hermes tries again by itself, and your other twins are in sync. |
| "A Hermes bot called recipe-planner was already here, so this twin is recipe-planner-2. The other one was left as it is." | Nothing. Your own bot is untouched. |
| `'twin' is not a hermes command` | The plugin is installed but switched off: `hermes plugins enable grok-bot-twin`. |
| "Grok Bot Twin was installed from an administrator window…" (Windows) | Windows keeps what an administrator window installs away from your ordinary programs, Hermes Desktop included. Run the one `icacls` line it prints in that administrator window; next time install from an ordinary one. |

## In the terminal

Everything above has a command, and these are what the timed tests used:

```sh
hermes twin share on             # on the Mac with the Grok app: start sharing and print a link code
hermes twin share code           # a new link code
hermes twin link 7KQ4-M2XD-P9RA  # on another computer: link it
hermes twin fleet                # which computers hold which twins, and how fresh they are
hermes twin fleet name Laptop   # the name your other computers show for this one
hermes twin fleet settings       # which settings follow your twins; `... settings model on` switches one
hermes twin unlink               # this computer leaves; on the Mac: hermes twin fleet remove NAME
hermes twin share off            # on the Mac: stop sharing; nothing is removed anywhere
```

**Up to date while Hermes is closed (optional, terminal only).** Twins follow while Hermes is open. On a linked
computer, `hermes twin background on` sets up one small background helper for your account so they also follow
while Hermes is closed: a launchd agent on macOS (macOS shows its own "background item added" notice once), a
systemd user unit on Linux (after logout only if "lingering" is on for your account), a scheduled task of your
own account on Windows (no administrator rights; set to start when you sign in). It runs as you,
never as root, and on a linked computer it waits for no calls. Idle for two minutes it used 0.00 s of processor time on Linux and
0.04 s on a Mac (on Windows 0.1 s in one minute), and it holds about 95 MB of memory (99 MB on the Mac, 98 MB
on Windows). After a crash it was running again about 30 s later on Linux and macOS. `hermes twin background`
says whether it is on. `hermes twin background off` stops it and deletes what it added; unlinking on that
computer or removing Grok Bot Twin there does the same (seen on all three systems). A computer that was
unlinked from the Mac keeps its helper waiting until you turn it off. On Windows switching it on can take up
to a minute: it waits until the helper has really started.
After you update Grok Bot Twin the helper notices within about a minute and starts again by itself with the new
version (on Windows at its task's next look, within 15 minutes, or when you next open Hermes).
The helper does what an open Hermes does: with requests switched on it also works on requests. The command is
meant for linked computers. On the Mac that shares it is not refused, and the helper then keeps sharing going
while Hermes is closed, so there it does listen, on the one address described next; that use was not tested.

**What the network sees (for developers).** The Mac that shares listens on one address only: its Tailscale
address when Tailscale is running, else its private home-network address. Never on every interface, never on
a public address; port 18777. It keeps the address it first shared on for as long as that address works, so a
Mac that started on the home network stays there after Tailscale is started: set "Address to share on" to move
it, and link your computers again. Idle, sharing used 0.12 s of processor time in two minutes. Your other
computers only make outgoing connections to that address. No key and no code is ever sent between your computers:
linking is an exchange in which
both computers prove they know the code without sending it, and from then on every request and every answer
is signed with a key the two worked out at that moment. Another device that turns up at the Mac's address can
neither read your list nor make a linked computer change or remove anything; the most it achieves is that the
linked computer says the Mac no longer answers. A recording of the moment of
linking gives away neither the key nor the code, someone without the code gets five guesses in 16 million, and
a recorded request sent again is refused, also after Hermes on the Mac was started again. Signing is not
hiding: see the table above for what a plain home network can read. Details: [DISCLOSURE.md](DISCLOSURE.md).

## A twin's memory on your other computers

Off for every twin. A Hermes bot keeps what it has learned in two small files of notes, `MEMORY.md` and
`USER.md` (what it knows about you). They stay on the computer they were written on unless you switch memory on
for a twin: once on the Mac that shares, and once on each linked computer that should take part. Under
**Twin memory** on the Twins page each twin has its own switch; in the terminal it is
`hermes twin fleet memory "Test Scout" on`.

**What follows.** The notes in those two files of that twin. A note written on any of the computers arrives on
the others; a note removed on one leaves the others. On a running Hermes or with the background helper that
takes about 15 seconds either way (in the test on four computers: 1 to 14 seconds).
A chat that is already open sees new notes when you start the next chat (Hermes reads memory when a chat begins).

**What never follows.** A note that looks like it holds a password, key, token, door code, bank or card number;
a note that reads like an instruction to the model rather than a note; a note with characters that cannot be
seen or with look-alike letters. Such a note stays on the computer it is on, and the page says how many there
are, never what they say. The check is strict on purpose, so now and then an ordinary note stays behind too. It
can also miss things: do not keep passwords in a bot's memory. Switching memory on moves notes only: chats never
travel, and skills and settings have their own switches.

**Which computers.** The Mac that shares decides. When you switch a twin on there, the computers linked at that
moment take part, and the page lists them under the twin, each with its own switch, so you can keep one out (a
computer you share with someone, say). A computer you link later, or link anew, is out until you let it in
there; in the terminal `hermes twin fleet memory "Test Scout" with "Laptop"` (or `without`). A computer that
is out is answered exactly as if memory were off. And each of those computers takes part only once you switch
the twin on there too.

**How it travels.** Between your own linked computers only, on the link they already have, to no cloud. Unlike
the list of twins, memory is encrypted from one computer to the other (with a key the two made when you linked
them, which never travelled), so it is not readable on a plain home network either. Someone listening can see
that two of your computers talked about memory and roughly how much, not which twin or what. The Mac that
shares is the meeting point: it holds the merged notes in that twin's own memory files, like every computer
that takes part.

**When two computers changed the same note** before they met, both versions are kept and the page shows them
side by side under that twin with **Keep this one** and **Keep both**. Nothing is thrown away to settle it for
you.

**A linked computer cannot clear or churn a twin's memory everywhere.** Removing a few notes travels. Within a
day one linked computer can remove five notes of a file, or half of it if that is more; beyond that the notes
stay on the Mac and come back. And the Mac takes 24 rounds of changes a day from one computer; further ones
wait on that computer until the next day (it still receives). Clear a memory on the Mac that shares, and the
others follow.

**Undo.** Before the plugin changes a memory file it keeps the file as it was, on that computer only (the
newest 30 and the first of each day for 30 days, and the memory from before you first switched it on for as
long as the twin is there).
**Earlier versions** under the twin lists them; **Put back** puts one back and switches memory off for that
twin on this computer, so your other computers keep theirs.

**Forget on this computer.** One press: the notes that came from your other computers leave that twin's memory
on this computer, the earlier versions kept here are deleted, and the switch goes off. Notes written on this
computer stay, and nothing changes on any other computer. (Hermes' own backups of a memory file, if it made
any, are Hermes' and are not touched.)

Switching memory off anywhere changes no note: each computer keeps what it has. Switching it on again joins what
the computers hold, so a note removed on one of them in between comes back from the others. Unlinking a
computer switches it off there, and a new link starts with every switch off; the earlier versions kept for a
twin that left a computer are deleted after 30 days, or at once with Forget. A computer on an older Grok Bot Twin keeps following
names and descriptions as before and simply takes no part in memory.

## Ask a twin on another computer

Once your computers are linked, you can ask a twin's copy on another one to do something there. In a Hermes
chat on this computer, type:

```
/twin ask Laptop: how much disk space is free?
```

Only you ask: the model in a chat has no tool for it, and a command that a chat starts cannot ask either.

The copy of that twin on Laptop gets this as an ordinary chat message, answers with what it finds there, and
the answer comes back into your chat under a line that says where it was done and how long it took, like this:

```
done on Laptop - 19 s
Windows, 50 GB free on C:
```

**It is off until you switch it on, computer by computer.** On the computer that should take requests, open
**Twins** and switch on **Let my other computers ask this one to do things**, under the list of computers
(terminal: `hermes twin requests on`). The same switch ends it at once, also for a request that is running
(on Windows a program that the running request had already started goes on to its own end; in our test that
was two minutes). Asking needs no switch; being asked does.

Switch it on only on a computer you would let your other computers use as you do: a request can do what a
chat with that bot can do there. Requests are signed, so nobody else on your network can send or repeat one.
On Tailscale they are also encrypted; on a plain home network what is asked and what comes back can be read by
others on that network (the page says so).

What a request can and cannot do:

- It is one chat turn of the twin's own Hermes bot on that computer, with that computer's tools and that
  computer's approval rules. The plugin changes none of them, so what Hermes there lets a chat do without
  asking, a request can do too.
- A step that needs your approval waits for you **on the computer where it would run**. Your chat says
  `waiting for approval on Laptop`. On Laptop the Twins page shows the request and the step first, with two
  answers, **Do it once** and **Don't** (terminal: `hermes twin requests`). No other computer can answer
  for it. If nobody answers within
  5 minutes the step is not done, and the answer says
  `done on Laptop - 5 min; a step was not approved there`.
- Only your linked computers can ask, over the link that carries the twin list. Requests pass through the Mac
  with the Grok app, so it has to be on.
- What travels is the text you typed (2,000 characters at most) and the twin's answer. Anything that looks
  like a password or key is masked on the way out and on the way back.
- Every request is in the log on both computers (Twins page, or `hermes twin requests`): who asked, what, and
  how it ended.

**When a step needs approval: two choices, made on the computer that runs the request.** They are under
the switch on that computer's Twins page (terminal: `hermes twin requests mode`). No other computer and no
request can change the choice.

| Choice | What happens to a step that needs approval |
|---|---|
| **Ask on this computer** (the default) | It waits until you answer on that computer. Nothing about the step leaves it. |
| **Only safe requests** | It is refused at once and the answer says why. Requests that only read still work. |

In our tests an answer took 1 to 8 s from a Mac or a Linux computer and 12 to 27 s from Windows, with a script
standing in for the model. With a real model an answer took 12 to 34 s, and 53 s for the first one after Hermes
had been restarted on the asked computer.

| Your chat says | Do this |
|---|---|
| `not sent: requests are switched off on Laptop` | On Laptop, open **Twins** and switch it on. |
| `waiting for approval on Laptop` | Answer on Laptop: its Twins page shows the step and the two answers. |
| `done on Laptop - 3 s; a step was refused: Laptop takes only safe requests` | Laptop is set to "Only safe requests". Change it on Laptop if you want such steps to wait for you. |
| `not done: Laptop could not be reached` | Check that Laptop and the Mac with the Grok app are on, with Hermes open on both. |
| `not done on Laptop` and "The twin on Laptop could not answer." | Open that twin's chat on Laptop to see why; for example, it may have no working model. |
| "The clocks of your computers differ by more than 15 minutes" | Set the date and time to automatic on both computers. |
| "Too many requests to Laptop in a short time." | Try again in a minute. The limits are 6 a minute and 40 an hour. |

In the terminal: `hermes twin ask Laptop "how much disk space is free?"` (add `--twin "Acme Support"` when you
have several twins), from a terminal of your own (not one a chat started).

## Known limitations

- The plugin takes an address that is in Tailscale's address range for a Tailscale address. On
  the rare network that uses such addresses without Tailscale, the page would call the link encrypted when it
  is not.
- Settings follow one way, from the Mac with the Grok app. A setting changed by hand on a linked computer is
  replaced the next time that item changes on the Mac (Put back, or the item's switch, keeps yours).
- The persona that follows is text a linked computer's twin will act on. It comes only from a computer you
  linked, signed; on a plain home network it is readable like names and descriptions. Switch **Persona** off
  on a computer that should keep its own.
- Between computers, sharing was tested on macOS, one Linux distribution and Windows 11, with the terminal commands; the
  Twins page was clicked through on a Mac only. Not tested: other Linux distributions, a Mac that falls asleep while sharing, the
  background helper starting by itself when you sign in to Windows, Windows on ARM, and a Windows account that
  is not an administrator.
