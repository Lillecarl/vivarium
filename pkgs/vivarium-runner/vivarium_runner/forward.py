"""Turning port-forward rules into passt arguments.

passt is the only path into a guest from the host, and its forwards are
fixed for the life of the process: ``conf_ports()`` binds every socket
while parsing options, there is no control socket, and the ``auto`` mode
that watches ``/proc/net/tcp`` is pasta-only -- pasta shares the target
namespace's ``/proc`` and passt does not.  Adding a forward to a running
passt therefore means restarting it, which drops every established
connection, including the ssh session you were in when you started the
service you wanted to reach.

So the forwards are decided before the guest boots, and the way to avoid
having to guess them is to make them cheap enough to take all at once.
passt's port spec accepts a bind address, and a spec made only of
exclusions is parsed in "weak" mode, where a port that will not bind is
skipped instead of fatal.  ``-t 127.0.0.2/~32768-60999`` is thus every
non-ephemeral port, bound on one address, in about a third of a second
and 17 MB -- so a guest with an address to itself has all of its ports
forwarded before it has decided which ones it wants, and two guests
never collide because they are on different addresses.

What does not work, and is why :func:`probe` exists: a rule on
``0.0.0.0`` conflicts with every other guest's wide range, one port at a
time, and passt reports that as a bare ``Address already in use`` and
exits.  An overlapping pair of specs within one instance is fatal the
same way, which is why the privileged block below is excluded from the
wide range rather than simply added alongside it.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass, replace
from pathlib import Path

ALL = "all"
"""``ports`` value meaning every port passt is willing to bind."""

_UNPRIVILEGED_START = Path("/proc/sys/net/ipv4/ip_unprivileged_port_start")
_LOCAL_PORT_RANGE = Path("/proc/sys/net/ipv4/ip_local_port_range")

_DEFAULT_UNPRIVILEGED_START = 1024
_DEFAULT_EPHEMERAL = (32768, 60999)
"""What passt's own ``fwd_probe_ephemeral()`` falls back to (RFC 6335)."""

_LOOPBACK_FIRST = 2
_LOOPBACK_LAST = 254
"""``127.0.0.2`` through ``127.0.0.254``.  All of ``127.0.0.0/8`` is on
``lo`` without anyone having to configure it, so a guest can be given an
address of its own with no privileges and no setup -- and 127.0.0.1 is
left alone, because the host's own services are there."""

_SENTINEL_PORT = 65534
"""Probed to find out whether a wide rule already owns an address.  Any
non-ephemeral port would do; this one is unlikely to be a real service
that we mistake for another guest."""


class ForwardError(Exception):
    """A forward cannot be set up: a collision, or nowhere to put it."""


@dataclass(frozen=True)
class PortMap:
    """One host port and the guest port it reaches."""

    host: int
    guest: int

    @classmethod
    def from_json(cls, data: dict | int) -> PortMap:
        if isinstance(data, int):
            return cls(host=data, guest=data)
        return cls(host=data["host"], guest=data["guest"])

    def __str__(self) -> str:
        return f"{self.host}" if self.host == self.guest else f"{self.host}:{self.guest}"


@dataclass(frozen=True)
class Rule:
    """One ``(address, ports)`` pair to hand passt.

    ``address`` is None until :func:`resolve` picks one; ``ports`` is
    either :data:`ALL` or a tuple of :class:`PortMap`.
    """

    address: str | None = None
    ports: str | tuple[PortMap, ...] = ALL
    protocols: tuple[str, ...] = ("tcp",)
    remap_privileged: bool = True
    privileged_offset: int = 10000

    @classmethod
    def from_json(cls, data: dict) -> Rule:
        ports = data.get("ports", ALL)
        return cls(
            address=data.get("address"),
            ports=(
                ALL
                if ports == ALL
                else tuple(PortMap.from_json(port) for port in ports)
            ),
            protocols=tuple(data.get("protocols", ["tcp"])),
            remap_privileged=data.get("remapPrivileged", True),
            privileged_offset=data.get("privilegedOffset", 10000),
        )

    @property
    def wide(self) -> bool:
        return self.ports == ALL

    def host_ports(self) -> list[int]:
        """Host ports this rule needs bound, for probing.

        A wide rule needs the whole non-ephemeral range and cannot be
        checked exhaustively; :data:`_SENTINEL_PORT` stands in for it,
        which is enough to notice another guest already living here.
        """
        if self.wide:
            return [_SENTINEL_PORT]
        return [port.host for port in self.ports]


