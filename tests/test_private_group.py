"""Linux accounts with a private group and umask 002 (Ubuntu, Fedora and others): Hermes makes `profiles` and
each bot's folder group-writable there, and a twin must still be writable on such a computer.

The rule (twin/store.py): group-writable is accepted only for the folders Hermes itself makes for its bots, and
only when the group is the user's own private group. Writable by others, a shared group, or the plugin's own
data folders: refused as before.
"""
from __future__ import annotations

import os
import types

import pytest

from twin_testlib import central, home, plugin  # noqa: F401
from hermes_plugins.grok_bot_twin.twin import hermes_side as hs, store


@pytest.fixture(autouse=True)
def _fresh_cache():
    store._private_groups.clear()
    yield
    store._private_groups.clear()


def _accounts(monkeypatch, *, group_name="pat", members=(), others=(), primary=None, folder=None):
    """A stand-in account database: the current user is 'pat' with primary group `primary` (default: the group
    new folders get here; on macOS that is the parent folder's group, not the user's), that group is called
    `group_name` and lists `members`; `others` are (name, uid, gid) of more users."""
    uid = os.getuid()
    gid = os.stat(folder).st_gid if folder is not None else os.getgid()
    primary = gid if primary is None else primary
    me = types.SimpleNamespace(pw_name="pat", pw_uid=uid, pw_gid=primary)
    users = [me] + [types.SimpleNamespace(pw_name=n, pw_uid=u, pw_gid=g) for n, u, g in others]

    def getgrgid(g):
        if g != gid:
            raise KeyError(g)
        return types.SimpleNamespace(gr_name=group_name, gr_gid=gid, gr_mem=list(members))

    monkeypatch.setattr(store, "_pwd", types.SimpleNamespace(getpwuid=lambda u: me, getpwall=lambda: users))
    monkeypatch.setattr(store, "_grp", types.SimpleNamespace(getgrgid=getgrgid))
    return gid


def test_a_group_is_private_only_when_it_is_the_users_own_and_nobody_else_is_in_it(monkeypatch):
    gid = _accounts(monkeypatch)
    assert store._private_group(gid) is True
    store._private_groups.clear()
    gid = _accounts(monkeypatch, members=("pat",))  # listing yourself changes nothing
    assert store._private_group(gid) is True
    for kwargs in ({"group_name": "staff"},                       # a shared group (macOS: staff)
                   {"members": ("pat", "sam")},                   # someone else was added to it
                   {"others": (("sam", os.getuid() + 1, os.getgid()),)},  # someone else has it as primary group
                   {"primary": os.getgid() + 1}):                 # not this user's primary group
        store._private_groups.clear()
        gid = _accounts(monkeypatch, **kwargs)
        assert store._private_group(gid) is False, kwargs
    store._private_groups.clear()
    assert store._private_group(os.getgid() + 12345) is False  # an unknown group
    monkeypatch.setattr(store, "_pwd", None)
    store._private_groups.clear()
    assert store._private_group(os.getgid()) is False  # no account database: never assumed


def _umask_002_profiles(home):
    """What Hermes leaves behind under umask 002: profiles/ and one bot, both rwxrwxr-x."""
    prof = home / "profiles" / "test-scout"
    prof.mkdir(parents=True)
    os.chmod(home / "profiles", 0o775)
    os.chmod(prof, 0o775)
    return prof


def test_with_a_private_group_a_twin_in_group_writable_profiles_can_be_written(home, monkeypatch):
    _accounts(monkeypatch, folder=home)
    prof = _umask_002_profiles(home)
    assert hs.write_instructions(prof, "Be brief.") == ""
    assert "Be brief." in (prof / "SOUL.md").read_text(encoding="utf-8")
    assert oct((prof / "SOUL.md").stat().st_mode & 0o777) == "0o600"
    # a new bot next to it (what a following computer does for a new twin) is made owner-only
    new = store.ensure_dir(home / "profiles" / "test-quill")
    assert (new.stat().st_mode & 0o777) == 0o700
    sub = store.ensure_dir(new / "plugins" / "grok-bot-twin")
    assert (sub.stat().st_mode & 0o777) == 0o700


def test_with_a_shared_group_group_writable_profiles_stay_refused(home, monkeypatch):
    _accounts(monkeypatch, group_name="staff", folder=home)
    prof = _umask_002_profiles(home)
    with pytest.raises(store.UnsafePath) as err:
        hs.write_instructions(prof, "Be brief.")
    assert "writable by group or others" in str(err.value)
    assert not (prof / "SOUL.md").exists()
    with pytest.raises(store.UnsafePath):
        store.ensure_dir(home / "profiles" / "test-quill")


@pytest.mark.parametrize("mode", [0o777, 0o757, 0o707])
def test_writable_by_others_is_refused_whatever_the_group(home, monkeypatch, mode):
    _accounts(monkeypatch, folder=home)
    prof = _umask_002_profiles(home)
    for folder in (home / "profiles", prof):
        os.chmod(folder, mode)
        try:
            with pytest.raises(store.UnsafePath):
                hs.write_instructions(prof, "Be brief.")
        finally:
            os.chmod(folder, 0o775)


@pytest.mark.parametrize("where", ["", "plugin-data", "plugin-data/grok-bot-twin"])
def test_the_plugins_own_folders_stay_owner_only_even_with_a_private_group(home, monkeypatch, where):
    _accounts(monkeypatch, folder=home)
    store.central_dir()
    d = home / where if where else home
    os.chmod(d, 0o770)
    try:
        with pytest.raises(store.UnsafePath):
            store.central_dir()
    finally:
        os.chmod(d, 0o700)
    # and a folder deeper inside a bot is not one of the two Hermes makes with the umask
    deep = home / "profiles" / "test-scout" / "skills"
    deep.mkdir(parents=True)
    os.chmod(deep, 0o775)
    with pytest.raises(store.UnsafePath):
        store.ensure_dir(deep / "x")


@pytest.mark.skipif(os.name != "posix", reason="POSIX accounts")
def test_on_this_computer_the_real_answer_matches_the_account(home):
    """No stand-ins: under umask 002 a twin is writable exactly when this account's group is private."""
    old = os.umask(0o002)
    try:
        prof = home / "profiles" / "test-scout"
        prof.mkdir(parents=True)
    finally:
        os.umask(old)
    assert (prof.stat().st_mode & 0o777) == 0o775
    if store._private_group(prof.stat().st_gid):
        hs.write_instructions(prof, "Be brief.")
        assert "Be brief." in (prof / "SOUL.md").read_text(encoding="utf-8")
    else:
        with pytest.raises(store.UnsafePath):
            hs.write_instructions(prof, "Be brief.")
