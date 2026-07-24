"""Lightweight, locally-hosted operator status page - spawned
automatically at the start of every test case (see
testcases.utils.spawn_operator_dashboard, called from TestCase.run()).

Deliberately minimal: the only interaction it offers is a "Stop test"
button, reusing tools/stop_test.py's own request_stop() rather than
reimplementing what "stop" means - so this button and the standalone
CLI tool can never disagree. Everything else is read-only: test status
(running/passing/failing/stopped) and an error message if something
breaks. No command-sending capability at all, unlike tools/manual_gui.py
- sending arbitrary commands into an already-running automated test
could interfere with its own control logic, whereas a status view is
safe to have open for every test, automated or manual.

Built on the stdlib http.server rather than a new web framework
dependency - the actual surface here is exactly three endpoints (the
page itself, a JSON status poll, and the stop action), which doesn't
need one.

Status/error are pushed explicitly via set_status()/set_error() (see
TestCase.run()) rather than this dashboard polling the test case
itself - that's what lets it correctly reflect a failure that happens
before any Rulebook runner even exists yet (e.g. a PreTestSetup crash),
and it needs no device-specific or DUT-specific knowledge at all.

Separately, while the test is still running, the page also reflects the
Rulebook's own *live* pass/fail state - the aggregate test_status
LiveRulebookRunner already publishes on the tagged telemetry stream on
every frame (see testcases/asimov/live_rulebook_runner.py), which can
go PASS/FAIL well before the test itself reaches any final conclusion
(a non-fatal bound violating doesn't raise anything - see
check_fatal_violation()'s docstring - so run()'s own try/except never
sees it). This is a second, independent data path from set_status()/
set_error() above: a small background thread here subscribes to the
tagged stream directly (the same one tools/manual_gui.py already reads
test_id/test_name from) and filters for frames matching this test's
own test_id, purely to read that one channel - it still needs no
device-specific knowledge, since test_status is generic Rulebook output,
not a hardware channel.

A finished test lingers on its dashboard until the operator closes it
out (see TestCase.run()'s _wait_until_interrupted()) - but an operator
who just closes the browser tab instead sends this process nothing at
all; there's no persistent connection, only polling. That would leave
the process (and this port) held indefinitely, and silently cost the
*next* test its own dashboard (its bind would just fail). So on
successful bind, __init__ below records this test_id in a small lock
file keyed by port; reclaim_stale_dashboard() reads it, and, if the
port is still held, leaves the same stop marker tools/stop_test.py
would, then waits briefly for it to let go - see that function's own
docstring for the full reasoning. spawn_operator_dashboard()
(testcases/utils.py) calls it right before constructing a new
OperatorDashboard.
"""
from __future__ import annotations

import json
import logging
import socket
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

import zmq

from hardware.protocol import DEFAULT_TAGGED_TELEMETRY_ENDPOINT, TAGGED_TELEMETRY_TOPIC, TaggedTelemetryFrame

from .stop_test import request_stop

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_RECLAIM_TIMEOUT_S = 5.0


def _dashboard_lock_path(port: int) -> Path:
    """Where the dashboard currently holding this port (if any) records
    which test_id owns it - keyed by port, since the port is the actual
    scarce resource reclaim_stale_dashboard() manages, not any
    particular test_id."""
    return Path(tempfile.gettempdir()) / f"mytest-dashboard-{port}.lock"


