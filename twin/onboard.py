"""`hermes twin setup --check | --plan | --yes`: one setup command, whoever runs it (you, a coding agent, Hermes).

The plan is built from facts that cost nothing to read: files are stat()ed, the plugin's own records are read,
botport is never run and Grok's bot list is never asked for. `--check` says where this computer stands and
`--plan` says exactly what a run with the same options would do; neither changes anything. A run does only the
steps that are left, so running it again is safe.

Nothing here prints the name of a bot the caller did not type: twins are counted, never listed, so the answer can
be shown in a chat with an agent. The link code somebody typed is never printed back.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from . import safe
from .engine import EngineError
from .model import plural

PAGE = "the Twins page in Hermes Desktop"
# what a step is, in one word (also the `state` in --json)
DONE, WILL, YOU, OPTIONAL, WAITS, NOT_NEEDED = "done", "will", "you", "optional", "waits", "not_needed"
MARK = {DONE: "✓", WILL: "→", YOU: "!", OPTIONAL: "–", WAITS: "·", NOT_NEEDED: "–"}
NOTHING_CHANGED = "Nothing was changed."
RUN_AGAIN = "Safe to run again: a step that is done is skipped."

BOTPORT_VERIFY_DOES = (
    "Compares botport's SHA-256 with the list of release files that ships with this plugin (no network).",
    "Runs botport once to read its version.",
    "Records the result in Hermes' plugin data folder (plugin-data/grok-bot-twin).",
)
BOTPORT_TAKE_DOES = (
    "Reads that one file and compares its SHA-256 with the list of release files that ships with this plugin.",
    "On a match, keeps a copy in Hermes' plugin data folder; the download stays where it is. A file that does "
    "not match is not kept and never run.",
)
TWIN_DOES = (
    "Asks botport for your bot list (it reads the Grok app's local files; nothing is sent anywhere).",
    "Makes one Hermes bot (a Hermes profile) per twin, with the bot's name and description.",
    "Nothing changes in Grok. Instructions are not copied: you paste them on the Twins page.",
)
SHARE_DOES = (
    "Switches on \"Share my twins with my other computers\".",
    "While Hermes is open, this computer then listens on ONE private address of its own (Tailscale, else your "
    "home network), never a public one.",
    "Prints a link code: one computer, 10 minutes.",
)
LINK_DOES = (
    "Makes one outgoing connection to the computer the code names (one of yours); the code itself is never sent.",
    "Keeps a key for that computer in Hermes' plugin data folder.",
    "Makes one Hermes bot (a Hermes profile) on this computer for each twin of that computer.",
)
SKILL_SYNC = ("Skill sync (optional): on the Twins page, a twin's … menu, Skill sync…. It needs a small sync bot "
              "in your own Grok account, and every run of that bot spends Grok credit.")
BACKGROUND = "Following while Hermes is closed (optional): hermes twin background on"


def _step(sid: str, state: str, title: str, does: Any = (), how: Optional[str] = None, ask: bool = False) -> dict:
    return {"id": sid, "state": state, "title": title, "does": list(does), "how": how, "ask": bool(ask)}


def _wants(a: Any) -> dict:
    """What the caller asked for, cleaned: bots by name, every bot, sharing, a link code, a file."""
    bots = []
    for raw in getattr(a, "bot", None) or []:
        name = safe.clean_name(raw, 100) if isinstance(raw, str) else ""
        if name and name.lower() not in [b.lower() for b in bots]:
            bots.append(name)
    code = getattr(a, "link", None)
    return {"bots": bots[:50], "all": bool(getattr(a, "all", False)), "share": bool(getattr(a, "share", False)),
            "link": code if isinstance(code, str) and code.strip() else None,
            "file": getattr(a, "helper", None) or None,
            "unattested": bool(getattr(a, "allow_unattested", False))}


def check_usage(svc: Any, a: Any) -> None:
    """Options that cannot go together, said before anything is looked at or done."""
    w = _wants(a)
    if getattr(a, "check", False) and getattr(a, "plan", False):
        raise EngineError("usage", "Give --check or --plan, not both.",
                          "--check says where this computer stands; --plan says what a run would do.")
    if w["link"] and (w["bots"] or w["all"] or w["share"] or w["file"]):
        raise EngineError("usage", "--link is for a computer that gets its twins from another one; it does not go "
                                   "with --bot, --all, --share or --file.",
                          "On the computer with the Grok app: hermes twin setup --share. On the other one: "
                          "hermes twin setup --link CODE")
    if w["bots"] and w["all"]:
        raise EngineError("usage", "Give --bot NAME or --all, not both.")


def _fleet(svc: Any) -> dict:
    try:
        f = svc.fleet()
        return f if isinstance(f, dict) else {}
    except Exception:
        return {}


def _twin_facts(svc: Any) -> tuple:
    """(real twins, their names in lower case when remembered, how many still wait for their instructions, how
    many for a model). From the plugin's own records only: botport is not run."""
    try:
        ids = [i for i in svc.pair_ids() if i not in svc.demo.pair_ids()]
    except Exception:
        ids = []
    names, no_text, no_model = set(), 0, 0
    try:
        st = svc.cached_status(compute=False) or {}
    except Exception:
        st = {}
    for p in st.get("pairs") or []:
        if not isinstance(p, dict) or p.get("pair_id") not in ids:
            continue
        grok = p.get("grok") if isinstance(p.get("grok"), dict) else {}
        if isinstance(grok.get("name"), str):
            names.add(grok["name"].lower())
        left = p.get("setup_left") if isinstance(p.get("setup_left"), list) else []
        no_text += 1 if "instructions" in left else 0
        no_model += 1 if "model" in left else 0
    return ids, names, no_text, no_model


