"""A window telling the operator what to do, with a button that says they did it.

Spawned by `await_operator()` (testcases/ydrive/teststeps/) while a test waits for
a person. Clicking the button leaves the same marker file
`python -m tools.operator_ack` leaves, so the waiting test is polling for one
thing whichever way it is answered - and the test's own loop keeps checking for a
fatal bound, a stop request and a lost recorder throughout, which is what a
blocking dialog inside the test process would have suspended.

    python -m tools.operator_prompt --test-id <test_id> --message "do the thing"

Its own process, not a window inside the test process, for the reason Tkinter
insists on: a Tk main loop owns the thread it runs on. Inside the test process it
would either block the sequence or have to run off the main thread, which Tk does
not support. As a separate process it can be terminated the moment the wait ends,
however the wait ended.

Deliberately cannot cancel a test. The only outcomes are acknowledging and
closing the window - stopping a run is tools/stop_test.py's job and the status
page's, and a dialog that could do both invites clicking the wrong one while
standing at live hardware. Closing the window does not acknowledge: the test goes
on waiting, and the operator can re-open this or use operator_ack.
"""
from __future__ import annotations

import argparse
import logging
import sys

from tools.operator_ack import acknowledge

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

WRAP_WIDTH_PX = 420


def show(test_id: str, message: str) -> int:
    """Show the window and block until it is closed. Returns 0 if the operator
    acknowledged, 1 if they closed it without doing so, 2 if no window could be
    opened at all - a headless stand, or a Python without tkinter, neither of
    which should stop a test that is still perfectly answerable from the CLI."""
    try:
        import tkinter as tk
    except ImportError:
        logger.warning("tkinter is not available - acknowledge with `python -m tools.operator_ack`")
        return 2

    acknowledged = False

    def on_click() -> None:
        nonlocal acknowledged
        acknowledged = True
        acknowledge(test_id)
        root.destroy()

    try:
        root = tk.Tk()
    except tk.TclError as exc:  # no display
        logger.warning("no display for the operator prompt (%s) - use `tools.operator_ack`", exc)
        return 2

    root.title("Mytest - operator action needed")
    root.attributes("-topmost", True)  # a stand's screen has other windows on it
    frame = tk.Frame(root, padx=24, pady=20)
    frame.pack()
    tk.Label(frame, text="Operator action needed", font=("TkDefaultFont", 15, "bold")).pack()
    tk.Label(frame, text=test_id, font=("TkDefaultFont", 10), fg="#666").pack(pady=(2, 14))
    tk.Label(frame, text=message, wraplength=WRAP_WIDTH_PX, justify="left",
             font=("TkDefaultFont", 12)).pack()
    tk.Button(frame, text="Done - continue the test", command=on_click,
              font=("TkDefaultFont", 13, "bold"), padx=16, pady=10).pack(pady=(20, 4))
    tk.Label(frame, text="Closing this window does not acknowledge; the test keeps waiting.",
             font=("TkDefaultFont", 9), fg="#666").pack()

    root.update_idletasks()
    width, height = root.winfo_width(), root.winfo_height()
    x = (root.winfo_screenwidth() - width) // 2
    y = (root.winfo_screenheight() - height) // 3
    root.geometry(f"+{x}+{y}")
    root.mainloop()

    if acknowledged:
        logger.info("operator acknowledged test %s", test_id)
        return 0
    logger.info("window closed without acknowledging - test %s is still waiting", test_id)
    return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-id", required=True)
    parser.add_argument("--message", required=True)
    args = parser.parse_args()
    sys.exit(show(args.test_id, args.message))
