"""Addresses and link codes for sharing twins between the owner's machines. Standard library only.

Which address the share server may listen on, and which addresses a follower may connect to:
- the Tailscale address (100.64.0.0/10), preferred when Tailscale is up;
- a private home-network address (10/8, 172.16/12, 192.168/16);
- loopback (tests, tunnels).
Never all interfaces, never a public address. The same rule is applied to the address a link code carries and to
a host the owner types, so a follower never talks to anything outside those ranges.

A link code is 12 characters from an alphabet of 31 with no look-alikes (no 0, 1, I, L or O), shown as
ABCD-EFGH-JKMN. Its 59 bits are: the source's IPv4 address (32), a 3-bit check of that address (so most typing
slips are caught before anything is contacted), and 24 random bits. It lasts 10 minutes, works once, and stops
working after 5 wrong tries.
"""
from __future__ import annotations

import hashlib
import ipaddress
import secrets
import socket
from typing import Any, Optional

DEFAULT_PORT = 18777
CODE_LEN = 12
CODE_TTL_S = 600
CODE_MAX_TRIES = 5
_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"  # 31: nobody has to tell 0 from O or 1 from I or L
_BASE = len(_ALPHABET)
_TAILSCALE = ipaddress.ip_network("100.64.0.0/10")
_PRIVATE = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
_SECRET_BITS = 24
_CHECK_BITS = 3  # 32 + 3 + 24 = 59 bits, which 12 characters of 31 just hold (31**12 > 2**59)


def kind_of(address: Any) -> Optional[str]:
    """"tailscale", "lan" or "loopback" for an IPv4 address the plugin may use; None for anything else."""
    try:
        ip = ipaddress.ip_address(str(address))
    except ValueError:
        return None
    if ip.version != 4:
        return None
    if ip.is_loopback:
        return "loopback"
    if ip in _TAILSCALE:
        return "tailscale"
    if any(ip in net for net in _PRIVATE):
        return "lan"
    return None


def _route_address(probe: str) -> Optional[str]:
    """This machine's own address on the interface that would carry traffic to `probe`. A UDP socket is
    "connected" (which sends nothing) and asked for its local end."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((probe, 9))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def tailscale_address() -> Optional[str]:
    addr = _route_address("100.100.100.100")
    return addr if addr and kind_of(addr) == "tailscale" else None


def lan_address() -> Optional[str]:
    for probe in ("192.0.2.1", "10.255.255.255", "192.168.255.255"):
        addr = _route_address(probe)
        if addr and kind_of(addr) == "lan":
            return addr
    return None


def share_address(configured: str = "") -> Optional[tuple]:
    """(address, kind) to listen on: the configured one when it is an allowed address of this machine's, else
    Tailscale, else the home network. None when there is nothing suitable."""
    if configured:
        kind = kind_of(configured)
        return (str(ipaddress.ip_address(configured)), kind) if kind else None
    for find in (tailscale_address, lan_address):
        addr = find()
        if addr:
            return addr, kind_of(addr)
    return None


def is_own_address(address: Any) -> bool:
    """Is this address one of this machine's own right now? (A socket can be bound only to an address the
    machine has; nothing is sent and nothing listens.) Asked before the share server's own secret is sent to "the
    serving process on this machine": after a move to another network that address may be somebody else's."""
    if not kind_of(address):
        return False
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((str(address), 0))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def resolve_host(host: Any) -> Optional[str]:
    """An address a follower may connect to, from what the owner typed (an address or a name on the home
    network / tailnet). None when it is not an allowed address."""
    if not isinstance(host, str) or not host.strip() or len(host) > 253:
        return None
    host = host.strip()
    if kind_of(host):
        return str(ipaddress.ip_address(host))
    try:
        ipaddress.ip_address(host)
        return None  # an address, but not an allowed one
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return None
    for info in infos:
        addr = info[4][0]
        if kind_of(addr):
            return addr
    return None


# ---- link codes -------------------------------------------------------------------------------------------------
def _check(addr_int: int) -> int:
    return hashlib.sha256(addr_int.to_bytes(4, "big")).digest()[0] & ((1 << _CHECK_BITS) - 1)


def _encode(value: int) -> str:
    out = []
    for _ in range(CODE_LEN):
        value, digit = divmod(value, _BASE)
        out.append(_ALPHABET[digit])
    return "".join(reversed(out))


def normalize_code(text: Any) -> Optional[str]:
    """The 12 code characters, upper case, without dashes or spaces; None when it is not a link code."""
    if not isinstance(text, str) or len(text) > 64:
        return None
    raw = "".join(ch for ch in text.upper() if ch not in " -\t\r\n–—")
    if len(raw) != CODE_LEN or any(ch not in _ALPHABET for ch in raw):
        return None
    return raw


def pretty_code(raw: str) -> str:
    return "-".join(raw[i:i + 4] for i in range(0, CODE_LEN, 4))


def make_code(address: str) -> str:
    """A fresh link code (12 characters, no dashes) that carries `address`."""
    addr_int = int(ipaddress.ip_address(address))
    value = (addr_int << (_CHECK_BITS + _SECRET_BITS)) | (_check(addr_int) << _SECRET_BITS) \
        | secrets.randbits(_SECRET_BITS)
    return _encode(value)


def code_address(text: Any) -> Optional[str]:
    """The address a link code carries; None when the text is not a link code, its check does not fit, or the
    address is not one the plugin may connect to."""
    raw = normalize_code(text)
    if raw is None:
        return None
    value = 0
    for ch in raw:
        value = value * _BASE + _ALPHABET.index(ch)
    addr_int = value >> (_CHECK_BITS + _SECRET_BITS)
    if addr_int >= 1 << 32:
        return None
    check = (value >> _SECRET_BITS) & ((1 << _CHECK_BITS) - 1)
    if check != _check(addr_int):
        return None
    address = str(ipaddress.ip_address(addr_int))
    return address if kind_of(address) else None