def _port_is_free(host: str, port: int) -> bool:
    """True if nothing is currently bound to (host, port). Used both to
    detect a stale lock file (the previous process already exited
    without cleaning up - e.g. it was killed directly) and to notice
    once a still-alive previous process has actually let go of the port
    after being asked to stop.

    Sets SO_REUSEADDR before binding, matching http.server.HTTPServer's
    own allow_reuse_address=True (which ThreadingHTTPServer inherits) -
    without it, this probe can report the port as still taken for
    several seconds after the real server already released it (an OS-
    level TIME_WAIT-adjacent quarantine that SO_REUSEADDR is specifically
    for), even though the real dashboard's own bind would have succeeded
    immediately. Found by measuring this reclaim taking ~5s in practice
    versus a ~1s worst case on paper."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind((host, port))
    except OSError:
        return False
    else:
        return True
    finally:
        probe.close()


def reclaim_stale_dashboard(
    host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, timeout_s: float = DEFAULT_RECLAIM_TIMEOUT_S
) -> None:
    """Called by spawn_operator_dashboard() before constructing a new
    OperatorDashboard, so a new test doesn't silently lose its status
    page just because the previous test's operator closed the browser
    tab instead of stopping it. Only one dashboard is ever up at a time
    on a given test stand (see stop_test.py's discovery docstring for
    the same one-test-at-a-time assumption), so whatever holds this
    port already is necessarily the previous test still lingering in
    its own _wait_until_interrupted() (testcases/base.py) - never a
    second, concurrent test.

    A no-op if nothing is bound to the port at all - the common case,
    since most tests get closed out properly. If the lock file is
    stale (its process already exited without cleaning up), the port
    is already free and this just clears the leftover file. Otherwise
    it's a live lingering process: this leaves it the same stop marker
    tools/stop_test.py would (request_stop()), rather than killing it
    directly - a raw kill is exactly what the marker-file mechanism
    exists to avoid needing (see StopRequested's docstring in
    testcases/base.py) - and waits up to timeout_s for it to actually
    let go of the port.

    If it doesn't free up in time (e.g. a process wedged deep in
    teardown), this simply gives up rather than escalating to a kill:
    the OSError from the next bind attempt is already handled the same
    way spawn_operator_dashboard() handles any other bind failure, so a
    test can never fail *harder* because of this - at worst, it starts
    with no dashboard at all, exactly as before this existed."""
    lock_path = _dashboard_lock_path(port)
    if not lock_path.exists():
        return
    if _port_is_free(host, port):
        lock_path.unlink(missing_ok=True)
        return
    try:
        stale_test_id = lock_path.read_text().strip()
    except OSError:
        return
    if stale_test_id:
        logger.info("reclaiming operator dashboard port %s from lingering test %s", port, stale_test_id)
        request_stop(stale_test_id)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _port_is_free(host, port):
            return
        time.sleep(0.1)
    logger.warning(
        "test %s did not release operator dashboard port %s within %.1fs - new dashboard may fail to start",
        stale_test_id,
        port,
        timeout_s,
    )

_PAGE_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>mytest operator status</title>
<style>
  body {
    font-family: -apple-system, BlinkMacSystemFont, sans-serif;
    margin: 2em;
    background: #eef1f6;
    color: #222;
    transition: background-color 0.3s ease;
  }
  body.running { background: #eef1f6; }
  body.passing { background: #ddf3df; }
  body.failing { background: #fbdcdc; }
  body.stopped { background: #e8e8e8; }

  h1 { font-size: 1.3em; font-weight: normal; margin-bottom: 0.2em; }
  .brand { font-size: 1.8em; font-weight: bold; color: #5430ef; }
  #meta { color: #666; font-size: 0.95em; margin-bottom: 1em; }
  #ascii-anim {
    font-family: monospace;
    font-size: 6em;
    letter-spacing: 0.3em;
    text-align: center;
    white-space: pre;
    margin: 1.2em 0;
    color: #000;
  }
  #result-line { font-size: 1.3em; font-weight: bold; margin: 0.3em 0; }
  #result-line.pass { color: #1e7a3c; }
  #result-line.fail { color: #8b1a1a; }
  #error { color: #8b1a1a; white-space: pre-wrap; font-family: monospace; margin-top: 1em; }
  #hint { color: #777; font-size: 0.85em; font-style: italic; margin-top: 0.5em; }
  button {
    font-size: 1.25em;
    padding: 0.7em 1.8em;
    background: #8b1a1a;
    color: white;
    border: none;
    border-radius: 8px;
    cursor: pointer;
    margin-top: 1em;
  }
  button:hover { background: #ad2222; }
  button:disabled { background: #bbb; cursor: default; }
</style>
</head>
<body class="running">
<h1><span class="brand">Mytest</span> Status: <span id="status-word">Running</span></h1>
<div id="meta">connecting...</div>
<div id="ascii-anim"></div>
<div id="result-line"></div>
<div id="error"></div>
<div id="hint"></div>
<p><button id="stop-button" onclick="stopTest()">Stop test</button></p>
<script>
const STATUS_WORDS = {
  running: 'Running',
  passing: 'Complete',
  failing: 'Complete',
  stopped: 'Aborted',
};
const ASCII_FRAMES = ['-    ', ' -   ', '  -  ', '   - ', '    -', '   - ', '  -  ', ' -   '];
let asciiFrameIndex = 0;
let isRunning = true;
function tickAscii() {
  const el = document.getElementById('ascii-anim');
  if (isRunning) {
    el.textContent = ASCII_FRAMES[asciiFrameIndex % ASCII_FRAMES.length];
    asciiFrameIndex++;
  } else {
    el.textContent = 'X';
  }
}
async function poll() {
  try {
    const res = await fetch('/status');
    const data = await res.json();
    document.getElementById('meta').textContent = data.test_name + ' (test_id=' + data.test_id + ')';
    document.getElementById('status-word').textContent = STATUS_WORDS[data.status] || data.status;
    isRunning = (data.status === 'running');

    // While running, color/result reflect the Rulebook's own live
    // test_status (a non-fatal bound violating doesn't raise anything,
    // so run()'s own pass/fail tracking never sees it - see this
    // module's docstring) - once the test has a final outcome, that
    // takes over instead.
    let colorState = data.status;
    let resultText = (data.status === 'passing') ? 'Pass' : (data.status === 'failing') ? 'Fail' : null;
    if (data.status === 'running' && data.live_result) {
      colorState = (data.live_result === 'PASS') ? 'passing' : 'failing';
      resultText = (data.live_result === 'PASS') ? 'Pass' : 'Fail';
    }
    document.body.className = colorState;
    const resultEl = document.getElementById('result-line');
    resultEl.textContent = resultText ? ('Result: ' + resultText) : '';
    resultEl.className = (colorState === 'passing') ? 'pass' : (colorState === 'failing') ? 'fail' : '';

    document.getElementById('error').textContent = data.error || '';
    document.getElementById('stop-button').disabled = (data.status !== 'running');
    document.getElementById('hint').textContent = (data.status !== 'running')
      ? "This page stays up so you can see the final result - close the test's own terminal (Ctrl+C) when done."
      : '';
  } catch (e) {
    document.getElementById('meta').textContent = 'disconnected - test process has likely ended';
    document.getElementById('stop-button').disabled = true;
    document.getElementById('hint').textContent = '';
    isRunning = false;
  }
}
async function stopTest() {
  document.getElementById('stop-button').disabled = true;
  try {
    await fetch('/stop', {method: 'POST'});
  } catch (e) {
    // test process may already be gone by the time this resolves - fine either way
  }
}
setInterval(poll, 1000);
setInterval(tickAscii, 300);
poll();
tickAscii();
</script>
</body>
</html>
"""


