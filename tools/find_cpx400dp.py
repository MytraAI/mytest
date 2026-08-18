"""Find a CPX400DP on the network when its address has moved.

The instrument reports DHCP but sits on a segment with no DHCP server, so it
self-assigns a link-local address, and that address changes if a DHCP server
appears or on a collision. Nothing announces the move: the driver just fails to
connect, or - if something else has taken the address - refuses what answers
because `*IDN?` is not a CPX400DP.

This opens the SCPI port on every address in a network and asks each thing that
answers who it is, so what it reports is the instrument's own identity rather
than "something is listening here". Any TTi instrument found is printed, since
knowing a *different* unit answers at the expected address is as useful as
finding the right one.

    python -m tools.find_cpx400dp
    python -m tools.find_cpx400dp --network 169.254.0.0/16
    python -m tools.find_cpx400dp --host 169.254.229.133   # check one address

With no arguments it scans the networks this machine has an address on: the
whole 169.254.0.0/16 for a link-local address, since the instrument self-assigns
anywhere in it, and the surrounding /24 otherwise. A /16 is 65534 addresses and
takes a couple of minutes, most of it waiting on addresses with nothing there;
a /24 is done in seconds. Raise --concurrency to trade sockets for time.

Lives in tools/ with the other operator-facing entry points: nothing in the
test architecture depends on it, and it talks to the instrument directly rather
than through a driver process.
"""
from __future__ import annotations

import argparse
import asyncio
import ipaddress
import socket
import sys
from typing import Iterable, Iterator, List, Optional, Tuple

from hardware.cpx400dp.transport import DEFAULT_PORT
from protocol import asyncio_compat

CONNECT_TIMEOUT_S = 0.5
"""How long one address gets to accept a connection. Short: on the same segment
an instrument answers in milliseconds, and this is paid once per address that
isn't there."""

IDENTITY_TIMEOUT_S = 2.0
"""How long something that *did* accept gets to answer `*IDN?`. Generous - it
is paid only by addresses that are actually listening, and a device mid-command
can be slow to reply."""

DEFAULT_CONCURRENCY = 512
"""Addresses probed at once. Each is one socket being opened, so this is what
decides whether a /16 takes one minute or ten."""

PROGRESS_INTERVAL_S = 2.0

MODEL = "CPX400DP"


def local_networks() -> List[ipaddress.IPv4Network]:
    """The networks this machine can reach directly, from its own addresses.

    A link-local address means the whole 169.254.0.0/16, because that is where
    the instrument self-assigns; anything else is assumed to be a /24, which is
    what a stand on a small private segment will be. Neither is read from the
    interface's real netmask - pass --network when that assumption is wrong."""
    networks: List[ipaddress.IPv4Network] = []
    for address in _local_addresses():
        if address.is_loopback:
            continue
        if address.is_link_local:
            network = ipaddress.IPv4Network("169.254.0.0/16")
        else:
            network = ipaddress.IPv4Network(f"{address}/24", strict=False)
        if network not in networks:
            networks.append(network)
    return networks


def _local_addresses() -> List[ipaddress.IPv4Address]:
    """This machine's own IPv4 addresses.

    Two sources, because neither is complete on its own: resolving the hostname
    misses interfaces that aren't in DNS, and the UDP probe below reports only
    the address that would be used to reach one particular destination. The
    probe sends nothing - connecting a UDP socket just selects a route."""
    found: List[ipaddress.IPv4Address] = []

    def remember(text: str) -> None:
        try:
            address = ipaddress.IPv4Address(text)
        except ValueError:
            return
        if address not in found:
            found.append(address)

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            remember(info[4][0])
    except socket.gaierror:
        pass

    for destination in ("169.254.1.1", "8.8.8.8"):
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect((destination, 9))
            remember(probe.getsockname()[0])
        except OSError:
            pass
        finally:
            probe.close()

    return found


async def identify(host: str, port: int, connect_timeout_s: float) -> Optional[str]:
    """Ask whatever is at `host:port` who it is, or None if nothing answers.

    Returns the raw `*IDN?` reply, or an empty string when the connection was
    accepted but nothing came back - which is itself worth reporting, since the
    instrument answers nothing at all to a command it does not implement."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), connect_timeout_s
        )
    except (OSError, asyncio.TimeoutError):
        return None
    try:
        writer.write(b"*IDN?\n")
        await writer.drain()
        reply = await asyncio.wait_for(reader.readline(), IDENTITY_TIMEOUT_S)
        return reply.decode("ascii", errors="replace").strip()
    except (OSError, asyncio.TimeoutError):
        return ""
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


async def scan(
    addresses: Iterable[str],
    port: int,
    concurrency: int,
    connect_timeout_s: float,
    total: Optional[int] = None,
) -> List[Tuple[str, str]]:
    """Probe every address, at most `concurrency` at a time.

    Workers pull from a shared iterator rather than a task being created per
    address, so a /16 costs the same memory as a /24."""
    remaining: Iterator[str] = iter(addresses)
    found: List[Tuple[str, str]] = []
    scanned = 0

    async def worker() -> None:
        nonlocal scanned
        while True:
            try:
                host = next(remaining)
            except StopIteration:
                return
            identity = await identify(host, port, connect_timeout_s)
            scanned += 1
            if identity is not None:
                found.append((host, identity))
                print(f"  {host}\t{identity or '(no reply to *IDN?)'}", flush=True)

    async def progress() -> None:
        while True:
            await asyncio.sleep(PROGRESS_INTERVAL_S)
            if total:
                print(f"  ... {scanned}/{total} addresses", flush=True)
            else:
                print(f"  ... {scanned} addresses", flush=True)

    reporter = asyncio.create_task(progress(), name="find-cpx400dp-progress")
    try:
        workers = min(concurrency, total or concurrency)
        await asyncio.gather(*(worker() for _ in range(workers)))
    finally:
        reporter.cancel()
    return found


async def main(args: argparse.Namespace) -> int:
    if args.host:
        targets = [args.host]
        total = 1
        print(f"checking {args.host}:{args.port}", flush=True)
    else:
        networks = (
            [ipaddress.IPv4Network(args.network)] if args.network else local_networks()
        )
        if not networks:
            print("no usable local network found - pass --network", file=sys.stderr)
            return 1
        targets = [str(h) for network in networks for h in network.hosts()]
        total = len(targets)
        for network in networks:
            print(f"scanning {network} on port {args.port}", flush=True)

    found = await scan(targets, args.port, args.concurrency, args.connect_timeout, total)

    supplies = [(host, identity) for host, identity in found if MODEL in identity.upper()]
    if not found:
        print(f"\nnothing answered on port {args.port}")
        return 1
    if not supplies:
        print(f"\nfound {len(found)} device(s), none a {MODEL}")
        return 1

    print(f"\nfound {len(supplies)} {MODEL}(s):")
    for host, identity in supplies:
        print(f"  {host}\t{identity}")
    print("\nuse it with:")
    print(f"  python -m hardware.cpx400dp.main --host {supplies[0][0]}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--network", default=None, help="network to scan, e.g. 169.254.0.0/16")
    parser.add_argument("--host", default=None, help="check this one address instead of scanning")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--connect-timeout", type=float, default=CONNECT_TIMEOUT_S)
    sys.exit(asyncio_compat.run(main(parser.parse_args())))