def has_twins(svc: Any) -> bool:
    return bool(_twin_facts(svc)[0])


def build(svc: Any, a: Any) -> dict:
    """The plan for this computer: every step with its state, what exactly it does, and who does it."""
    w = _wants(a)
    s = svc.setup_state()
    f = _fleet(svc)
    role = f.get("role") or "none"
    me = safe.clean_name((f.get("this_machine") or {}).get("name"), 60) or "this computer"
    app = bool(s["grok_app"]["found"])
    twins, known_names, no_text, no_model = _twin_facts(svc)
    # which kind of computer this is: one that gets its twins over a link, or the one with the Grok app
    linked = role == "follower"
    follows = linked or bool(w["link"]) or (not app and not twins and not s["helper"]["found"] and not w["file"]
                                             and role != "source")
    steps = [_step("plugin", DONE, "Grok Bot Twin is installed and switched on in Hermes.")]
    if follows:
        _plan_linked(steps, w, f, linked, app)
    else:
        _plan_grok(steps, w, s, f, app, twins, known_names, no_text, no_model)
    required = ("link",) if follows else ("grok_app", "botport", "twins", "finish")
    left = [st for st in steps if st["id"] in required and st["state"] != DONE]
    return {"computer": me, "kind": "linked" if follows else "grok", "complete": not left, "steps": steps,
            "next": _next(steps, required)}


def _plan_linked(steps: list, w: dict, f: dict, linked: bool, app: bool) -> None:
    steps.append(_step("grok_app", NOT_NEEDED,
                       "This computer gets its twins from the computer with your Grok bots, over a link. It needs "
                       "no botport." if app else
                       "The Grok app is not on this computer, so its twins come from the computer that has it, "
                       "over a link. It needs no botport."))
    if linked:
        src = f.get("source") or {}
        name = safe.clean_name(src.get("name"), 60) or "your other computer"
        n = len(f.get("twins") or [])
        state = {"in_sync": "in sync", "syncing": "getting its first twins", "offline": "not answering right now",
                 "sharing_off": "sharing is switched off there"}.get(src.get("state"), "linked")
        steps.append(_step("link", DONE, f"Linked to {name}: {state}, {plural(n, 'twin')} here."))
        if w["link"]:
            steps[-1]["how"] = ("The code you gave was not used. To link this computer to another one, unlink it "
                                "first: hermes twin unlink")
    elif w["link"]:
        steps.append(_step("link", WILL, "Link this computer with the code you gave.", LINK_DOES, ask=True))
    else:
        steps.append(_step("link", YOU, "This computer is not linked yet.",
                           how="On the computer with your Grok bots run hermes twin setup --share (or switch on "
                               "\"Share my twins with my other computers\" on its Twins page): it shows a link "
                               "code. Then here: hermes twin setup --link CODE"))
    steps.append(_step("background", OPTIONAL, BACKGROUND))


