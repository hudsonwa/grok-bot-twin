"""R2: every matter kind the page can produce has a box and a page action (no silent empty `<span>`).

The open choices of the instructions sync and of Hermes -> Grok (`open_conflicts` in /status) used to become a
kind `choose` that no box drew. They are now the one "changed in both places" matter, whose box settles them
(tests/desktop/check.mjs, "R2:" assertions, drives the box itself)."""
import re
from pathlib import Path

PAGE = (Path(__file__).resolve().parents[1] / "desktop" / "plugin.js").read_text(encoding="utf-8")


def _body(start: str) -> str:
    """The text of one function or arrow, from `start` to the line that closes it at the same indent (a single
    expression such as `hasMainAction` ends at the first blank line)."""
    i = PAGE.index(start)
    pad = " " * (len(start) - len(start.lstrip(" ")))
    close = rf"^{pad}\}}$" if start.endswith("{") else r"^$"
    return PAGE[i:re.compile(close, re.M).search(PAGE, i).end()]


def _kinds_made() -> set:
    return set(re.findall(r"kind: '(\w+)'", _body("function mattersOf(pair) {")))


def test_R2_every_matter_kind_has_a_box_and_an_action():
    made = _kinds_made()
    assert made >= {"error", "missing", "conflict", "waiting", "unsent", "secret"}
    box = _body("  const box = m => {")
    drawn = dict(re.findall(r"if \(m\.kind === '(\w+)'\)\s*\{?\s*return jsx\(\s*(\w+)", box))
    last = re.findall(r"^    return (.*)$", box, re.M)
    assert last and last[-1].startswith("jsx(UnsentBox"), "the box's last answer is a real box, never an empty span"
    drawn.setdefault("unsent", "UnsentBox")
    assert "el('span', { key: m.id })" not in PAGE
    assert made <= set(drawn), f"kinds with no box: {sorted(made - set(drawn))}"
    acts = _body("const hasMainAction = (m, pair) =>")
    has = set(re.findall(r"m\.kind === '(\w+)'", acts))
    assert made <= has, f"kinds with no page action: {sorted(made - has)}"
    icons = set(re.findall(r"(\w+): '", PAGE[PAGE.index("const ICON_OF = {"):].split("\n", 1)[0]))
    assert made <= icons, f"kinds with no header icon: {sorted(made - icons)}"


def test_R2_open_choices_are_the_conflict_matter_and_settled_from_the_page():
    assert "'choose'" not in _body("function mattersOf(pair) {")
    assert "open_conflicts" in _body("function mattersOf(pair) {")
    # the instructions sync's card is read and answered by its own routes; Hermes -> Grok's by its sheet
    assert "pairPath(pid, 'instructions-sync')" in PAGE
    assert "pairPath(pid, 'instructions-sync/resolve')" in PAGE
    assert "post(path('resolve'), { item, keep })" in PAGE
    blocked = _body("function blockedSentence(pair, fromBackend) {")
    assert "m.kind === 'conflict'" in blocked