# ── the host's own limits ──────────────────────────────────────────


def _read_ints(path: Path, fallback: tuple[int, ...]) -> tuple[int, ...]:
    try:
        return tuple(int(field) for field in path.read_text().split())
    except (OSError, ValueError):
        return fallback


def unprivileged_start() -> int:
    """Lowest port the host will let an unprivileged process bind.

    1024 unless someone has lowered ``net.ipv4.ip_unprivileged_port_start``,
    which is the whole reason guest port 22 is not simply host port 22.
    """
    return _read_ints(_UNPRIVILEGED_START, (_DEFAULT_UNPRIVILEGED_START,))[0]


def ephemeral_range() -> tuple[int, int]:
    """The host's ephemeral port range, as passt probes it."""
    values = _read_ints(_LOCAL_PORT_RANGE, _DEFAULT_EPHEMERAL)
    return (values[0], values[1]) if len(values) == 2 else _DEFAULT_EPHEMERAL


# ── resolution ─────────────────────────────────────────────────────


def _remap(rule: Rule, start: int) -> tuple[Rule, list[str]]:
    """Move host ports the kernel will not give us out of the way.

    Returns the rule and whatever the user should be told about it.  A
    remap is never silent: the port you asked for is not the port you
    get, and finding that out from a refused connection is worse than
    reading it at boot.
    """
    if start <= 1:
        return rule, []

    offset = rule.privileged_offset
    if rule.wide:
        if not rule.remap_privileged:
            return rule, [
                f"ports below {start} are not forwarded "
                f"(remapPrivileged is off)"
            ]
        return rule, [
            f"ports 1-{start - 1} need privileges the host will not give us; "
            f"reaching them on {offset + 1}-{offset + start - 1} instead"
        ]

    kept: list[PortMap] = []
    notes: list[str] = []
    for port in rule.ports:
        if port.host >= start:
            kept.append(port)
        elif rule.remap_privileged:
            moved = replace(port, host=port.host + offset)
            kept.append(moved)
            notes.append(
                f"host port {port.host} needs privileges we do not have; "
                f"forwarding {moved.host} -> guest {port.guest} instead"
            )
        else:
            notes.append(
                f"host port {port.host} needs privileges we do not have "
                f"and remapPrivileged is off; guest {port.guest} is unreachable"
            )
    return replace(rule, ports=tuple(kept)), notes