def _plan_grok(steps: list, w: dict, s: dict, f: dict, app: bool, twins: list, known: set, no_text: int,
               no_model: int) -> None:
    from .engine import release_target
    tag = s["release_tag"]
    if app:
        steps.append(_step("grok_app", DONE, "The Grok desktop app is on this computer."))
    else:
        steps.append(_step("grok_app", YOU, "The Grok desktop app is not on this computer.",
                           how="Install the Grok desktop app and sign in; setup notices by itself. (A computer "
                               "without the Grok app links to the one that has it: hermes twin setup --link CODE)"))
    # ---- botport ---------------------------------------------------------------------------------------------
    found, checked = bool(s["helper"]["found"]), bool(s["check"]["done"])
    download = s.get("download") if isinstance(s.get("download"), dict) else None
    archive = (download or {}).get("file_name") or f"botport-{release_target()}.tar.gz"
    in_downloads = ((download or {}).get("found") or {}).get("name") if download else None
    if w["file"]:
        steps.append(_step("botport", WILL, "Take the botport file you named and verify it "
                                            f"(release {tag}).", BOTPORT_TAKE_DOES + BOTPORT_VERIFY_DOES[1:]))
    elif found and checked and not w["unattested"]:
        version = safe.clean_name(s["helper"].get("version"), 30)
        steps.append(_step("botport", DONE, f"botport {version} is verified.".replace("  ", " ")
                           if not s["check"].get("unattested") else
                           "botport is in use WITHOUT being verified (a build of your own)."))
    elif found:
        steps.append(_step("botport", WILL, f"Verify botport (release {tag}).", BOTPORT_VERIFY_DOES))
    elif in_downloads:
        steps.append(_step("botport", WILL, f"Use the botport download found in your Downloads folder "
                                            f"({safe.clean_name(in_downloads, 80)}) and verify it (release {tag}).",
                           BOTPORT_TAKE_DOES + BOTPORT_VERIFY_DOES[1:], ask=True))
        steps[-1]["from_downloads"] = True
    else:
        steps.append(_step("botport", YOU, "botport, a small free app that reads your bot list from Grok, is not "
                                           "on this computer yet.",
                           how=f"Download {archive} from {s['download_url']} with your browser (this plugin never "
                               "downloads it), then run hermes twin setup again: it finds the file in Downloads. "
                               f"Or press Connect my Grok bots on {PAGE}."))
    ready = steps[-1]["state"] in (DONE, WILL) and app
    # ---- twins -----------------------------------------------------------------------------------------------
    have = f"{plural(len(twins), 'twin')} on this computer." if twins else "No twins yet."
    asked = [b for b in w["bots"] if b.lower() not in known]
    if (w["bots"] and not asked):
        steps.append(_step("twins", DONE, f"{have} Every bot you named already has a twin."))
    elif w["bots"] or w["all"]:
        what = ("Twin every Grok bot that has no twin yet (names are not printed)." if w["all"]
                else "Twin " + ", ".join(f'"{b}"' for b in asked) + ".")
        if ready:
            steps.append(_step("twins", WILL, what, TWIN_DOES, ask=True))
        else:
            steps.append(_step("twins", WAITS, f"{what} This waits until botport is verified."))
    elif twins:
        steps.append(_step("twins", DONE, have))
    else:
        steps.append(_step("twins", YOU if ready else WAITS, have,
                           how=f"Choose your bots on {PAGE} (Connect my Grok bots), or name them: hermes twin setup "
                               "--bot \"NAME\" (hermes twin list-grok shows the names; --all takes every bot)."))
    # ---- what only the owner can finish ------------------------------------------------------------------------
    if no_text or no_model:
        waits = lambda n: f"{plural(n, 'twin')} still {'waits' if n == 1 else 'wait'}"  # noqa: E731
        title = " and ".join(x for x in (
            f"{waits(no_text)} for {'its' if no_text == 1 else 'their'} instructions" if no_text else "",
            f"{waits(no_model)} for a model" if no_model else "") if x) + "."
        how = " ".join(x for x in (
            f"On {PAGE}: Paste its instructions (copied from the Grok app; the Grok app does not keep them on this "
            "computer)." if no_text else "",
            f"On {PAGE}: Choose a model (a new twin takes the model of your other Hermes chats; this computer has "
            "none yet)." if no_model else "",
            "Nobody but you can do this.") if x)
        steps.append(_step("finish", YOU, title, how=how))
    elif twins:
        steps.append(_step("finish", DONE, "No twin waits for its instructions or for a model, as far as the last "
                                           "check knows (hermes twin shows each one)."))
    # ---- sharing ---------------------------------------------------------------------------------------------
    share = f.get("share") if isinstance(f.get("share"), dict) else {}
    if share.get("on"):
        n = len([m for m in f.get("machines") or [] if isinstance(m, dict) and not m.get("this")])
        steps.append(_step("share", DONE, "Sharing with your other computers is on; "
                           + (f"{plural(n, 'computer')} linked." if n else "no other computer is linked yet.")
                           + " (A code for one more computer: hermes twin share code)"))
    elif w["share"]:
        if twins or _state_of(steps, "twins") == WILL:
            steps.append(_step("share", WILL, "Share your twins with your other computers.", SHARE_DOES, ask=True))
        else:
            steps.append(_step("share", WAITS, "Share your twins with your other computers. This waits until "
                                               "there is a twin to share."))
    else:
        steps.append(_step("share", OPTIONAL, "Your twins on your other computers (optional): hermes twin setup "
                                              "--share, then on each other computer hermes twin setup --link CODE"))
    steps.append(_step("skill_sync", OPTIONAL, SKILL_SYNC))


