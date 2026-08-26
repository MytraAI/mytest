"""One consumer per TelemetryClient, and what happens when that is broken.

The failure this covers ended a 40-cycle zdrive run: `ValueError: not enough values
to unpack (expected 2, got 1)`, raised from a temperature read that had nothing to do
with the thread it was racing. A SUB socket is not thread-safe, and the testbed handed
the same client to LiveRulebookRunner's thread and read it from the test's own.

The crash was the lesser half. A subscription delivers each frame once, so two readers
divide the stream instead of both seeing it - which for the runner means fatal bounds
evaluated on the frames the other reader did not take first, and nothing saying so.
"""
from __future__ import annotations

import threading

import pytest
import zmq

from hardware.clients.telemetry_client import (
    ConcurrentTelemetryRead,
    TelemetryClient,
    TelemetryTimeout,
)
from protocol.wire import TELEMETRY_TOPIC, TelemetryFrame


@pytest.fixture
def endpoint():
    return "inproc://telemetry-sharing-test"


@pytest.fixture
def publisher(endpoint):
    socket = zmq.Context.instance().socket(zmq.PUB)
    socket.bind(endpoint)
    yield socket
    socket.close(linger=0)


def _frame(seq: int) -> TelemetryFrame:
    return TelemetryFrame(device="odrive", seq=seq, t=float(seq), channels={"n": seq})


# --- a torn message is named, not unpacked ------------------------------------


def test_a_message_that_is_not_topic_and_payload_says_so(publisher, endpoint):
    """The half of a torn pair the loser of the race is left holding. A bare
    `_, raw = recv_multipart()` reported only that a tuple was the wrong size, from a
    stack frame that never mentions telemetry."""
    client = TelemetryClient(endpoint=endpoint, timeout_s=1.0)
    publisher.send_multipart([TELEMETRY_TOPIC])  # topic only: no payload behind it

    with pytest.raises(ConcurrentTelemetryRead) as caught:
        next(client.frames())

    assert "1 part" in str(caught.value)
    assert "more than one thread" in str(caught.value)
    client.close()


def test_a_whole_message_still_reads_normally(publisher, endpoint):
    client = TelemetryClient(endpoint=endpoint, timeout_s=1.0)
    publisher.send_multipart([TELEMETRY_TOPIC, _frame(7).to_bytes()])

    assert next(client.frames()).seq == 7
    client.close()


# --- a second reader is refused rather than served --------------------------


def test_a_second_thread_reading_one_client_is_told_what_it_has_done(publisher, endpoint):
    """Refused rather than queued behind the first reader: serializing them would stop
    the crash and leave them splitting the stream, which is the failure worth keeping
    loud."""
    client = TelemetryClient(endpoint=endpoint, timeout_s=5.0)
    in_recv = threading.Event()
    release = threading.Event()
    raised = []

    real_recv = client._socket.recv_multipart

    def blocking_recv(*a, **kw):
        in_recv.set()
        release.wait(5.0)
        return real_recv(*a, **kw)

    client._socket.recv_multipart = blocking_recv
    publisher.send_multipart([TELEMETRY_TOPIC, _frame(1).to_bytes()])

    def first_reader():
        try:
            next(client.frames())
        except Exception as exc:  # pragma: no cover - only on a broken fix
            raised.append(exc)

    reader = threading.Thread(target=first_reader, daemon=True)
    reader.start()
    assert in_recv.wait(5.0), "the first reader never reached the socket"

    with pytest.raises(ConcurrentTelemetryRead):
        client._recv_frame()

    release.set()
    reader.join(5.0)
    assert not raised, f"the first reader should have been unaffected: {raised}"
    client.close()


def test_one_reader_at_a_time_is_never_refused(publisher, endpoint):
    """Sequential reads on one client are the normal case and must not trip the
    detector - it is not a lock callers have to think about."""
    client = TelemetryClient(endpoint=endpoint, timeout_s=1.0)
    for seq in range(5):
        publisher.send_multipart([TELEMETRY_TOPIC, _frame(seq).to_bytes()])

    assert [f.seq for _, f in zip(range(5), client.frames())] == [0, 1, 2, 3, 4]
    client.close()


# --- the testbeds keep the runner's stream to itself -------------------------


def test_every_stream_a_zdrive_run_evaluates_has_its_own_reader():
    """A testbed method must never read a client main_execution hands to
    runner.start() - see this module's docstring for what that cost."""
    import inspect

    from testbeds.zdrive_testbed import zdrive_testbed

    source = inspect.getsource(zdrive_testbed.ZdriveTestbed)
    for shared in ("self.telemetry.latest_frame", "self.bus_telemetry.latest_frame",
                   "self.tc_daq_telemetry.latest_frame"):
        assert shared not in source, f"{shared} is the runner's stream, not this process's"


def test_every_stream_a_ydrive_run_evaluates_has_its_own_reader():
    import inspect

    from testbeds.ydrive_testbed import ydrive_testbed

    source = inspect.getsource(ydrive_testbed.YdriveTestbed)
    for shared in ("self.telemetry.latest_frame", "self.supply_telemetry.latest_frame",
                   "self.tc_daq_telemetry.latest_frame"):
        assert shared not in source, f"{shared} is the runner's stream, not this process's"
