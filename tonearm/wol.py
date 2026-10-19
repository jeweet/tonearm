"""Wake-on-LAN (pure Python, testable headless).

A "magic packet" — six 0xFF bytes, then the MAC address sixteen times — sent as a UDP
broadcast wakes a sleeping machine whose network card has Wake-on-LAN enabled. Tonearm
learns the Kodi machine's MAC address from Kodi itself while connected (the
``Network.MacAddress`` info label), so waking it later needs no setup.
"""
from __future__ import annotations

import ipaddress
import re
import socket

PORTS = (9, 7)          # the usual Wake-on-LAN ports (discard / echo)

_HEX = re.compile(r"[0-9a-fA-F]")


def normalize_mac(text: str | None) -> str | None:
    """``"AA-bb-cc-dd-ee-ff"``, ``"aabb.ccdd.eeff"``… → ``"AA:BB:CC:DD:EE:FF"``;
    None for anything that isn't a usable MAC address (including all zeros, which
    Kodi reports when it can't tell)."""
    if not text:
        return None
    digits = "".join(_HEX.findall(text))
    stripped = re.sub(r"[\s:.\-]", "", text)
    if len(digits) != 12 or len(stripped) != 12:
        return None
    if digits.strip("0") == "" or digits.lower() == "f" * 12:
        return None
    return ":".join(digits[i:i + 2] for i in range(0, 12, 2)).upper()


def magic_packet(mac: str) -> bytes:
    norm = normalize_mac(mac)
    if norm is None:
        raise ValueError(f"Not a MAC address: {mac!r}")
    return b"\xff" * 6 + bytes.fromhex(norm.replace(":", "")) * 16


def targets(host: str | None) -> list[str]:
    """Where to send: the general broadcast, plus — when Kodi's host is an IPv4
    address on a private network — its /24 broadcast (some routers and Wi-Fi access
    points pass only directed broadcasts on)."""
    out = ["255.255.255.255"]
    try:
        ip = ipaddress.ip_address((host or "").strip("[]"))
    except ValueError:
        return out
    if ip.version == 4 and ip.is_private:
        net = ipaddress.ip_network(f"{ip}/24", strict=False)
        out.append(str(net.broadcast_address))
    return out


def wake(mac: str, host: str | None = None, ports=PORTS) -> int:
    """Send the magic packet (a few times, to every target and port); returns how many
    were sent. Raises OSError when none could be (no network at all)."""
    packet = magic_packet(mac)
    sent, last_err = 0, None
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for _ in range(3):                    # UDP: a packet may get lost
            for addr in targets(host):
                for port in ports:
                    try:
                        sock.sendto(packet, (addr, port))
                        sent += 1
                    except OSError as err:
                        last_err = err
    if not sent:
        raise last_err or OSError("Wake-on-LAN packet not sent")
    return sent


__all__ = ["normalize_mac", "magic_packet", "targets", "wake"]