def _state_of(steps: list, sid: str) -> Optional[str]:
    for st in steps:
        if st["id"] == sid:
            return st["state"]
    return None


def _next(steps: list, required: tuple) -> str:
    """The one next thing, in a sentence."""
    if any(st["state"] == WILL for st in steps):
        return "Run hermes twin setup with the same options (add --yes to go ahead without being asked)."
    if any(st["id"] in required and st["state"] in (YOU, WAITS) for st in steps):
        return "The first line marked ! is yours to do. Then run hermes twin setup --check again."
    return "Nothing is left that setup can do. hermes twin shows your twins."


# ---- saying it ---------------------------------------------------------------------------------------------------
def lines(plan: dict, mode: str) -> list:
    """The plan in words. mode: 'check' (where this computer stands), 'plan' (what a run would do), 'run' (what
    this run is about to do: only the steps it does), 'idle' (a run that had nothing to do: where things stand)."""
    kind = "it has the Grok app" if plan["kind"] == "grok" else "it gets its twins over a link"
    out = []
    if mode == "check":
        out.append(f"Grok Bot Twin setup on {plan['computer']} ({kind}). --check changes nothing.")
    elif mode == "idle":  # a plain run that found nothing it can do by itself
        out.append(f"Grok Bot Twin setup on {plan['computer']} ({kind}). {NOTHING_CHANGED}")
    elif mode == "plan":
        out.append(f"Grok Bot Twin setup on {plan['computer']} ({kind}). --plan changes nothing; this is what a "
                   "run with the same options does.")
    else:
        out.append(f"Grok Bot Twin setup on {plan['computer']} will do this:")
    for st in plan["steps"]:
        if mode == "run" and st["state"] != WILL:
            continue
        says_what_a_run_does = mode in ("plan", "run")
        word = {WILL: "Will do: " if says_what_a_run_does else "Not done yet: ", YOU: "Yours to do: ",
                WAITS: "Waits: "}.get(st["state"], "")
        out.append(f" {MARK[st['state']]} {word}{st['title']}")
        if st["state"] == WILL and says_what_a_run_does:
            out.extend(f"     {d}" for d in st["does"])
        if st["how"] and st["state"] != WILL:
            out.append(f"     {st['how']}")
    if mode != "run":
        if plan["complete"] and not any(st["state"] == WILL for st in plan["steps"]):
            out.append("Setup is complete on this computer.")
        out.append(f"Next: {plan['next']}")
    return out


