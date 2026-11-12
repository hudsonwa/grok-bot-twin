"""Several twins at once: after "Twin N bots", each new twin still needs its instructions and a model. This module is
the backend of the sheet that does both for all ticked twins together.

- A model for many twins: `set_model` writes one plain model id into a twin's own config.yaml (never the default
  profile's), keeping the other keys of its `model` mapping. `batch_info` lists the ids this Hermes already uses.
- Instructions for many twins: the owner pastes one request (setup_export.REQUEST) into each bot's own Grok chat and
  drops the answer files on the sheet, or saves them to Downloads where `downloads_scan` picks them up. Each file is
  matched to a twin by the bot's id or name and imported with the one-twin import (`import_setup`), which leaves
  memories and facts about the owner out and adds routines switched off.

A file's text lives for the length of one call: it is never written, logged or echoed (events carry counts). The
only thing kept about a file in Downloads is a sha256 digest of its name, size and time, so it is not imported twice.
"""
from __future__ import annotations

import contextlib
import hashlib
import math
import os
import re
import stat
from pathlib import Path
from typing import Any, Optional

from . import hermes_side as hs
from . import osflags as _osflags
from . import safe, store
from . import setup_export as sx
from .connect import BadInput, downloads_dir
from .engine import EngineError
from .finish import BAD_REQUEST
from .model import valid_id

PIDS_MAX = 200
FILES_MAX = 40                # files of one drop, and files of Downloads looked at in one scan
MODELS_MAX = 20
PROFILES_READ_MAX = 60        # twin config files read to list the models in use
SEEN_FILE = "batch-seen.json"
SEEN_MAX = 500
SCAN_ENTRIES_MAX = 5000       # entries of the Downloads folder looked at (names, types, sizes and times only)
FILE_NAME_CAP = 120
SUFFIXES = (".json", ".txt", ".md")
MARK = "grok-bot-export"
LEFT_PARTS = ("instructions", "model")
_HEX64 = re.compile(r"[0-9a-f]{64}")
# a model id: one plain word of the characters model ids are made of (provider/model, model:tag, version dots)
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,199}")

BAD_MODEL = "That is not a model name. Choose one from the list, or type its exact id."
NO_TWIN_WHY = "No ticked twin is for the bot in this file."
NO_TWIN_MANY = "No ticked twin is for any of the {n} bots in this file."
TWO_TWINS_WHY = "Two ticked twins have this bot's name; import this file into one twin at a time."
DUP_WHY = "Another file is for the same twin; that one is used."


def model_id(value: Any) -> Optional[str]:
    """A model id that may be written into a twin's config: hermes_side's plain-word rule, and only the characters
    model ids are made of."""
    s = hs._plain(value)
    return s if s and _MODEL_RE.fullmatch(s) else None


def _key(name: Any) -> str:
    return safe.clean_name(name or "", 100).casefold()


def _file_name(value: Any) -> str:
    return safe.clean_name(value if isinstance(value, str) else "", FILE_NAME_CAP) or "(no name)"


def _files(files: Any) -> list:
    """The dropped files as [(shown name, text)], checked. A text over the cap stays in the list (as None) so its
    row says so."""
    if not isinstance(files, list) or not files or len(files) > FILES_MAX:
        raise BadInput("Choose between 1 and 40 setup export files.")
    out = []
    for f in files:
        if not isinstance(f, dict) or not isinstance(f.get("text"), str) or not isinstance(f.get("name", ""), str) \
                or len(f.get("name", "")) > 1000:
            raise BadInput(BAD_REQUEST)
        out.append((_file_name(f.get("name", "")), f["text"] if len(f["text"]) <= sx.TEXT_CAP else None))
    return out


def _bots(text: Optional[str]) -> tuple[list, str]:
    """([(bot id or "", bot name)], why not) of one file. Only the two fields are looked at."""
    if text is None:
        return [], "That file is too large to be a setup export."
    try:
        doc = sx.parse(text)
    except EngineError as exc:
        return [], exc.message
    out = []
    for b in doc.get("bots", [])[:sx.BOTS_MAX]:
        if not isinstance(b, dict):
            continue
        # (the bot's name exactly as pick_bot shows it, so import_setup finds the same bot by it)
        name = safe.clean_name(sx._one_line(b.get("name"), sx.FIELD_CUT), 100)
        bid = b.get("id") if isinstance(b.get("id"), str) and valid_id(b.get("id")) else ""
        if name or bid:
            out.append((bid, name))
    if not out:
        return [], "That setup export names no bot."
    return out, ""


