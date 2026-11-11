"""What copies over, and how: the one table.

Every place that says what a twin carries says it from this file: the fold "The whole table" in a twin's Details
on the Twins page, the README's table and `docs/DISCLOSURE.md`. The page and the README cannot import Python, so
their copies are written from here and a test compares them:

    python3 twin/copies.py --check    # are README.md and desktop/plugin.js the same as this table?
    python3 twin/copies.py --write    # write the table into both

A row that waits for work that is not merged yet names it in `needs`. While that name is in PENDING the row says
`until` (what is true of this version); afterwards it says `how`. When the work is merged: take its name out of
PENDING, read the row's `how` once more against what was built, and run `--write`. Nothing else has to change.

Standard library only, no imports from the plugin: the file also runs on its own.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

# Work that is not in this version yet. One name per row that waits for it (see `needs` below).
PENDING: tuple[str, ...] = ()

NOT_YET = "Not in this version yet."

GROUPS: tuple[tuple[str, str], ...] = (
    ("grok", "From Grok to Hermes"),
    ("back", "From Hermes back to Grok"),
    ("computers", "Between your own computers"),
)

# key, group, what, how, needs, until
_ROWS: tuple[tuple[str, str, str, str, Optional[str], Optional[str]], ...] = (
    ("name", "grok", "Name and description",
     "By itself, in seconds, while Hermes is open.", None, None),
    ("setup", "grok", "Instructions, routines and connections",
     "With Import setup export: the bot writes a file about itself when you ask it in Grok. Instructions are filled "
     "in, routines arrive switched off, and connections arrive as a list of names. Logins are never copied. With skill "
     "sync set up, instructions also follow Grok's every 15 minutes by themselves, unless you switch that off. When "
     "it is unsure which to keep, you are asked.",
     "xs-export",
     "You bring these over yourself: paste the instructions once, then make the routines and connections again in "
     "Hermes. Importing the bot's setup export is not in this version yet."),
    ("skills", "grok", "Skills",
     "With skill sync, which is optional: a sync bot in your own Grok account carries them. Without it you copy "
     "them yourself.", None, None),
    ("grok-conversations", "grok", "Conversations",
     "Does not copy: whole conversations stay in Grok. Copying them comes in a later version.", None, None),
    ("grok-memory", "grok", "Grok memory",
     "Does not copy: memory is the most private thing a bot holds, so none is taken unless you switch on memory "
     "facts, which need skill sync.", None, None),
    ("grok-model", "grok", "Grok model setting",
     "Does not copy: nothing Hermes reads from Grok names the bot's model, so the twin uses the model you choose in "
     "Hermes.", None, None),
    ("back-persona", "back", "Name, description and instructions",
     "You make the same change in Grok yourself.", None, None),
    ("back-skills", "back", "Skills",
     "With skill sync, after you press Send to Grok: a skill Grok does not have yet is installed there. A skill "
     "Grok already has under that name is not replaced; paste the change into Grok yourself. Without skill sync "
     "you paste them all yourself.", None, None),
    ("back-setup", "back", "Routines and connections",
     "You change them in Grok yourself.", None, None),
    ("fleet-name", "computers", "Name and description",
     "By itself, in seconds, once the other computer is linked and Hermes is open on both.", None, None),
    ("fleet-skills", "computers", "Skills",
     "Off until you switch it on. Then the text of each twin's skills follows the Mac with the Grok app. A skill "
     "changed on both computers asks you which one to keep.",
     "xs-skills", f"{NOT_YET} Each computer keeps its own."),
    ("fleet-routines", "computers", "Routines",
     "Off until you switch it on. Then each twin's routines copy to every linked computer, and each routine runs "
     "on one computer that you choose, never on two.",
     "xs-routines", f"{NOT_YET} Each computer keeps its own."),
    ("fleet-memory", "computers", "Memory",
     "Off until you switch it on for a twin. Then it travels encrypted, between your linked computers only.",
     "xs-memory", f"{NOT_YET} Each computer keeps its own."),
    ("fleet-settings", "computers", "Settings",
     "By itself, item by item: tools, preferences and instructions follow the Mac with the Grok app. Each computer "
     "keeps its own model unless you switch that on.",
     "xs-config", f"{NOT_YET} Each computer keeps its own."),
    ("fleet-instructions", "computers", "Instructions",
     "By itself, as one of the twin's settings. You can switch that off on each computer.",
     "xs-config",
     "Copying them is not in this version yet: on a linked computer you add them to the Hermes bot yourself."),
    ("fleet-private", "computers", "Chats, files and logins",
     "Not copied. A request you send to another computer and its answer are the only chat text that travels, and "
     "only with requests switched on.", None, None),
)


def rows(pending: Optional[tuple[str, ...]] = None) -> list[dict]:
    """The table as it reads now: one dict per row (key, group, what, how, pending). `how` is the sentence to
    show; `pending` is True while the row waits for work that is not merged."""
    waiting = PENDING if pending is None else pending
    out = []
    for key, group, what, how, needs, until in _ROWS:
        late = needs is not None and needs in waiting
        out.append({"key": key, "group": group, "what": what, "how": until if late and until else how,
                    "pending": late})
    return out


def table(pending: Optional[tuple[str, ...]] = None) -> list[dict]:
    """The same rows under their three headings: [{"key", "title", "rows": [...]}]."""
    all_rows = rows(pending)
    return [{"key": key, "title": title, "rows": [r for r in all_rows if r["group"] == key]} for key, title in GROUPS]


def never() -> list[str]:
    """What is never copied from Grok, by name (fixed words, for a chat tool's answer)."""
    return [r["what"] for r in rows() if r["key"] in ("grok-memory", "grok-model")]


def lines(pending: Optional[tuple[str, ...]] = None) -> list[str]:
    """The table as plain lines, for a terminal."""
    out = []
    for group in table(pending):
        out.append(f"{group['title']}:")
        out.extend(f"  {r['what']}: {r['how']}" for r in group["rows"])
    return out


# ── the two written copies ──────────────────────────────────────────────────

README_BEGIN = "<!-- what-copies:begin (written by twin/copies.py: change the table there) -->"
README_END = "<!-- what-copies:end -->"
PAGE_BEGIN = "// what-copies:begin (written by twin/copies.py: change the table there)"
PAGE_END = "// what-copies:end"


def readme_block(pending: Optional[tuple[str, ...]] = None) -> str:
    out = [README_BEGIN, "", "| What | How |", "|---|---|"]
    for group in table(pending):
        out.append(f"| **{group['title']}** | |")
        out.extend(f"| {r['what']} | {r['how']} |" for r in group["rows"])
    out.extend(["", README_END])
    return "\n".join(out)


def _js(text: str) -> str:
    """A JavaScript string in the page's own style (single quotes; double quotes when the text holds one)."""
    return json.dumps(text, ensure_ascii=False) if "'" in text else "'" + text.replace("\\", "\\\\") + "'"


def page_block(pending: Optional[tuple[str, ...]] = None) -> str:
    out = [PAGE_BEGIN, "const WHAT_COPIES = ["]
    groups = table(pending)
    for gi, group in enumerate(groups):
        out.append("  {")
        out.append(f"    title: {_js(group['title'])},")
        out.append("    rows: [")
        for ri, r in enumerate(group["rows"]):
            fields = f"what: {_js(r['what'])}, how: {_js(r['how'])}" + (", pending: true" if r["pending"] else "")
            out.append(f"      {{ {fields} }}" + ("," if ri < len(group["rows"]) - 1 else ""))
        out.append("    ]")
        out.append("  }" + ("," if gi < len(groups) - 1 else ""))
    out.append("]")
    out.append(PAGE_END)
    return "\n".join(out)


def _swap(text: str, begin: str, end: str, block: str) -> Optional[str]:
    """`text` with what stands between the two marks replaced by `block`; None when the marks are not there once."""
    if text.count(begin) != 1 or text.count(end) != 1 or text.index(begin) > text.index(end):
        return None
    return text[:text.index(begin)] + block + text[text.index(end) + len(end):]


def written(root: Path, pending: Optional[tuple[str, ...]] = None) -> dict[Path, Optional[str]]:
    """What README.md and desktop/plugin.js should hold, by path (None: the marks are missing in that file)."""
    out: dict[Path, Optional[str]] = {}
    for rel, begin, end, block in (("README.md", README_BEGIN, README_END, readme_block(pending)),
                                   ("desktop/plugin.js", PAGE_BEGIN, PAGE_END, page_block(pending))):
        path = root / rel
        out[path] = _swap(path.read_text(encoding="utf-8"), begin, end, block)
    return out


def main(argv: list[str]) -> int:
    root = Path(__file__).resolve().parent.parent
    if argv == ["--check"] or argv == ["--write"]:
        stale = 0
        for path, want in written(root).items():
            if want is None:
                print(f"{path.name}: the what-copies marks are missing")
                stale += 1
            elif want != path.read_text(encoding="utf-8"):
                stale += 1
                if argv == ["--write"]:
                    path.write_bytes(want.encode("utf-8"))  # bytes: the same line ends on every system
                    print(f"{path.name}: written")
                    stale -= 1
                else:
                    print(f"{path.name}: differs from twin/copies.py (run: python3 twin/copies.py --write)")
        return 1 if stale else 0
    if argv:
        print("usage: python3 twin/copies.py [--check | --write]")
        return 2
    print("\n".join(lines()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