def public(plan: dict) -> dict:
    """The plan for --json: the same facts as the lines, nothing more."""
    return {"computer": plan["computer"], "kind": plan["kind"], "complete": plan["complete"],
            "steps": [{k: st[k] for k in ("id", "state", "title", "does", "how", "ask")} for st in plan["steps"]],
            "next": plan["next"]}


# ---- doing it ----------------------------------------------------------------------------------------------------
def twin_bots(svc: Any, bots: list, every: bool, out: Callable[[str], None], err: Callable[[str], None]) -> int:
    """Give the named bots (or every bot) a twin. A bot that already has one is left as it is. Names the caller
    did not type are not printed."""
    data = svc.grok_list()
    real = [b for b in data.get("bots") or [] if isinstance(b, dict) and not b.get("demo")]
    if every:
        if not real:
            err("No bots were found in your Grok app. Run hermes twin doctor to see why.")
            return 1
        chosen = [b for b in real if not b.get("twinned_pair_id")]
        already = len(real) - len(chosen)
    else:
        chosen, already = [], 0
        for want in bots:
            # (the sample's invented bots are not setup's business: hermes twin demo shows those)
            hits = [b for b in real if str(b.get("name") or "").lower() == want.lower()]
            if not hits:
                err(f"No Grok bot is called \"{want}\". hermes twin list-grok shows the names. {NOTHING_CHANGED}")
                return 1
            for b in hits:
                if b.get("twinned_pair_id"):
                    already += 1
                elif b not in chosen:
                    chosen.append(b)
    if not chosen:
        out("Every bot you named already has a twin." if not every else "Every Grok bot already has a twin.")
        return 0
    ids = [b["grok_id"] for b in chosen]
    prev = svc.preview(ids, {})
    profiles = {p["grok"]["id"]: p["hermes"]["profile"] for p in prev["pairs"] if p["hermes"]["profile"]}
    ids = [p["grok"]["id"] for p in prev["pairs"]]
    already += len(prev.get("skipped") or [])
    if not ids:
        out("Every bot you named already has a twin." if not every else "Every Grok bot already has a twin.")
        return 0
    res = svc.create(ids, profiles)
    made = len(res.get("pairs") or [])
    out(f"{plural(made, 'bot')} now {'has' if made == 1 else 'have'} a twin."
        + (f" {plural(already, 'bot')} already had one." if already else ""))
    for note in res.get("notes") or []:
        out(f" note: {note}")
    return 0 if made else 1


def share_on(svc: Any, out: Callable[[str], None]) -> int:
    f = svc.fleet_share(True)
    sh = f.get("share") or {}
    if sh.get("state") == "error":
        problem = sh.get("problem") or {}
        out(f"Sharing is switched on, but: {problem.get('message', 'it could not start')}")
        if problem.get("hint"):
            out(f"  {problem['hint']}")
        return 1
    via = {"tailscale": " over Tailscale (encrypted)", "lan": " on your home network (signed, not encrypted)",
           "loopback": " on this computer only"}.get(sh.get("network"), "")
    out(f"Sharing is on{via}.")
    code = (sh.get("code") or {}).get("code")
    if not code:
        code = ((svc.fleet_code().get("code")) or {}).get("code")
    if code:
        out(f"Link code: {code}   (one computer, 10 minutes)")
        out("On the other computer:  hermes twin setup --link CODE")
    out("Sharing runs while Hermes is open on this computer.")
    return 0


def link(svc: Any, code: str, out: Callable[[str], None]) -> int:
    f = svc.fleet_link(code)
    src = f.get("source") or {}
    name = safe.clean_name(src.get("name"), 60) or "your other computer"
    out(f"Linked to {name}. {plural(len(f.get('twins') or []), 'twin')} here so far; the rest arrive by "
        "themselves (hermes twin fleet).")
    try:
        from . import bgagent
        hint = bgagent.link_hint()
        if hint:
            out(hint)
    except Exception:
        pass
    return 0
