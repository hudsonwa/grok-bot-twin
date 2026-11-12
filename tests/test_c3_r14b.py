"""R14b: the README fits one screen's worth of reading (<= 150 lines); the rest lives in docs/, once.

Every relative link in README.md, AGENTS.md, CLAUDE.md and docs/*.md must reach a file that exists, and a `#part`
of such a link a heading in that file (GitHub's way of turning a heading into an anchor). The disclosure has one
home, docs/DISCLOSURE.md: the README only points to it.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
LINKED = [ROOT / "README.md", ROOT / "AGENTS.md", ROOT / "CLAUDE.md"] + sorted((ROOT / "docs").glob("*.md"))
LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)\)")


def _anchors(path: Path) -> set:
    out = set()
    text = re.sub(r"```.*?```", "", path.read_text(encoding="utf-8"), flags=re.S)
    for title in re.findall(r"^#{1,6} +(.+?) *$", text, flags=re.M):
        slug = re.sub(r"[^\w\- ]", "", title.strip().lower().replace("`", "")).replace(" ", "-")
        out.add(slug)
    return out


def test_R14b_the_readme_is_at_most_150_lines():
    assert len(README.splitlines()) <= 150, len(README.splitlines())


def test_R14b_every_relative_link_in_the_docs_reaches_a_file_and_its_heading():
    broken, seen = [], 0
    for doc in LINKED:
        text = re.sub(r"```.*?```", "", doc.read_text(encoding="utf-8"), flags=re.S)
        for target in LINK.findall(text):
            if re.match(r"[a-z]+:", target):
                continue  # https:, mailto: and the like are not files here
            seen += 1
            path, _, anchor = target.partition("#")
            dest = (doc.parent / path).resolve() if path else doc
            if not dest.exists():
                broken.append(f"{doc.relative_to(ROOT)}: {target} (no such file)")
            elif anchor and dest.suffix == ".md" and anchor not in _anchors(dest):
                broken.append(f"{doc.relative_to(ROOT)}: {target} (no such heading)")
    assert seen > 30 and not broken, broken


def test_R14b_the_disclosure_lives_in_one_place():
    part = README.split("## Disclosure", 1)[1].split("\n## ", 1)[0]
    assert "docs/DISCLOSURE.md" in part and len(part.strip().splitlines()) <= 2
    # the README no longer repeats what docs/DISCLOSURE.md says
    for words in ("sand-client-persistence", "Gatekeeper", "xs-memory.json", "inbox/pairs.txt", "SHAKE-256"):
        assert words not in README, words
    # the long sections now live in docs/, each in one file
    for heading, home in (("## Everyday use", "GUIDE.md"), ("## Settings", "GUIDE.md"), ("## Uninstall", "GUIDE.md"),
                          ("## Ask a twin on another computer", "SHARING.md"), ("## The sample tour", "TERMINAL.md")):
        assert heading + "\n" not in README, heading
        homes = [p.name for p in (ROOT / "docs").glob("*.md") if heading + "\n" in p.read_text(encoding="utf-8")]
        assert homes == [home], (heading, homes)