def _bind(address: str, port: int) -> socket.socket:
    """Bind one port the way passt will, so a probe means what it says."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # passt sets this on every listening socket (util.c:109), so probing
    # without it would call a TIME_WAIT leftover a collision.
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((address, port))
    except OSError:
        sock.close()
        raise
    return sock


def _probe(address: str, ports: list[int]) -> int | None:
    """The first of *ports* that will not bind on *address*, if any."""
    held: list[socket.socket] = []
    try:
        for port in ports:
            try:
                held.append(_bind(address, port))
            except OSError:
                return port
        return None
    finally:
        for sock in held:
            sock.close()


def _assign(rules: list[Rule], taken: set[str]) -> str:
    """Find a loopback address free for every rule that wants one."""
    wanted = sorted({port for rule in rules for port in rule.host_ports()})
    for octet in range(_LOOPBACK_FIRST, _LOOPBACK_LAST + 1):
        address = f"127.0.0.{octet}"
        if address in taken:
            continue
        if _probe(address, wanted) is None:
            return address
    raise ForwardError(
        f"no free address in 127.0.0.{_LOOPBACK_FIRST}-{_LOOPBACK_LAST} "
        f"for ports {', '.join(str(port) for port in wanted)}"
    )


def resolve(
    rules: list[Rule], *, taken: set[str], start: int | None = None
) -> tuple[list[Rule], list[str]]:
    """Give every rule a concrete address and a bindable set of ports.

    *taken* is the addresses already handed to other guests in this run;
    it is updated in place.  Callers must resolve every guest before
    booting any of them, or two guests race for the same address.
    """
    if start is None:
        start = unprivileged_start()

    unaddressed = [rule for rule in rules if rule.address is None]
    if unaddressed:
        address = _assign(unaddressed, taken)
        taken.add(address)
        rules = [
            replace(rule, address=address) if rule.address is None else rule
            for rule in rules
        ]

    resolved: list[Rule] = []
    notes: list[str] = []
    for rule in rules:
        rule, said = _remap(rule, start)
        if rule.wide or rule.ports:
            resolved.append(rule)
        notes.extend(f"{rule.address}: {note}" for note in said)
    return resolved, notes


def probe(rules: list[Rule]) -> None:
    """Raise if anything a rule asks for is already bound.

    passt would find this out for itself and exit with a bare ``Address
    already in use``, naming neither the guest nor the rule.
    """
    for rule in rules:
        assert rule.address is not None, "probe() runs after resolve()"
        clash = _probe(rule.address, rule.host_ports())
        if clash is not None:
            what = "every port" if rule.wide else f"port {clash}"
            raise ForwardError(
                f"cannot forward {what} on {rule.address}: "
                f"{rule.address}:{clash} is already bound"
            )


# ── passt arguments ────────────────────────────────────────────────


def _specs(rule: Rule, start: int) -> list[str]:
    """The one or two passt port specs a rule turns into."""
    if not rule.wide:
        return [
            f"{rule.address}/"
            + ",".join(str(port) for port in sorted(rule.ports, key=lambda p: p.host))
        ]

    low, high = ephemeral_range()
    # An exclusion-only spec is what puts passt in weak mode, where a
    # port it cannot bind is skipped rather than fatal.  Excluding the
    # ephemeral range costs nothing: passt excludes it anyway.
    excluded = [f"~{low}-{high}"]

    remapped: list[str] = []
    if start > 1 and rule.remap_privileged:
        offset = rule.privileged_offset
        first, last = offset + 1, offset + start - 1
        # The wide range would otherwise bind these itself, and passt
        # treats a second spec over ports it already has as fatal.
        excluded.append(f"~{first}-{last}")
        remapped.append(f"{rule.address}/{first}-{last}:1-{start - 1}")

    return [f"{rule.address}/" + ",".join(excluded)] + remapped


def to_args(rules: list[Rule], start: int | None = None) -> list[str]:
    """Bridge arguments carrying *rules*, as ``--tcp-ports``/``--udp-ports``."""
    if start is None:
        start = unprivileged_start()
    flags = {"tcp": "--tcp-ports", "udp": "--udp-ports"}
    args: list[str] = []
    for rule in rules:
        for spec in _specs(rule, start):
            for protocol in rule.protocols:
                args += [flags[protocol], spec]
    return args


def reachable(rules: list[Rule], guest_port: int, start: int) -> list[str]:
    """Where a guest port can be reached from the host, as ``addr:port``.

    Empty if nothing forwards it, which is the interesting answer: it is
    what tells you a service came up somewhere the host cannot see.
    """
    low, high = ephemeral_range()
    found: list[str] = []
    for rule in rules:
        if rule.wide:
            if low <= guest_port <= high:
                continue
            if guest_port >= start:
                found.append(f"{rule.address}:{guest_port}")
            elif rule.remap_privileged:
                found.append(f"{rule.address}:{guest_port + rule.privileged_offset}")
        else:
            found += [
                f"{rule.address}:{port.host}"
                for port in rule.ports
                if port.guest == guest_port
            ]
    return found


# The guest's own address on its uplink, and the two addresses passt
# answers on behind it.
#
# passt shares the *host's* address by default: a guest is given the
# host's real IPv4, netmask and router, so the host's LAN is on-link to
# the guest and a test's environment depends on where the host is. On a
# hosted machine that LAN has other people's servers on it.
#
# 10.0.2.0/24 instead, which is the range QEMU's own user-mode network
# has used for twenty years, so it is the one a reader already knows --
# and the one modules/k8s.nix has described as the guest's uplink all
# along.
#
# Every guest gets the same address, and that is right: each has a passt
# of its own, none of them shares a link, and two guests that must reach
# each other do it on `vec1` with addresses a test chose. What makes a
# guest reachable from the host is its own 127.0.0.x, which forward.py
# hands out per guest and which is unaffected by any of this.
UPLINK_ADDRESS = "10.0.2.15"
UPLINK_PREFIX = 24
UPLINK_GATEWAY = "10.0.2.2"


# Where passt answers DNS for the guest.  Free in the uplink /24, and the
# address QEMU's own user-mode networking has used for this for years.
UPLINK_DNS = "10.0.2.3"

# Where passt sends a query it is asked to forward.  A constant, and not
# the host's own resolver: reading the host's nameserver files gave a
# different answer on a laptop and on a GitHub runner, and the runner's
# answer was the 127.0.0.53 stub, which no guest can reach.  Two rounds of
# CI went on deriving this address before the derivation itself was the
# thing to remove.
#
# The guests do not depend on it.  modules/guest.nix names its own
# resolvers and drops the ones DHCP offers, so this covers only something
# that asks passt directly.
UPLINK_UPSTREAM_DNS = "1.1.1.1"

# For a network that does not reach 1.1.1.1.  The one knob, and the only
# thing here that looks at the host at all.
ENV_DNS = "UML_DNS_HOST"


OFFLINE_OUTBOUND = "127.0.0.1"
"""What passt binds its outbound sockets to when a run asks for no
internet.

