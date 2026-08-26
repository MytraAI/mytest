"""State the board was already holding when the driver connected, and what may refuse a run.

THE RUN THIS COMES FROM. On 2026-08-26 a person switched the supplies off part-way
through a 265-cycle zdrive run. The gate driver recorded its own rail collapsing as
last_drv_fault = 0x400000 and the axis disarmed with DC_BUS_UNDER_VOLTAGE, both correct.
Every restart afterwards then failed:

    the ODrive is not fit to operate after 5.0s -
    still latched after being cleared: {'last_drv_fault': 'UNKNOWN_BIT_0x400000'}

while active_errors cleared to 0 and the board was fit. clear_errors() does not reset
last_drv_fault and nothing else can, so the stand was unusable until somebody found the
one stuck bit. The board was never the problem: a record of a past fault was being read
as a present one.
"""
from __future__ import annotations

import logging

from hardware.odrive import odrive_errors as oe
from hardware.odrive.odrive_backend import OdriveBackend


DRV_FAULT_FROM_THE_OUTAGE = 4194304  # 0x400000, as recorded on the stand
UNDER_VOLTAGE = 512                  # ODriveError.DC_BUS_UNDER_VOLTAGE


def _after_the_outage(**overrides):
    """The board as the next run finds it: the previous run's wreckage, nothing live."""
    frame = {channel: 0 for channel in oe.WATCHED_CHANNELS}
    frame.update(
        last_drv_fault=DRV_FAULT_FROM_THE_OUTAGE,
        disarm_reason=UNDER_VOLTAGE,
        active_errors=0,          # cleared: the bus is back and the board is fit
        axis_current_state=1,     # IDLE
    )
    frame.update(overrides)
    return frame


# --- what may refuse a run ---------------------------------------------------


def test_a_board_holding_only_a_past_fault_is_fit_to_operate():
    """The whole failure: this frame refused every restart for as long as the board
    stayed powered."""
    assert oe.faults_in_frame(_after_the_outage()) == {}


def test_a_live_error_still_refuses_the_run():
    """The gate has to keep gating - a bus that is genuinely down must not be started on."""
    faults = oe.faults_in_frame(_after_the_outage(active_errors=UNDER_VOLTAGE))

    assert "active_errors" in faults
    assert "DC_BUS_UNDER_VOLTAGE" in faults["active_errors"]


def test_a_live_condition_still_refuses_the_run():
    """A mapper with no encoder estimate describes the board now, and clearing cannot
    touch it - the cause has to change."""
    faults = oe.faults_in_frame(_after_the_outage(commutmapper_status=4))

    assert "commutmapper_status" in faults


def test_the_past_fault_is_still_reported_somewhere():
    """Not gating is not the same as not knowing. A gate-driver fault that really does
    prevent operation says so through active_errors; this stays readable either way."""
    records = oe.records_in_frame(_after_the_outage())

    assert "last_drv_fault" in records
    assert "0x400000" in records["last_drv_fault"]
    assert "disarm_reason" in records


def test_an_unrecognised_bit_cannot_be_the_reason_a_run_is_refused():
    """0x400000 has no name in the installed package's DrvFault enum, which made the
    message read worse than the situation. An unknown bit in a record is a decode gap,
    never a verdict about the hardware."""
    frame = _after_the_outage(last_drv_fault=0x40000000)

    assert oe.faults_in_frame(frame) == {}
    assert "UNKNOWN_BIT" in oe.records_in_frame(frame)["last_drv_fault"]


# --- a procedure in progress is not a fault ----------------------------------


def test_a_procedure_that_is_running_is_not_a_fault():
    """BUSY means a procedure is running, which is the normal state part-way through
    arming. Read as a fault it warned on every arm, and a check sampling mid-arm saw a
    working stand as a broken one."""
    assert oe.is_fault("axis_procedure_result", 1) is False
    assert oe.faults_in_frame(_after_the_outage(axis_procedure_result=1)) == {}


def test_a_procedure_that_failed_is_still_a_fault():
    """Only BUSY is progress - DISARMED is an outcome, and stays loud."""
    assert oe.is_fault("axis_procedure_result", 3) is True


# --- what the run inherited --------------------------------------------------


def test_what_the_board_arrived_holding_is_captured(caplog):
    caplog.set_level(logging.DEBUG)
    backend = OdriveBackend()

    backend._log_error_transitions(_after_the_outage())

    assert backend._inherited["last_drv_fault"] == "UNKNOWN_BIT_0x400000"
    assert backend._inherited["disarm_reason"] == "DC_BUS_UNDER_VOLTAGE"


def test_a_clean_board_inherits_nothing():
    backend = OdriveBackend()

    backend._log_error_transitions({channel: 0 for channel in oe.WATCHED_CHANNELS})

    assert backend._inherited == {}


def test_a_fault_that_appears_after_the_first_frame_is_not_inherited():
    """Inherited means the board arrived with it. Anything appearing later is this
    run's, and the distinction is the point of recording it at all."""
    backend = OdriveBackend()
    backend._log_error_transitions({channel: 0 for channel in oe.WATCHED_CHANNELS})

    backend._log_error_transitions(
        {**{c: 0 for c in oe.WATCHED_CHANNELS}, "active_errors": UNDER_VOLTAGE}
    )

    assert backend._inherited == {}


def test_the_startup_line_says_whether_it_stands_in_the_way(caplog):
    """The log is where a person meets this. 'already set at startup' alone reads as a
    problem to solve before starting, which for a record it is not."""
    caplog.set_level(logging.DEBUG)
    backend = OdriveBackend()

    backend._log_error_transitions(_after_the_outage(active_errors=UNDER_VOLTAGE))

    lines = caplog.text
    assert "not a reason to refuse this run" in lines, "the record must say it is history"
    assert "cannot operate until it clears" in lines, "the live error must say it blocks"
