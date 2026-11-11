"""Connecting: the facts "Connect your Grok bots" shows, the one cause when twins cannot follow Grok, and the
botport file the owner downloaded.

What happens to a chosen file, in order: it must be a plain file of sane size (never a link); its bytes are read
once; an archive is opened in memory and only the one member named `botport` is taken; the SHA-256 of the archive
and of the program are compared with the pinned release table (twin/pins.py). Only on a match is anything kept
(the plugin's own 0700 copy) or run (botport's `version`, by the verified-copy path in engine.py). A file that
does not match is not kept. Nothing is downloaded here: the owner's browser does that.

The Downloads folder is looked at by exact name only (`stat`, no listing, nothing opened) while botport is not
verified yet, so the page can say "Found botport in Downloads".
"""
from __future__ import annotations

import contextlib
import errno
import hashlib
import io
import os
import platform
import stat
import tarfile
import tempfile
from pathlib import Path
from typing import Any, Optional

from . import config, pins, safe, store
from . import osflags as _osflags
from .engine import (EXPECTED_BOTPORT_TAG, GH_HELP_COMMANDS, GH_HELP_URL, INSTALL_HINT, EngineError, find_gh,
                     release_url, valid_tag)

# The most a chosen file may hold (botport itself is about 2.5 MB). The file is read into memory once, so the
# cap is also what one request can make this process hold.
HELPER_CAP = 64 * 1024 * 1024
ARCHIVE_MEMBERS_CAP = 2000
_PROGRAM_MAGIC = (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce",
                  b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf", b"\x7fELF")
BAD_REQUEST = "That request was not understood."
TOO_LARGE = "That file is too large to be botport."
# The Grok desktop app's own download page, per platform.system(). Fixed text, checked by hand at plugin release
# time; no user input ever goes into it. A platform that is not listed has no button on the page.
GROK_APP_DOWNLOAD_URLS: dict = {}
# One plain sentence per cause (the page shows the sentence and switches on the cause, never on the words).
P_GROK_GONE = "The Grok desktop app is not on this computer any more."
P_BOTPORT_GONE = "botport is no longer on this computer."
P_BOTPORT_STALE = "botport was updated and has to be verified again."
P_NO_BUILD = "There is no botport for this kind of computer yet."
CAUSES = ("grok_app", "botport_missing", "botport_unverified", "no_build")
# What each real twin's row carries while one cause holds all of them up.
CAUSE_ERROR_CODES = {"grok_app": "grok_app_missing", "botport_missing": "botport_missing",
                     "botport_unverified": "untrusted_binary"}
NOT_BOTPORT_HINT = "Choose the file you downloaded with the Download botport button."
FRESH_DOWNLOAD_HINT = "Download it again with the Download botport button, then choose the new file."


class BadInput(EngineError):
    def __init__(self, message: str) -> None:
        super().__init__("bad_input", message)


def not_botport() -> EngineError:
    return EngineError("not_botport", "That is not botport.", NOT_BOTPORT_HINT)


def grok_app_download_url(system: Optional[str] = None) -> Optional[str]:
    url = GROK_APP_DOWNLOAD_URLS.get(platform.system() if system is None else system)
    return url if isinstance(url, str) and url.startswith("https://") and len(url) <= 200 else None


def program_from_archive(data: bytes) -> bytes:
    """The bytes of the one member named `botport` in a release archive. The archive is never unpacked to disk;
    links, devices and names with `..` or a leading `/` are never picked. Only what botport ships is opened: a
    gzip archive or a plain one (no bzip2, no xz)."""
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz" if data[:2] == b"\x1f\x8b" else "r:") as tar:
            seen = 0
            total = 0
            for m in tar:
                seen += 1
                total += max(0, m.size)
                if seen > ARCHIVE_MEMBERS_CAP or total > 4 * HELPER_CAP:
                    break
                parts = m.name.replace("\\", "/").split("/")
                if (not m.isreg() or parts[-1] != "botport" or m.name.startswith("/") or ".." in parts
                        or m.size <= 0 or m.size > HELPER_CAP):
                    continue
                fh = tar.extractfile(m)
                if fh is None:
                    continue
                blob = fh.read(HELPER_CAP + 1)
                if len(blob) <= HELPER_CAP:
                    return blob
    except (tarfile.TarError, OSError, EOFError, ValueError):
        pass
    raise not_botport()


def downloads_dir() -> Optional[Path]:
    """The user's Downloads folder, only when it is a real folder (a link is not followed, wherever it points)."""
    d = Path(os.path.expanduser("~")) / "Downloads"
    try:
        st = os.lstat(d)
    except OSError:
        return None
    return d if stat.S_ISDIR(st.st_mode) else None


def download_names(file_name: str) -> list:
    """The exact names a browser may give the download: the file itself, the same with " (1)" to " (9)" before
    the extension, and the program inside the folder a browser makes when it unpacks on arrival."""
    stem = file_name[:-len(".tar.gz")] if file_name.endswith(".tar.gz") else file_name
    ext = file_name[len(stem):]
    return [file_name, *[f"{stem} ({i}){ext}" for i in range(1, 10)], f"{stem}/botport", "botport"]


def download_candidates(file_name: str) -> list:
    """[(shown name, path)] of the download in Downloads, newest first. `stat` only: no folder is listed and
    no file is opened. Only plain files (and, for the unpacked case, a real folder holding one) count."""
    d = downloads_dir()
    if d is None:
        return []
    found = []
    for name in download_names(file_name):
        path = d / name
        try:
            if "/" in name and not stat.S_ISDIR(os.lstat(path.parent).st_mode):
                continue
            st = os.lstat(path)
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode) and 0 < st.st_size <= HELPER_CAP:
            found.append((st.st_mtime, name.split("/")[0], path))
    found.sort(key=lambda f: f[0], reverse=True)
    return [(shown, path) for _t, shown, path in found]


