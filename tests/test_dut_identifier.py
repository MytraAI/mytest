"""TestCase.DUT, and the three places it has to agree with.

Which DUT produced a run decides where that run's results are filed, so the
identifier has to be exactly the DUT package's own directory name - not
approximately it, and not something that drifts when a directory is renamed.
Three independent statements of the same string exist, and nothing but a test
holds them together:

- the directory under testcases/
- the DUT attribute on that package's base test case
- the "<dut>.<test>" keys in testcases/registry.py

These also pin the two things that make the attribute worth having over
deriving it from __module__ at runtime: it is inherited by subclasses defined
anywhere, and it reaches the verdict.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from protocol.verdict import Verdict
from testcases.base import TestCase
from testcases.example_dut.testcases.base_example_dut_test import BaseExampleDutTest
from testcases.registry import REGISTERED_TESTS
from testcases.teststeps.duts import DUT_SERIAL_NUMBERS, serials_for
from testcases.ydrive.testcases.base_ydrive_test import BaseYdriveTest
from testcases.ydrive.testcases.testcases import EnduranceCycleTest
from testcases.zdrive.testcases.base_zdrive_test import BaseZdriveTest

DUT_BASE_CLASSES = (BaseExampleDutTest, BaseYdriveTest, BaseZdriveTest)

TESTCASES_DIR = Path(__file__).resolve().parent.parent / "testcases"


def _dut_directories() -> set:
    """Every DUT package under testcases/, as a set of directory names.

    Recognised by shape rather than by a list of names to keep in step, and
    rather than by excluding the shared packages one at a time: a DUT is a
    package with its own testcases/ and its own channel surface. A new shared
    package (teststeps/ is the first) is therefore not a DUT by construction,
    and a real DUT missing either half is not silently exempted from the checks
    below - it fails the first one."""
    return {
        entry.name
        for entry in TESTCASES_DIR.iterdir()
        if (entry / "testcases" / "__init__.py").exists()
        and (entry / "channels.py").exists()
    }


def test_every_dut_directory_has_a_base_class_declaring_it():
    """The set of DUT directories and the set of declared DUTs are the same set.

    A new DUT package that forgets the attribute fails here rather than
    producing runs that cannot be filed."""
    assert {cls.DUT for cls in DUT_BASE_CLASSES} == _dut_directories()


@pytest.mark.parametrize("cls", DUT_BASE_CLASSES)
def test_dut_matches_the_package_it_is_declared_in(cls):
    """The attribute equals the directory its own module lives in - so renaming
    the directory without the attribute (or the reverse) is caught."""
    package = Path(cls.__module__.replace(".", "/"))
    assert cls.DUT in package.parts


def test_registry_keys_are_prefixed_with_a_real_dut():
    """Every registered test is keyed "<dut>.<test>" with a dut that exists.

    run_test.py looks tests up by these keys, so a typo here is a test nobody
    can start - and a prefix that is not a DUT is a run filed under a stand
    that does not exist."""
    declared = {cls.DUT for cls in DUT_BASE_CLASSES}
    for key in REGISTERED_TESTS:
        prefix = key.split(".")[0]
        assert prefix in declared, f"{key!r} is keyed by {prefix!r}, which is not a DUT"


def test_registry_key_matches_the_dut_of_the_test_it_builds():
    """Constructing each registered test yields one whose DUT is its key's prefix.

    The strongest form of the check: the key, the class hierarchy and the
    attribute all have to agree, not just exist."""
    for key, factory in REGISTERED_TESTS.items():
        case = factory(f"{key}-dut-check", False)
        assert case.DUT == key.split(".")[0], key


def test_dut_is_inherited_by_a_subclass_defined_outside_the_package():
    """A subclass written in a test module still reports the DUT it came from.

    This is the case that rules out deriving the identifier from __module__:
    this class's module is tests.test_dut_identifier, and the answer is still
    ydrive."""

    class OneOffCase(EnduranceCycleTest):
        pass

    assert OneOffCase.DUT == "ydrive"


def test_base_testcase_declares_no_dut():
    """TestCase itself has no DUT - only a DUT package's base class can say."""
    assert TestCase.DUT == ""


def test_verdict_carries_the_dut():
    """A verdict records which DUT produced the run, and survives a round-trip."""
    verdict = Verdict(
        test_id="t", test_name="n", lifecycle="COMPLETED", bounds_result="PASS",
        started_at=0.0, ended_at=1.0, dut="zdrive",
    )
    assert Verdict.from_dict(verdict.to_dict()).dut == "zdrive"


def test_verdict_written_before_this_field_existed_reads_as_no_dut():
    """A verdict with no `dut` key at all is legible, and reports no DUT.

    The engine amends verdicts in place (protocol/verdict.py amend_completeness
    round-trips through from_dict), so a field it dropped would be a field
    silently deleted from every run it touched."""
    data = {
        "test_id": "t", "test_name": "n", "lifecycle": "COMPLETED",
        "bounds_result": "PASS", "started_at": 0.0, "ended_at": 1.0,
    }
    assert Verdict.from_dict(data).dut == ""


def test_serial_catalogue_offers_only_what_a_stand_can_run():
    """Each DUT's prompt offers the units catalogued against it, and no others."""
    assert serials_for("zdrive") == ("ZDRIVE2IN",)
    assert serials_for("ydrive") == ("YDRIVE1", "YDRIVE2", "ZDRIVE2IN")


def test_catalogued_serials_name_real_duts():
    """No entry is catalogued against a DUT package that does not exist."""
    declared = {cls.DUT for cls in DUT_BASE_CLASSES}
    for serial, duts in DUT_SERIAL_NUMBERS.items():
        assert duts, f"{serial!r} is catalogued against no DUT at all"
        for dut in duts:
            assert dut in declared, f"{serial!r} names {dut!r}, which is not a DUT"


@pytest.mark.parametrize("cls", (BaseYdriveTest, BaseZdriveTest))
def test_run_detail_fields_use_this_duts_serials(cls):
    """The serial dropdown a stand shows is the catalogue's answer for that stand."""
    serial_field = cls.RUN_DETAIL_FIELDS[0]
    assert serial_field.channel == "dut_serial_number"
    assert serial_field.choices == serials_for(cls.DUT)
