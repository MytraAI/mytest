"""Every state channel a DUT publishes is seeded, for every DUT, automatically.

THE HAZARD. The telemetry engine fixes a wide file's header from the union of its
first HEADER_SAMPLE_FRAMES frames and drops any channel that first appears later.
A channel published tens of seconds into a run - a stopping distance, a slip, a
camera's verdict - is therefore absent from the recorded file while the run
reports a clean pass: the measurement missing, and nothing saying so. Each DUT's
channels.py seeds its state channels against exactly this.

WHY THIS IS ONE TEST AND NOT ONE PER DUT. The same check written per DUT is a
check somebody has to remember to write for the next device under test, which is
the same as not having it. This discovers the DUT packages instead, so a new one
is covered by existing when it is added, and a channel added to an old one is
covered by being published.

WHY IT READS SOURCE RATHER THAN RUNNING ANYTHING. A published channel's name is a
string literal at the point it is published, and finding those needs no hardware,
no testbed and no run. Anything computing a channel name at runtime is invisible
here and is meant to be - see the bound-status channels, which come from a
rulebook rather than from a literal and are documented as exempt.
"""
from __future__ import annotations

import importlib
import inspect
import pkgutil
import re

import pytest

import testcases

SET_STATE_LITERAL = re.compile(r'set_state\(\s*"([A-Za-z_][A-Za-z0-9_]*)"')
"""set_state("name", ...) - a literal name only. A computed one has no literal to find."""

DERIVED_KEY_LITERAL = re.compile(r'^\s*"([A-Za-z_][A-Za-z0-9_]*)":', re.M)
"""A key in the dict a derived_channels() implementation returns. Derived channels never
pass through set_state() at a call site, so the sweep above cannot see them."""

FIELD_CHANNEL_LITERAL = re.compile(r'RunDetail\(\s*"[^"]*"\s*,\s*"([A-Za-z_][A-Za-z0-9_]*)"')
"""An operator-prompt field's channel. Published through the field object rather than by
name - set_state(field.channel, ...) - so the name exists only in this declaration.

Matched on RunDetail specifically, not on any `channel=`: a Bound takes a channel too, and
those name a DEVICE's channels, which are the driver's to declare and not seeded here."""

RULEBOOK_DERIVED = re.compile(r"_status$|^test_status$")
"""Channels a rulebook produces rather than a test: one per bound, plus the aggregate.
Their names are built from bound labels at runtime, so no channels.py lists them and this
sweep cannot see them - see each DUT's channels.py, which says so."""


def dut_packages():
    """Every DUT package under testcases/ that declares a channel surface.

    Discovered rather than listed: what makes a package a DUT here is having a
    channels.py with DEFAULT_STATE in it, which is the same thing that makes it subject
    to this check."""
    found = []
    for module in pkgutil.iter_modules(testcases.__path__):
        if not module.ispkg:
            continue
        try:
            channels = importlib.import_module(f"testcases.{module.name}.channels")
        except ModuleNotFoundError:
            continue
        if hasattr(channels, "DEFAULT_STATE"):
            found.append((module.name, channels.DEFAULT_STATE))
    return found


def _names_in(source: str) -> set:
    names = set(SET_STATE_LITERAL.findall(source))
    names |= set(FIELD_CHANNEL_LITERAL.findall(source))
    for match in re.finditer(r"def derived_channels\(.*?(?=\n    [@A-Za-z]|\Z)", source, re.S):
        names |= set(DERIVED_KEY_LITERAL.findall(match.group(0)))
    return names


def framework_published() -> set:
    """Channels the framework publishes for every DUT - current_step from the @step
    decorator, and so on. Swept rather than listed, so a new one is not a new exemption
    somebody has to add to this file."""
    names = set()
    for module in pkgutil.iter_modules(testcases.__path__):
        if module.ispkg:
            continue
        try:
            names |= _names_in(inspect.getsource(
                importlib.import_module(f"testcases.{module.name}")))
        except (OSError, TypeError, ModuleNotFoundError):
            continue
    return names


def published_names(package: str) -> set:
    """Channel names this DUT's own modules publish, pushed or derived."""
    names = set()
    for module in pkgutil.walk_packages(
        importlib.import_module(f"testcases.{package}").__path__,
        prefix=f"testcases.{package}.",
    ):
        try:
            source = inspect.getsource(importlib.import_module(module.name))
        except (OSError, TypeError, ModuleNotFoundError):
            continue
        names |= _names_in(source)
    return {name for name in names if not RULEBOOK_DERIVED.search(name)}


DUTS = dut_packages()


def test_there_are_duts_to_check():
    """If discovery breaks, every test below passes by finding nothing - which is the one
    way a sweep like this fails silently."""
    assert len(DUTS) >= 2, f"discovered only {[name for name, _ in DUTS]}"


@pytest.mark.parametrize("package,default_state", DUTS, ids=[name for name, _ in DUTS])
def test_every_state_channel_this_dut_publishes_is_seeded(package, default_state):
    missing = sorted(published_names(package) - set(default_state))

    assert not missing, (
        f"testcases/{package} publishes {missing} but testcases/{package}/channels.py "
        f"does not seed them, so the engine drops them from the recorded file"
    )


@pytest.mark.parametrize("package,default_state", DUTS, ids=[name for name, _ in DUTS])
def test_this_dut_seeds_nothing_it_never_publishes(package, default_state):
    """The other direction, and the reason it is worth checking: a seeded channel nothing
    writes is a column of the seed value for the whole run - present, numeric, and never
    once a measurement. Which is how a channel that quietly stopped being published looks."""
    orphaned = sorted(set(default_state) - published_names(package) - framework_published())

    assert not orphaned, (
        f"testcases/{package}/channels.py seeds {orphaned} but nothing in "
        f"testcases/{package} publishes them - a column of the seed value forever"
    )