class ConnectMixin:
    """The connect half of TwinService (see service.py for the attributes it uses)."""

    # ---- what is wrong, as one cause -----------------------------------------------------------------------------
    def _was_connected(self) -> bool:
        """True once this install had botport verified (its check record is kept) or holds a real twin: only then
        is "no longer here" a true sentence. A computer that never was set up has no cause; the Connect sheet
        covers it."""
        if self._real_pair_ids():
            return True
        try:
            return os.path.lexists(self.central / "botport-trust.json")
        except OSError:
            return False

    def _cause(self, app: bool, found: bool, checked: bool, has_build: bool) -> tuple:
        """(cause, sentence), most specific first; (None, None) when nothing is wrong or nothing was ever set up."""
        if not has_build and not (found and checked):
            return "no_build", P_NO_BUILD
        if not app and self._real_pair_ids():
            return "grok_app", P_GROK_GONE
        if self._was_connected():
            if not found:
                return "botport_missing", P_BOTPORT_GONE
            if not checked:
                return "botport_unverified", P_BOTPORT_STALE
        return None, None

    def setup_state(self) -> dict:
        """What "Connect your Grok bots" shows. Cheap and side-effect free: files are stat()ed and the check record
        is read; botport itself never runs here."""
        cfg = config.settings()
        tag = cfg.get("botport_release_tag") or EXPECTED_BOTPORT_TAG
        bp = self.botport()
        helper = {"found": bp is not None, "version": None, "source": None}
        check = {"done": False, "problem": None, "gh_found": False, "unattested": False, "method": None}
        if bp is not None:
            try:
                chosen = os.path.realpath(str(self.chosen_helper()))
            except OSError:
                chosen = ""
            helper["source"] = "settings" if cfg["botport_path"] else ("chosen" if bp.path == chosen else "path")
            try:
                q = bp.quick_status()
            except EngineError as exc:
                q = {"checked": False, "version": None, "problem": exc.message}
            check["done"] = bool(q["checked"])
            # a local build accepted in the terminal without the check: the page must not call it "verified"
            check["unattested"] = bool(q.get("unattested"))
            check["method"] = q.get("method") if check["done"] and q.get("method") in ("pinned", "gh", "local") else None
            helper["version"] = safe.clean_name(q.get("version"), 30) or None
            check["problem"] = self._plain_check_problem(q.get("problem"))
        elif cfg["botport_path"]:
            check["problem"] = "The botport location in the plugin's settings does not point at a program."
        gh, gh_problem = find_gh()
        check["gh_found"] = gh is not None and not gh_problem
        app = self.grok_app_found()
        download = pins.download_for(tag)
        if download is not None:
            found = None
            if not check["done"]:  # looked for only while there is something to connect
                cands = download_candidates(download["file_name"])
                found = {"name": cands[0][0]} if cands else None
            folder = downloads_dir()
            download = {**download, "found": found, "dir": str(folder) if folder is not None else None}
        cause, problem = self._cause(app, helper["found"], check["done"], download is not None)
        return {"ready": helper["found"] and check["done"] and app,
                "grok_app": {"found": app, "download_url": grok_app_download_url()},
                "helper": helper, "check": check, "download_url": release_url(tag), "download": download,
                "problem": problem, "cause": cause, "release_tag": tag,
                "gh_help": {"url": GH_HELP_URL, "commands": list(GH_HELP_COMMANDS)}}

    @staticmethod
    def _plain_check_problem(problem: Any) -> Optional[str]:
        """botport's check problem for the page: None while it simply has not been verified yet (the row says so
        itself), else one plain sentence ending in what to do."""
        text = safe.clean_summary(problem, 200)
        if not text or "has not been checked yet" in text or "has not been verified yet" in text:
            return None
        if "writable by" in text or "owned by another" in text or "is a link" in text or "symbolic link" in text:
            return ("botport sits where other users of this computer could change it. Choose the downloaded "
                    "file again so Hermes keeps its own copy.")
        return text.rstrip(".") + ". Press Verify botport."

    def setup_brief(self) -> dict:
        s = self.setup_state()
        return {"ready": s["ready"], "grok_app_found": s["grok_app"]["found"], "helper_found": s["helper"]["found"],
                "helper_checked": s["check"]["done"], "problem": s["problem"], "cause": s["cause"]}

    # ---- the file the owner downloaded -----------------------------------------------------------------------------
    def choose_helper(self, path: Any = None, from_downloads: Any = False) -> dict:
        """The owner picked the downloaded file on the Twins page (`path`: the `botport` program itself, or the
        release archive that holds it), or pressed "Use it" on the download found in Downloads
        (`from_downloads=True`). Returns setup_state(). See the module text for the order of checks."""
        if not isinstance(from_downloads, bool):
            raise BadInput(BAD_REQUEST)
        if config.get("botport_path"):
            raise BadInput("The plugin's settings already name where botport is. Clear that setting first, then "
                           "choose the file again.")
        tag = self.release_tag()
        if from_downloads:
            if path is not None:
                raise BadInput(BAD_REQUEST)
            download = pins.download_for(tag)
            cands = download_candidates(download["file_name"]) if download else []
            if not cands:
                raise EngineError("not_found", "The download is not in Downloads any more.",
                                  "Press Download botport again.")
            first: Optional[EngineError] = None
            for _shown, src in cands:  # newest first; an older good copy still counts
                try:
                    return self._take_file(src, tag)
                except EngineError as exc:
                    first = first or exc
            raise first  # type: ignore[misc]
        if not isinstance(path, str) or not path.strip() or len(path) > 4096 or "\x00" in path:
            raise not_botport()
        src = Path(path.strip()).expanduser()
        if not src.is_absolute():
            raise not_botport()
        return self._take_file(src, tag)

    @staticmethod
    def _read_chosen(src: Path) -> bytes:
        """The chosen file's bytes. It is opened first (never through a link, and without waiting: a pipe put
        there in place of the file cannot hold the request up), and what is then checked (a plain file of sane
        size) is the opened file itself, not the name, so nothing can be swapped in between the look and the read."""
        try:
            # O_NONBLOCK: a pipe does not wait for a writer; O_NOCTTY: a terminal device is never taken over
            fd = _osflags.open_nofollow(str(src), os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
                                        | getattr(os, "O_NOCTTY", 0))
        except OSError as exc:
            # a link at the name: ELOOP (EMLINK on some systems)
            if exc.errno in (errno.ELOOP, errno.EMLINK):
                raise BadInput("Choose the file itself, not a shortcut to it.")
            if exc.errno in (errno.ENOENT, errno.ENOTDIR):
                raise BadInput("That file is not there any more. Choose the downloaded file again.")
            if exc.errno in (errno.ENXIO, errno.ENODEV, errno.EOPNOTSUPP):  # a socket or a device, not a file
                raise not_botport()
            if exc.errno in (errno.EISDIR, errno.EACCES) and src.is_dir():  # Windows cannot open a folder
                raise not_botport()
            raise BadInput("Hermes could not read that file. Choose the downloaded file again.")
        fh = None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):  # a folder, a pipe, a device
                raise not_botport()
            if st.st_size > HELPER_CAP:
                raise BadInput(TOO_LARGE)
            fh = os.fdopen(fd, "rb")
            data = fh.read(HELPER_CAP + 1)
        except OSError:
            raise BadInput("Hermes could not read that file. Choose the downloaded file again.")
        finally:
            with contextlib.suppress(OSError):
                fh.close() if fh is not None else os.close(fd)
        if len(data) > HELPER_CAP:
            raise BadInput(TOO_LARGE)
        return data

    def _take_file(self, src: Path, tag: str) -> dict:
        data = self._read_chosen(src)
        archive_sha = None
        if data[:2] == b"\x1f\x8b" or src.name.lower().endswith((".tar.gz", ".tgz", ".tar")):
            archive_sha = hashlib.sha256(data).hexdigest()
            data = program_from_archive(data)
        if not data.startswith(_PROGRAM_MAGIC):
            raise not_botport()
        pinned = pins.known(tag)
        if pinned:
            # verified here, before anything is kept or run: the program's bytes (or the archive they came in)
            # must be exactly a file of the pinned release, and the one for this kind of computer
            target = pins.program_target(hashlib.sha256(data).hexdigest(), tag) or pins.archive_target(archive_sha, tag)
            if target is None:
                raise EngineError("not_genuine", f"That file is not the genuine botport {tag.lstrip('v')}.",
                                  FRESH_DOWNLOAD_HINT)
            if target != pins.platform_target():
                raise EngineError("not_genuine", "That is botport for another kind of computer.",
                                  "Press Download botport to get the one for this computer, then choose the new file.")
        dest = self.chosen_helper()
        try:
            store.ensure_dir(dest.parent)
            tfd, tmp = tempfile.mkstemp(prefix=".botport-", dir=str(dest.parent))
            try:
                with os.fdopen(tfd, "wb") as out:
                    _osflags.make_private(out.fileno(), 0o700)
                    out.write(data)
                    out.flush()
                    os.fsync(out.fileno())
                _osflags.replace(tmp, dest)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
                raise
        except OSError:
            raise EngineError("unsafe_folder", "Hermes could not keep a copy of botport in its own data folder.",
                              "Run hermes twin doctor to see why.")
        self._botport = None
        self.invalidate()
        if pinned:
            # record the check (the kept copy is hashed again there, then asked for its version)
            bp = self.botport()
            try:
                if bp is None:
                    raise EngineError("not_found", "botport is not on this computer yet.", INSTALL_HINT)
                bp.trust(tag=tag)
            except EngineError:
                with contextlib.suppress(OSError):  # a file that cannot be used here is not kept either
                    os.unlink(dest)
                self._botport = None
                self.invalidate()
                raise
        self.emit("helper", None, {"chosen": True, "verified": bool(pinned)})
        return self.setup_state()

    # ---- verify botport (explicit owner step) -------------------------------------------------------------------
    @staticmethod
    def release_tag(tag: Any = None) -> str:
        """The botport release this install expects: `tag` (CLI --tag / API), else the botport_release_tag
        setting, else EXPECTED_BOTPORT_TAG. Anything not like v0.2.0 is refused."""
        if tag is None or tag == "":
            cfg = config.settings()
            if cfg.get("botport_release_tag_invalid"):
                raise BadInput("The botport release in the plugin's settings must look like v0.2.0.")
            tag = cfg.get("botport_release_tag") or EXPECTED_BOTPORT_TAG
        if not valid_tag(tag):
            raise BadInput("The botport release must look like v0.2.0.")
        return tag

    def trust_botport(self, allow_unattested: Any = False, tag: Any = None, method: Any = None,
                      quick: bool = False) -> dict:
        """Verify botport. `method=None`: the pinned release table first (no GitHub tool needed; covers a botport
        found on PATH or named in the settings), then GitHub's build record only if its tool is there. `method="gh"`
        asks for GitHub's build record on purpose. `allow_unattested=True` is the owner's explicit, recorded opt-out
        for a local build. The answer carries `method` (pinned | gh | local) and the new setup state.
        `quick=True` (the Twins page's plain ask): an answer in seconds and in the owner's words; when this plugin
        version has no table for the release the error is `not_pinned`, never the name of a developer's tool."""
        if not isinstance(allow_unattested, bool) or method not in (None, "", "gh", "pinned"):
            raise BadInput(BAD_REQUEST)
        tag = self.release_tag(tag)
        bp = self.botport()
        if bp is None:
            raise EngineError("not_found", "botport is not on this computer yet.", INSTALL_HINT)
        if quick is True and not allow_unattested:
            out = bp.trust(allow_unattested=False, tag=tag, method=method or None, quick=True)
        else:
            out = bp.trust(allow_unattested=allow_unattested, tag=tag, method=method or None)
        self.invalidate()
        view = self._trust_view(out)
        view["setup"] = self.setup_state()
        return view