def _match_rows(files: list, rows: list) -> list:
    """Every (file, bot) to the ticked twin it is for. `files`: [(shown name, text or None)]. Each match keeps the
    index of its file (`_i`, taken out before anything is returned)."""
    by_id: dict = {}
    by_name: dict = {}
    for r in rows:
        if r["grok_id"]:
            by_id.setdefault(r["grok_id"], []).append(r)
        by_name.setdefault(_key(r["name"]), []).append(r)
    out: list = []
    taken: set = set()
    for i, (fname, text) in enumerate(files):
        bots, why = _bots(text)
        if why:
            out.append({"_i": i, "file": fname, "pair_id": None, "name": "", "bot": "", "state": "not_export",
                        "why": why})
            continue
        found = []
        for bid, bname in bots:
            hit = by_id.get(bid, []) if bid else []
            if not hit and bname:
                hit = by_name.get(_key(bname), [])
            if len(hit) == 1:
                r = hit[0]
                dup = r["pair_id"] in taken
                taken.add(r["pair_id"])
                found.append({"_i": i, "file": fname, "pair_id": r["pair_id"], "name": r["name"], "bot": bname,
                              "state": "ambiguous" if dup else "matched", "why": DUP_WHY if dup else ""})
            elif len(hit) > 1:
                found.append({"_i": i, "file": fname, "pair_id": None, "name": "", "bot": bname,
                              "state": "ambiguous", "why": TWO_TWINS_WHY})
        if not found:
            found.append({"_i": i, "file": fname, "pair_id": None, "name": "",
                          "bot": bots[0][1] if len(bots) == 1 else "", "state": "no_twin",
                          "why": NO_TWIN_WHY if len(bots) == 1 else NO_TWIN_MANY.format(n=len(bots))})
        out += found
    return out


def _public(match: dict) -> dict:
    return {k: v for k, v in match.items() if not k.startswith("_")}


def _replace_model_block(text: str, block: str) -> Optional[str]:
    """`text` with its top-level `model:` entry (and the indented lines under it) replaced by `block`; None when
    the file has no single such entry."""
    lines = text.split("\n")
    starts = [i for i, ln in enumerate(lines) if re.match(r"model\s*:", ln)]
    if len(starts) != 1:
        return None
    i = starts[0]
    j = i + 1
    while j < len(lines) and (not lines[j].strip() or lines[j][:1] in (" ", "\t")):
        j += 1
    while j > i + 1 and not lines[j - 1].strip():  # blank lines before the next key stay where they were
        j -= 1
    new = lines[:i] + block.rstrip("\n").split("\n") + lines[j:]
    return "\n".join(new)