Loopback, so a connection the guest makes to anything off the host has no
route and fails at once.  Everything else about the uplink is unchanged
-- the guest still gets its address by DHCP, still hears about a gateway
and a resolver, and the host's way *in* still works -- because the guest
must look the same as it does in a sandbox, where passt runs and finds no
usable route either.

Not simply leaving passt out: that takes vec0 away, so the guest has no
uplink interface at all and no inbound forwards.  A test that reaches an
API server through a forward would then fail for a reason that is not the
one being reproduced."""


def uplink_args(offline: bool = False) -> list[str]:
    """passt arguments giving the guest an address of its own, and DNS.

    Both backends pass these -- QEMU straight to passt, UML through
    `uml-passt-bridge`'s `--passt` -- so that a guest's uplink looks the
    same whichever machine it turned out to be.

    `--dns-host` is named rather than left to passt's default, which is
    the first nameserver in the host's `/etc/resolv.conf`.  On a
    systemd-resolved host that is `127.0.0.53`, and passt then advertises
    it to the guest, where it means the guest's own resolver.  That is how
    every guest CI job came to fail on a runner with `lookup
    registry.k8s.io: no such host`.
    """
    upstream = os.environ.get(ENV_DNS) or UPLINK_UPSTREAM_DNS
    # Said out loud, because passt's own banner is not in a QEMU run's
    # output and a guest that cannot resolve gives no clue which of the
    # two addresses is the wrong one.
    print(f"[uplink] DNS on {UPLINK_DNS}, forwarded to {upstream}", flush=True)
    if offline:
        print(
            "[uplink] offline: outbound bound to"
            f" {OFFLINE_OUTBOUND}, so the guest reaches nothing off this host",
            flush=True,
        )
    return [
        "--address", UPLINK_ADDRESS,
        "--netmask", str(UPLINK_PREFIX),
        "--gateway", UPLINK_GATEWAY,
        "--dns-forward", UPLINK_DNS,
        "--dns-host", upstream,
        *(["--outbound", OFFLINE_OUTBOUND] if offline else []),
    ]