class OperatorDashboard:
    """Owns a small background HTTP server reflecting one test's live
    status, plus a "Stop test" action. See this module's docstring for
    why status is pushed rather than polled from the test case, and why
    there's no command-sending capability here."""

    def __init__(
        self,
        test_id: str,
        test_name: str,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        tagged_endpoint: str = DEFAULT_TAGGED_TELEMETRY_ENDPOINT,
    ):
        self._test_id = test_id
        self._test_name = test_name
        self._status = "running"
        self._error: Optional[str] = None
        self._live_result: Optional[str] = None  # "PASS"/"FAIL" from the Rulebook's own live test_status
        self._lock = threading.Lock()
        self._host = host
        self._port = port

        dashboard = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass  # keep console output limited to the framework's own logging

            def _send(self, code: int, content_type: str, body: bytes) -> None:
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802 (http.server's own naming convention)
                if self.path == "/":
                    self._send(200, "text/html; charset=utf-8", _PAGE_HTML.encode("utf-8"))
                elif self.path == "/status":
                    with dashboard._lock:
                        body = json.dumps(
                            {
                                "test_id": dashboard._test_id,
                                "test_name": dashboard._test_name,
                                "status": dashboard._status,
                                "error": dashboard._error,
                                "live_result": dashboard._live_result,
                            }
                        ).encode("utf-8")
                    self._send(200, "application/json", body)
                else:
                    self.send_error(404)

            def do_POST(self) -> None:  # noqa: N802
                if self.path == "/stop":
                    request_stop(dashboard._test_id)
                    self._send(200, "application/json", b'{"ok": true}')
                else:
                    self.send_error(404)

        self._server = ThreadingHTTPServer((host, port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True, name="operator-dashboard")

        # Bind succeeded above, so this test now owns the port - record that
        # so a future reclaim_stale_dashboard() call can find and stop this
        # test if its operator never closes the dashboard out themselves.
        self._lock_path = _dashboard_lock_path(port)
        try:
            self._lock_path.write_text(test_id)
        except OSError as exc:
            logger.warning("test %s: couldn't write dashboard lock file: %s", test_id, exc)

        self._tagged_endpoint = tagged_endpoint
        self._tagged_stop_event = threading.Event()
        self._tagged_thread = threading.Thread(
            target=self._watch_tagged_telemetry, daemon=True, name="operator-dashboard-tagged-watch"
        )

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self._port}/"

    def start(self, open_browser: bool = True) -> None:
        self._thread.start()
        self._tagged_thread.start()
        if open_browser:
            try:
                webbrowser.open(self.url)
            except webbrowser.Error as exc:
                # A genuinely headless machine (no browser, no $DISPLAY) -
                # plausible on the CentOS test stands AI/Mytest.md targets -
                # raises here instead of just returning False. The page is
                # still up at self.url for the operator to open manually;
                # not being able to auto-open a tab shouldn't crash the test
                # before pre_test_setup() even runs.
                logger.warning("test %s: couldn't auto-open a browser tab for %s: %s", self._test_id, self.url, exc)

    def set_status(self, status: str) -> None:
        with self._lock:
            self._status = status

    def set_error(self, message: str) -> None:
        with self._lock:
            self._error = message

    def _watch_tagged_telemetry(self) -> None:
        """Background thread: reads this test's own live test_status off
        the tagged telemetry stream - see this module's docstring for
        why that's a separate data path from set_status()/set_error().

        Polls with a timeout and checks _tagged_stop_event itself, rather
        than blocking in recv_multipart() and relying on stop() to close
        the socket out from under it: closing a zmq socket from a
        different thread than the one blocked reading it is a real crash
        risk, not just a hypothetical one - found via a lingering test
        getting reclaim_stale_dashboard()'d out from under it, which
        aborted the process with a libzmq "Bad file descriptor"
        assertion (src/signaler.cpp) instead of exiting cleanly. Owning
        the socket's full lifecycle in this one thread - including
        closing it, in the finally below, once this loop itself decides
        to stop - avoids that class of race entirely."""
        ctx = zmq.Context.instance()
        socket = ctx.socket(zmq.SUB)
        socket.setsockopt(zmq.SUBSCRIBE, TAGGED_TELEMETRY_TOPIC)
        socket.connect(self._tagged_endpoint)
        poller = zmq.Poller()
        poller.register(socket, zmq.POLLIN)
        try:
            while not self._tagged_stop_event.is_set():
                if not poller.poll(timeout=200):  # ms; re-checks the stop event this often
                    continue
                _, raw = socket.recv_multipart()
                frame = TaggedTelemetryFrame.from_bytes(raw)
                if frame.test_id != self._test_id:
                    continue
                test_status = frame.channels.get("test_status")
                if test_status in ("PASS", "FAIL"):
                    with self._lock:
                        self._live_result = test_status
        finally:
            socket.close(linger=0)

    def stop(self) -> None:
        self._tagged_stop_event.set()
        self._tagged_thread.join(timeout=2.0)
        self._server.shutdown()
        self._server.server_close()
        try:
            self._lock_path.unlink(missing_ok=True)
        except OSError as exc:
            # Called from TestCase.run()'s bare finally block - raising here
            # would replace whatever exception (a real test failure, a
            # deliberate stop) is already propagating out of run(), so this
            # is log-not-raise like every other best-effort cleanup in this
            # module (e.g. the lock file write in __init__).
            logger.warning("test %s: couldn't remove dashboard lock file: %s", self._test_id, exc)
