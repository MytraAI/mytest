"""A window telling the operator what to do, with a button that says they did it.

Spawned by `await_operator()` (testcases/ydrive/teststeps/) while a test waits for
a person. Clicking the button leaves the same marker file
`python -m tools.operator_ack` leaves, so the waiting test is polling for one
thing whichever way it is answered - and the test's own loop keeps checking for a
fatal bound, a stop request and a lost recorder throughout, which is what a
blocking dialog inside the test process would have suspended.

    python -m tools.operator_prompt --test-id <test_id> --message "do the thing"
    python -m tools.operator_prompt --test-id <id> --message "..." --field "DUT SN" --field "Load (lb)"
    python -m tools.operator_prompt ... --field "DUT SN" --choice "DUT SN=YDRIVE1,YDRIVE2"
    python -m tools.operator_prompt ... --field "ER Ticket" --pattern "ER Ticket=^ER-[0-9]+$"

With --field, the window collects free text instead of just confirming: one entry
per field, in the order given, and the answers are written into the marker file as
JSON for the waiting step to read. Every field must be filled - a run whose DUT
serial nobody wrote down is a run whose results cannot be attributed later, which
is the whole reason for asking at all.

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
import re
import sys
from typing import Dict, Optional, Sequence

from tools.operator_ack import acknowledge

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

WRAP_WIDTH_PX = 420


def normalise_and_check(
    answers: Dict[str, str],
    patterns: Dict[str, str],
    hints: Optional[Dict[str, str]] = None,
) -> Optional[str]:
    """Upper-case every patterned answer in place, and return the first complaint.

    Upper-cased before it is checked, and it is the upper-cased value that gets
    submitted: a patterned field has one canonical spelling, and the waiting step
    applies the same rule to whatever arrives however it arrived.

    A pattern naming a field that was not asked for is skipped rather than raising -
    that is a typo on a command line, and an unhandled exception inside a Tk callback
    is a button that silently does nothing.

    Outside show() because it is the part worth testing: it is what stops a typo
    ending a run somebody was in the middle of starting, and it needs no display."""
    hints = hints or {}
    for name, pattern in patterns.items():
        if name not in answers:
            continue
        answers[name] = answers[name].upper()
        if not re.match(pattern, answers[name]):
            return f"{name} should look like {hints.get(name) or pattern}"
    return None


def show(
    test_id: str,
    message: str,
    fields: Sequence[str] = (),
    choices: Optional[Dict[str, Sequence[str]]] = None,
    patterns: Optional[Dict[str, str]] = None,
    hints: Optional[Dict[str, str]] = None,
    headline: Optional[str] = None,
) -> int:
    """Show the window and block until it is closed. Returns 0 if the operator
    acknowledged, 1 if they closed it without doing so, 2 if no window could be
    opened at all - a headless stand, or a Python without tkinter, neither of
    which should stop a test that is still perfectly answerable from the CLI.

    With `fields`, the window collects a value for each before it will submit. A
    field named in `choices` is a read-only dropdown of those values rather than an
    entry, so what lands in the record is one of a known set and not a typo of
    one. A field named in `patterns` is free text that has to match that regular
    expression, upper-cased first, and the window will not submit until it does -
    `hints` is what the operator is told to type instead, since a regex is not an
    instruction.

    `headline` replaces the generic title with one line in large red bold - for a
    window whose whole point is that one sentence, where the detail underneath is
    for whoever fixes it rather than whoever is about to press the button. It
    replaces rather than adds, because two lines that size compete and neither
    gets read."""
    try:
        import tkinter as tk
    except ImportError:
        logger.warning("tkinter is not available - acknowledge with `python -m tools.operator_ack`")
        return 2

    acknowledged = False
    entries: dict = {}
    choices = choices or {}
    patterns = patterns or {}
    hints = hints or {}

    def on_click() -> None:
        nonlocal acknowledged
        answers = {name: entry.get().strip() for name, entry in entries.items()}
        missing = [name for name, value in answers.items() if not value]
        if missing:
            # Refused rather than accepted blank: an unattributable run is worse
            # than a run that waited for someone to type.
            complaint.config(text=f"still needed: {', '.join(missing)}")
            return
        # Refused here rather than by the test: this window is open with a person in
        # front of it, so a typo is something they can fix. The same answer arriving
        # from the CLI ends the run instead.
        bad = normalise_and_check(answers, patterns, hints)
        if bad:
            complaint.config(text=bad)
            return
        acknowledged = True
        acknowledge(test_id, answers)
        root.destroy()

    try:
        root = tk.Tk()
    except tk.TclError as exc:  # no display
        logger.warning("no display for the operator prompt (%s) - use `tools.operator_ack`", exc)
        return 2

    root.title("Mytest - " + (headline or "operator action needed"))
    root.attributes("-topmost", True)  # a stand's screen has other windows on it
    frame = tk.Frame(root, padx=24, pady=20)
    frame.pack()
    if headline:
        # Wrapped wider than the body: this is meant to be read across the room,
        # and a short shouted line broken over three lines reads as neither.
        tk.Label(
            frame, text=headline, font=("TkDefaultFont", 17, "bold"), fg="#b00",
            wraplength=WRAP_WIDTH_PX + 120, justify="center",
        ).pack()
    else:
        tk.Label(frame, text="Operator action needed", font=("TkDefaultFont", 15, "bold")).pack()
    tk.Label(frame, text=test_id, font=("TkDefaultFont", 10), fg="#666").pack(pady=(2, 14))
    tk.Label(frame, text=message, wraplength=WRAP_WIDTH_PX, justify="left",
             font=("TkDefaultFont", 12)).pack()

    if fields:
        form = tk.Frame(frame, pady=14)
        form.pack(fill="x")
        for row, name in enumerate(fields):
            tk.Label(form, text=name, font=("TkDefaultFont", 11), anchor="e", width=12).grid(
                row=row, column=0, sticky="e", pady=4, padx=(0, 8)
            )
            if name in choices:
                from tkinter import ttk

                # Read-only, so the answer is one of the known values rather than a
                # typo of one - a misspelled serial is a run attributed to nothing.
                entry = ttk.Combobox(
                    form, values=list(choices[name]), state="readonly",
                    font=("TkDefaultFont", 12), width=26,
                )
            else:
                entry = tk.Entry(form, font=("TkDefaultFont", 12), width=28)
            entry.grid(row=row, column=1, sticky="we", pady=4)
            entries[name] = entry
        next(iter(entries.values())).focus_set()
    complaint = tk.Label(frame, text="", fg="#b00", font=("TkDefaultFont", 10))
    complaint.pack()
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
    parser.add_argument(
        "--field", action="append", default=[], metavar="NAME",
        help="collect free text under this label; repeatable, order is the form's order",
    )
    parser.add_argument(
        "--choice", action="append", default=[], metavar="NAME=A,B,C",
        help="make that field a dropdown of these values instead of free text",
    )
    parser.add_argument(
        "--pattern", action="append", default=[], metavar="NAME=REGEX",
        help="refuse to submit until that field matches this regular expression",
    )
    parser.add_argument(
        "--hint", action="append", default=[], metavar="NAME=TEXT",
        help="what to tell the operator when that field's --pattern does not match",
    )
    parser.add_argument(
        "--headline", default=None,
        help="one line shown large and in red instead of the generic title",
    )
    args = parser.parse_args()
    choices = {}
    for spec in args.choice:
        name, _, values = spec.partition("=")
        choices[name] = [v for v in values.split(",") if v]
    patterns = dict(spec.partition("=")[::2] for spec in args.pattern)
    hints = dict(spec.partition("=")[::2] for spec in args.hint)
    sys.exit(show(args.test_id, args.message, args.field, choices,
                  patterns=patterns, hints=hints, headline=args.headline))