class BatchMixin:
    """The several-twins half of TwinService (see service.py for the attributes it uses)."""

    # ---- which twins ------------------------------------------------------------------------------------------
    def _batch_rows(self, pids: Any = None, need: bool = False) -> list:
        """[{pair_id, name, grok_id, profile, left, sample}] of every twin, or of `pids` in that order."""
        if pids is not None:
            if not isinstance(pids, list) or len(pids) > PIDS_MAX or not all(isinstance(p, str) for p in pids):
                raise BadInput(BAD_REQUEST)
            pids = list(dict.fromkeys(self.require_pair(p) for p in pids))
        if need and not pids:
            raise BadInput("Tick at least one twin.")
        demo = set(self.demo.pair_ids())
        shown = {p["pair_id"]: p for p in self.status()["pairs"] if isinstance(p, dict)}
        order = pids if pids is not None else list(shown)
        rows = []
        for pid in order:
            p = shown.get(pid)
            if p is None:
                raise EngineError("pair_not_found", "That is not one of your twins.", "Open the Twins page again.")
            g = p.get("grok") or {}
            rows.append({"pair_id": pid, "name": safe.clean_name(g.get("name"), 100),
                         "grok_id": g.get("id") if valid_id(g.get("id")) else "",
                         "profile": (p.get("hermes") or {}).get("profile") or "",
                         "left": [x for x in (p.get("setup_left") or []) if x in LEFT_PARTS],
                         "sample": pid in demo or p.get("demo") is True})
        return rows

    # ---- what the sheet shows first ------------------------------------------------------------------------------
    def _batch_models(self) -> tuple[str, list, dict]:
        """(the default profile's model id, the distinct model ids in use (default first), {model id: provider})."""
        root = store.hermes_root()
        homes = [root]
        demo = set(self.demo.pair_ids())
        for pid, e in list(self._registry().items())[:PROFILES_READ_MAX]:
            prof = (e.get("hermes") or {}).get("profile") if isinstance(e.get("hermes"), dict) else None
            home = self._profile_home(prof) if pid not in demo and prof and prof != "default" else None
            if home is not None and home != root:
                homes.append(home)
        default, models, providers = "", [], {}
        for n, home in enumerate(homes):
            m = model_id(hs.profile_model(home) or "")
            if n == 0:
                default = m or ""
            if m and m not in models and len(models) < MODELS_MAX:
                models.append(m)
            if m and m not in providers:
                prov = self._config_provider(home)
                if prov:
                    providers[m] = prov
        return default, models, providers

    @staticmethod
    def _config_provider(home: Path) -> str:
        text = hs._read_below_root(Path(home) / "config.yaml", hs.CONFIG_CAP)
        if not text:
            return ""
        try:
            doc = hs._yaml_load(text)
        except Exception:
            return ""
        m = doc.get("model") if isinstance(doc, dict) else None
        return (model_id(m.get("provider")) or "") if isinstance(m, dict) else ""

    def batch_info(self, pids: Any = None) -> dict:
        rows = self._batch_rows(pids)
        default, models, _ = self._batch_models()
        from . import endpoints as _ep
        eps = _ep.public(_ep.endpoints())
        twins = []
        for r in rows:
            m = "" if r["sample"] else model_id(hs.profile_model(self._profile_home(r["profile"])) or "") or ""
            twins.append({"pair_id": r["pair_id"], "name": r["name"], "profile": r["profile"], "left": r["left"],
                          "model": m, "sample": r["sample"]})
        from . import skill_sync
        ids = set(self.pair_ids())
        ss = skill_sync.load_state(self.central)
        from . import mirror_exports
        return {"request": mirror_exports.request(self.central), "pickup": mirror_exports.pickup(self.central),
                "privacy": sx.PRIVACY, "default_model": default, "models": models,
                "endpoints": eps,
                "twins": twins, "downloads": {"available": downloads_dir() is not None, "label": "Downloads"},
                "skill_sync": {"set_up": skill_sync.shared_root(self.central) is not None,
                               "pair_ids": [p for p in ss["twins"] if p in ids][:PIDS_MAX]}}

    # ---- the model -----------------------------------------------------------------------------------------
    def _endpoint_choice(self, endpoint: Any, m: str) -> Optional[dict]:
        """The listed endpoint the page chose (by name; never a URL from the page), or None for no endpoint."""
        if endpoint in (None, ""):
            return None
        from . import endpoints as _ep
        e = _ep.find(_ep.endpoints(), endpoint, m)
        if e is None:
            raise BadInput("That model server is not set up in this Hermes, or does not list that model. "
                           "Choose one from the list.")
        return e

    def set_model(self, pid: Any, model: Any, _from: str = "owner", _providers: Optional[dict] = None,
                  endpoint: Any = None, _endpoint: Optional[dict] = None) -> dict:
        """Write `model` into the twin's own config.yaml (atomic, 0600). A `model` mapping keeps its other keys and
        gets `default`; a text or no entry becomes the id. The provider is set when another profile of this Hermes
        uses the same model with a known provider. The default profile is never written."""
        pid = self.require_pair(pid)
        m = model_id(model)
        if not m:
            raise BadInput(BAD_MODEL)
        if pid in self.demo.pair_ids():
            return {"pair_id": pid, "model": m, "state": "sample"}
        row = self._batch_rows([pid])[0]
        prof = row["profile"]
        home = self._profile_home(prof)
        root = store.hermes_root()
        if home is None or not prof or prof == "default" or prof.startswith("-") or not valid_id(prof) \
                or Path(home) == root:
            raise EngineError("bad_profile", "This twin's Hermes bot could not be found.",
                              "Open the Twins page again; if it stays, stop twinning this bot and twin it again.")
        ep = _endpoint if _endpoint is not None else self._endpoint_choice(endpoint, m)
        providers = self._batch_models()[2] if _providers is None else _providers
        with store.locked():
            if ep is not None:
                state = self._write_model(Path(home), m, "custom", ep["_base_url"])
            else:
                state = self._write_model(Path(home), m, providers.get(m, ""))
        if state == "set" or not isinstance(self._registry().get(pid, {}).get("model"), dict):
            self._note_entry(pid, model={"from": "batch" if _from == "batch" else "owner", "value": m})
        self.invalidate()
        if state == "set":
            self.emit("model_set", pid, {"by": "batch" if _from == "batch" else "owner"})
        return {"pair_id": pid, "model": m, "state": state}

    @staticmethod
    def _write_model(home: Path, m: str, provider: str, base_url: str = "") -> str:
        """"set" or "same". The file is changed only in its `model` entry; it must read back as exactly what it had
        with that entry changed, or nothing is written."""
        cant = EngineError("unsafe_folder", "Hermes could not change the model in this twin's own folder. Nothing "
                           "was changed.", "Choose the model in Hermes under Settings, then Model.")
        path = home / "config.yaml"
        text = hs._read_below_root(path, hs.CONFIG_CAP)
        if text is None and os.path.lexists(path):
            raise cant
        try:
            yaml = hs._yaml_module()
            doc = yaml.safe_load(text) if text else {}
        except Exception:
            raise cant
        doc = {} if doc is None else doc
        if not isinstance(doc, dict):
            raise cant
        old = doc.get("model")
        if isinstance(old, dict):
            new: Any = dict(old)
            new["default"] = m
            if provider:
                new["provider"] = provider
            if base_url:
                new["base_url"] = base_url
        elif provider:
            new = {"default": m, "provider": provider, **({"base_url": base_url} if base_url else {})}
        else:
            new = m
        if hs._model_of(doc) == m and (not isinstance(old, dict) or not provider or old.get("provider") == provider) \
                and (not base_url or (isinstance(old, dict) and old.get("base_url") == base_url)):
            return "same"
        want = {**doc, "model": new}
        block = yaml.safe_dump({"model": new}, default_flow_style=False, sort_keys=True, allow_unicode=True)
        candidates = []
        if text and "model" in doc:
            swapped = _replace_model_block(text, block)
            if swapped is not None:
                candidates.append(swapped)
        elif text is not None or not os.path.lexists(path):
            base = text or ""
            candidates.append(base + ("" if not base or base.endswith("\n") else "\n") + block)
        candidates.append(yaml.safe_dump(want, default_flow_style=False, sort_keys=False, allow_unicode=True))
        for new_text in candidates:
            try:
                if yaml.safe_load(new_text) != want:
                    continue
            except Exception:
                continue
            try:
                hs._write_private(path, new_text)
            except (OSError, UnicodeError):
                raise cant
            return "set"
        raise cant

    def batch_models(self, pids: Any, model: Any, endpoint: Any = None) -> dict:
        if not model_id(model):
            raise BadInput(BAD_MODEL)
        ep = self._endpoint_choice(endpoint, model_id(model))
        rows = self._batch_rows(pids, need=True)
        providers = self._batch_models()[2]
        out = []
        for r in rows:
            try:
                state = self.set_model(r["pair_id"], model, _from="batch", _providers=providers,
                                       _endpoint=ep)["state"]
                why = ""
            except EngineError as exc:
                state, why = "failed", exc.message
            out.append({"pair_id": r["pair_id"], "name": r["name"], "state": state, "why": why})
        return {"results": out}

    # ---- matching and importing files ---------------------------------------------------------------------------
    def batch_match(self, files: Any, pids: Any = None) -> dict:
        got = _files(files)
        rows = self._batch_rows(pids)
        return {"matches": [_public(m) for m in _match_rows(got, rows)]}

    def _import_one(self, row: dict, match: Optional[dict], text: Optional[str]) -> dict:
        res = {"pair_id": row["pair_id"], "name": row["name"], "file": match["file"] if match else "",
               "state": "no_file", "instructions": False, "routines": 0, "connectors": 0, "lines_left_out": 0,
               "why": ""}
        if row["sample"]:
            res.update(state="sample", why=sx.SAMPLE[0])
            return res
        if match is None or text is None:
            return res
        try:
            got = self.import_setup(row["pair_id"], text, apply=True, bot=match["bot"] or None)
        except EngineError as exc:
            res.update(state="failed", why=exc.message)
            return res
        ins = got["instructions"]
        failed = [r for r in got["routines"] if r.get("state") == "failed"]
        why = ins["why"] if ins["state"] == "held" else (f"{failed[0]['name']}: {failed[0]['why']}" if failed else "")
        res.update(state="done", instructions=bool(ins["saved"]), routines=int(got["added"]),
                   connectors=len(got["connectors"]),
                   lines_left_out=len(ins["left_out"]) if ins["saved"] else 0, why=safe.clean_summary(why, 200))
        return res

    def _import_matches(self, rows: list, matches: list, texts: list, with_no_file: bool) -> list:
        mine = {m["pair_id"]: m for m in matches if m["state"] == "matched"}
        out = []
        for r in rows:
            m = mine.get(r["pair_id"])
            if m is None and not with_no_file:
                continue
            out.append(self._import_one(r, m, texts[m["_i"]] if m else None))
        return out

    def batch_import(self, files: Any, pids: Any = None) -> dict:
        got = _files(files)
        rows = self._batch_rows(pids)
        matches = _match_rows(got, rows)
        texts = [t for _, t in got]
        del got
        # (every ticked twin gets a line; with no twins named, the sample twins without a file are left out)
        if pids is None:
            hit = {m["pair_id"] for m in matches if m["state"] == "matched"}
            rows = [r for r in rows if not r["sample"] or r["pair_id"] in hit]
        out = self._import_matches(rows, matches, texts, with_no_file=True)
        del texts
        self.emit("batch_import", None, {"files": len(files), "twins": sum(1 for r in out if r["state"] == "done")})
        return {"results": out}

    # ---- Downloads -------------------------------------------------------------------------------------------
    def _seen_digests(self) -> list:
        data = store.read_json(self.central / SEEN_FILE, {}) or {}
        seen = data.get("seen") if isinstance(data, dict) else None
        return [d for d in seen if isinstance(d, str) and _HEX64.fullmatch(d)][-SEEN_MAX:] \
            if isinstance(seen, list) else []

    def _note_seen(self, digests: list) -> None:
        if not digests:
            return
        with store.locked():
            seen = self._seen_digests()
            seen = [d for d in seen if d not in digests] + list(digests)
            store.atomic_write_json(self.central / SEEN_FILE, {"seen": seen[-SEEN_MAX:]})

    def _batch_cache(self) -> dict:
        """digest -> {"pids": [twins the file is for], "imported": bool}, for this process (no text, no name)."""
        cache = getattr(self, "_batch_files", None)
        if cache is None:
            cache = self._batch_files = {}
        return cache

    @staticmethod
    def _download_candidates(folder: Path, since: float) -> list:
        """[(name, path, size, mtime_ns)] of the regular files in Downloads modified at or after `since`, with a
        setup export's suffix and size, newest first, at most FILES_MAX. Names, types, sizes and times only."""
        found = []
        try:
            with os.scandir(folder) as it:
                for n, entry in enumerate(it):
                    if n >= SCAN_ENTRIES_MAX:
                        break
                    name = entry.name
                    if not name.lower().endswith(SUFFIXES) or name.startswith("."):
                        continue
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if not stat.S_ISREG(st.st_mode) or _osflags.is_link(st) or st.st_size > sx.TEXT_CAP \
                            or st.st_size == 0 or st.st_mtime < since:
                        continue
                    found.append((name, Path(entry.path), st.st_size, st.st_mtime_ns))
        except OSError:
            return []
        found.sort(key=lambda f: f[3], reverse=True)
        return found[:FILES_MAX]

    @staticmethod
    def _read_download(path: Path, size: int, mtime_ns: int) -> Optional[str]:
        """The file's text, read through one descriptor opened without following a link; None when it changed since
        it was listed, is not the user's, is over the cap, is not UTF-8 or does not say it is a setup export."""
        try:
            fd = _osflags.open_nofollow(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        except OSError:
            return None
        try:
            st = os.fstat(fd)
            if (not stat.S_ISREG(st.st_mode) or st.st_size != size or st.st_mtime_ns != mtime_ns
                    or st.st_size > sx.TEXT_CAP or (_osflags.posix() and st.st_uid != os.getuid())):
                return None
            chunks, total = [], 0
            while total <= sx.TEXT_CAP:
                chunk = os.read(fd, min(1 << 20, sx.TEXT_CAP + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            if total > sx.TEXT_CAP:
                return None
            text = b"".join(chunks).decode("utf-8-sig")
        except (OSError, UnicodeDecodeError):
            return None
        finally:
            with contextlib.suppress(OSError):
                os.close(fd)
        return text if MARK in text else None

    def downloads_scan(self, since: Any, pids: Any = None, apply: Any = True) -> dict:
        """Setup exports saved to Downloads since `since` (epoch seconds), matched to the ticked twins and, with
        `apply`, imported. A file is imported once (its digest is kept). `waiting`: ticked twins no file is for."""
        if isinstance(since, bool) or not isinstance(since, (int, float)) or not math.isfinite(since) or since < 0 \
                or not isinstance(apply, bool):
            raise BadInput(BAD_REQUEST)
        rows = self._batch_rows(pids)
        real = [r for r in rows if not r["sample"]]
        folder = downloads_dir()
        # (and, with skill sync set up, the exports the sync bot copied up to the shared folder: mirror_exports)
        from . import mirror_exports
        found = (self._download_candidates(folder, float(since)) if folder is not None else []) \
            + mirror_exports.candidates(self.central, float(since))
        cache = self._batch_cache()
        seen = set(self._seen_digests())
        ticked = {r["pair_id"] for r in rows}
        covered: set = set()
        new_files, new_digests = [], []
        n_files = 0
        for name, path, size, mtime_ns in found:
            digest = hashlib.sha256(f"{name}\0{size}\0{mtime_ns}".encode("utf-8", "surrogatepass")).hexdigest()
            known = cache.get(digest)
            # (a file read before is not read again, unless it still waits to be imported)
            if known is not None and known["ticked"] == sorted(ticked) and (
                    not known["pids"] or digest in seen or not apply):
                if known["pids"] is not None:
                    n_files += 1
                    covered.update(known["pids"])
                continue
            text = self._read_download(path, size, mtime_ns)
            if text is None:
                cache[digest] = {"pids": None, "ticked": sorted(ticked)}
                continue
            n_files += 1
            matches = _match_rows([(_file_name(name), text)], rows)
            pids_for = [m["pair_id"] for m in matches if m["state"] == "matched"]
            cache[digest] = {"pids": pids_for, "ticked": sorted(ticked)}
            covered.update(pids_for)
            if digest not in seen and pids_for:
                new_files.append((_file_name(name), text, digest))
            del text
        if len(cache) > SEEN_MAX:
            for d in list(cache)[: len(cache) - SEEN_MAX]:
                cache.pop(d, None)
        results: list = []
        if apply and new_files:
            got = [(n, t) for n, t, _ in new_files]  # newest first: a twin with two new files takes the newest
            matches = _match_rows(got, rows)
            texts = [t for _, t in got]
            results = self._import_matches(rows, matches, texts, with_no_file=False)
            new_digests = [d for _, _, d in new_files]
            del got, texts
            self._note_seen(new_digests)
            self.emit("batch_import", None, {"files": len(new_digests),
                                             "twins": sum(1 for r in results if r["state"] == "done")})
        elif new_files:
            # (a look without importing: what an import would take, nothing written)
            for m in _match_rows([(n, t) for n, t, _ in new_files], rows):
                if m["state"] == "matched":
                    results.append({"pair_id": m["pair_id"], "name": m["name"], "file": m["file"], "state": "ready",
                                    "instructions": False, "routines": 0, "connectors": 0, "lines_left_out": 0,
                                    "why": ""})
        del new_files
        return {"files": n_files, "results": results,
                "waiting": [r["pair_id"] for r in real if r["pair_id"] not in covered]}
