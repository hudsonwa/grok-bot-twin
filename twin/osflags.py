"""Open flags the plugin's safety checks depend on. Missing ones fail closed with a clear error instead of
silently turning into 0 (which would follow links)."""
from __future__ import annotations

import os


class UnsupportedPlatform(OSError):
    """This operating system lacks a flag the plugin needs to open files safely (macOS and Linux have both)."""


def _need(name: str) -> int:
    value = getattr(os, name, None)
    if not value:
        raise UnsupportedPlatform(f"unsupported platform: os.{name} is not available, so files cannot be opened "
                                  "safely (Grok Bot Twin supports macOS and Linux)")
    return value


def o_nofollow() -> int:
    return _need("O_NOFOLLOW")


def o_directory() -> int:
    return _need("O_DIRECTORY")
