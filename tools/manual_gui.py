"""Barebones operator GUI.

Available channels are split into two collapsible top-level sections -
Telemetry channels and Command channels - each holding one collapsible
sub-group per device/endpoint, filterable by the search box. A device's
sub-group appears the moment it's added (telemetry: a raw per-device
subscription; commands: a validated CommandClient connection) - there's
no device-specific knowledge anywhere in this file, so the same tool
works unmodified against any current or future testcase, including
hardware this repo doesn't support yet.

Telemetry is deliberately per-device, not one shared stream: the tagged
stream (hardware/protocol.py's DEFAULT_TAGGED_TELEMETRY_ENDPOINT) is
still subscribed by default - it's the only place test-level context
(test_id/test_name, Rulebook bound-status, current_step) lives - but
it's just one more sub-group among however many raw per-device
telemetry subscriptions get added, since a shared/tagged stream has no
established way to disambiguate channels across more than one device
(that's an open architecture question above this tool's scope - see
AI/Mytest.md). A per-device raw subscription is unambiguous by
construction and works even with no test case running at all.

Checking a telemetry channel plots it on the live graph (see below).
Checking a command channel shows an editable JSON params field (e.g.
{"value": 1.5}) plus a Send button - JSON, not a bare value, because
the wire protocol has no way to discover an action's parameter
names/count (list_actions() returns names only), so there's no generic
way to know set_position wants value= while set_axis_state wants
state= and move_incremental wants two params, without hardcoding
per-action knowledge this tool is built to avoid. Supporting a
bare-value shortcut for the common single-value-setter case is a
reasonable future nice-to-have, not done here. "Value" for a command
channel is always the last value *sent from this GUI*, never a live
hardware readback - there's no generic way to read one back either
(reading is the telemetry stream's job, not the command server's).

The telemetry graph (matplotlib, embedded via FigureCanvasTkAgg) always
buffers up to MAX_HISTORY_S of history per checked channel in the
background regardless of which window is currently displayed, so
switching window size is instant. Up to two independently-auto-scaling
y-axes are available; each checked channel is manually assigned to one
via its own toggle button - no automatic/heuristic grouping, so
checking two channels never surprises you about which axis they share.
Pausing only freezes the redraw - buffering continues underneath, so
nothing recorded is lost - and Resume jumps back to the live view
rather than replaying whatever happened while paused. Each channel's
plot color is assigned once (from a fixed palette) and shown as a
swatch next to its name/axis-toggle row, since matplotlib's own legend
would duplicate the axis-toggle control's job of labeling each channel.

A collapsed section/sub-group in the available-channels list stays
collapsed across any redraw (a new channel discovered, a new device
added, a search-box edit) - only an explicit click on its own header
re-expands it.

Run with (from the repo root):
    python -m tools.manual_gui
"""
from __future__ import annotations

import itertools
import json
import queue
import threading
import time
import tkinter as tk
from collections import deque
from dataclasses import dataclass, field
from tkinter import ttk
from typing import Any, Callable, Deque, Dict, Optional, Tuple

import matplotlib

matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402

import zmq  # noqa: E402

from hardware.clients.command_client import CommandClient, CommandClientError  # noqa: E402
from hardware.protocol import (  # noqa: E402
    DEFAULT_TAGGED_TELEMETRY_ENDPOINT,
    TAGGED_TELEMETRY_TOPIC,
    TELEMETRY_TOPIC,
    TaggedTelemetryFrame,
    TelemetryFrame,
)

from .stop_test import request_stop  # noqa: E402

POLL_INTERVAL_MS = 100
PLOT_REDRAW_MS = 250
COMMAND_TIMEOUT_MS = 5000
TAGGED_STREAM_LABEL = "tagged stream (test context)"

MAX_HISTORY_S = 600.0  # always buffer the largest window (10 min), regardless of what's currently displayed
WINDOW_OPTIONS = (("10 s", 10.0), ("30 s", 30.0), ("1 min", 60.0), ("10 min", 600.0))
PLOT_COLORS = (
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
    "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
)


@dataclass
class TelemetryUpdate:
    """One decoded frame's worth of channels, from either the tagged
    stream or a raw per-device subscription - both subscriber classes
    below enqueue these onto the same shared queue so the GUI thread
    handles them uniformly regardless of source."""

    device_label: str
    channels: Dict[str, Any]
    test_context: Optional[Tuple[str, str]] = None  # (test_id, test_name) - tagged stream only


class TaggedTelemetrySubscriber:
    """Subscribes to the tagged telemetry stream - there is only ever
    one (see hardware/protocol.py's DEFAULT_TAGGED_TELEMETRY_ENDPOINT),
    carrying test-level context alongside whichever single device's raw
    channels that test happens to be watching. Runs on a background
    thread; Tkinter isn't thread-safe, so decoded updates are pushed
    onto update_queue rather than touching widgets directly - see
    ManualGuiApp._poll."""

    def __init__(self, endpoint: str, update_queue: "queue.Queue[TelemetryUpdate]"):
        self._ctx = zmq.Context.instance()
        self._socket = self._ctx.socket(zmq.SUB)
        self._socket.setsockopt(zmq.SUBSCRIBE, TAGGED_TELEMETRY_TOPIC)
        self._socket.connect(endpoint)
        self._update_queue = update_queue
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="tagged-telemetry-subscriber")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._socket.close(linger=0)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                _, raw = self._socket.recv_multipart()
            except zmq.ZMQError:
                return
            frame = TaggedTelemetryFrame.from_bytes(raw)
            self._update_queue.put(
                TelemetryUpdate(TAGGED_STREAM_LABEL, frame.channels, (frame.test_id, frame.test_name))
            )


class RawTelemetrySubscriber:
    """Subscribes directly to one device's own raw telemetry stream
    (TELEMETRY_TOPIC/TelemetryFrame - not the tagged one). Unambiguous
    by construction: tied to exactly one device's endpoint, added the
    same way command devices are. Works even with no test case running
    at all, since the raw stream comes straight from the hardware
    driver process."""

    def __init__(self, endpoint: str, update_queue: "queue.Queue[TelemetryUpdate]"):
        self._ctx = zmq.Context.instance()
        self._socket = self._ctx.socket(zmq.SUB)
        self._socket.setsockopt(zmq.SUBSCRIBE, TELEMETRY_TOPIC)
        self._socket.connect(endpoint)
        self._endpoint = endpoint
        self._update_queue = update_queue
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"telemetry-subscriber-{endpoint}")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._socket.close(linger=0)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                _, raw = self._socket.recv_multipart()
            except zmq.ZMQError:
                return
            frame = TelemetryFrame.from_bytes(raw)
            self._update_queue.put(TelemetryUpdate(self._endpoint, frame.channels))


@dataclass
class ChannelGroup:
    """One collapsible sub-group in the available-channels list - one
    per device/endpoint (or the tagged stream), under either the
    Telemetry or Command top-level section. collapsed is remembered
    independently per group and is never forced back open by a redraw -
    only an explicit click on its own header toggles it."""

    label: str
    collapsed: bool = False
    names: set = field(default_factory=set)


def _format_value(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def _to_plot_value(value: Any) -> Optional[float]:
    """Coerce a channel's raw value to something plottable - bool
    becomes 0.0/1.0, int/float pass through, anything else (a string
    state, a missing/None reading) returns None, meaning "don't plot
    this point" rather than crashing on a non-numeric channel."""
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    return None


class ManualGuiApp:
    """Top-level app: an available-channels list (Telemetry channels /
    Command channels, each holding per-device collapsible sub-groups,
    filterable by search), a live telemetry graph for checked telemetry
    channels, a command-channel area (editable JSON params + Send for
    checked command channels), and Add rows for both raw telemetry
    devices and command devices - added by endpoint during the
    session, same pattern for both."""

    def __init__(self, root: tk.Tk):
        self._root = root
        root.title("mytest - manual operator GUI")
        root.geometry("1250x700")

        self._update_queue: "queue.Queue[TelemetryUpdate]" = queue.Queue()
        self._result_queue: "queue.Queue[tuple]" = queue.Queue()

        self._telemetry_groups: Dict[str, ChannelGroup] = {}
        self._known_channels: Dict[Tuple[str, str], tk.BooleanVar] = {}
        self._telemetry_subscribers: Dict[str, Any] = {}
        self._telemetry_history: Dict[Tuple[str, str], Deque[Tuple[float, float]]] = {}
        self._telemetry_axis: Dict[Tuple[str, str], int] = {}
        self._telemetry_color: Dict[Tuple[str, str], str] = {}
        self._color_cycle = itertools.cycle(PLOT_COLORS)
        self._window_s = tk.DoubleVar(value=WINDOW_OPTIONS[0][1])
        self._paused = False

        self._command_groups: Dict[str, ChannelGroup] = {}
        self._known_commands: Dict[Tuple[str, str], tk.BooleanVar] = {}
        self._command_value_vars: Dict[Tuple[str, str], tk.StringVar] = {}
        self._command_result_vars: Dict[Tuple[str, str], tk.StringVar] = {}
        self._command_clients: Dict[str, CommandClient] = {}
        self._command_sending: set = set()  # keys currently in-flight - see _on_send_selected
        self._command_send_buttons: Dict[Tuple[str, str], ttk.Button] = {}

        self._section_collapsed = {"telemetry": False, "command": False}
        self._current_test_id: Optional[str] = None

        self._telemetry_groups[TAGGED_STREAM_LABEL] = ChannelGroup(label=TAGGED_STREAM_LABEL)

        self._build_ui(root)

        tagged_subscriber = TaggedTelemetrySubscriber(self._tagged_endpoint_var.get(), self._update_queue)
        tagged_subscriber.start()
        self._telemetry_subscribers[TAGGED_STREAM_LABEL] = tagged_subscriber

        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(POLL_INTERVAL_MS, self._poll)
        root.after(PLOT_REDRAW_MS, self._redraw_plot)

    # --- UI construction ---

    def _build_ui(self, root: tk.Tk) -> None:
        main = ttk.Frame(root)
        main.pack(fill="both", expand=True, padx=6, pady=6)

        left = ttk.Frame(main)
        left.pack(side="left", fill="both", expand=True, padx=(0, 4))

        test_context_row = ttk.Frame(left)
        test_context_row.pack(fill="x", pady=(0, 4))
        self._test_context_var = tk.StringVar(value="test: (no frame seen yet)")
        ttk.Label(test_context_row, textvariable=self._test_context_var).pack(side="left")
        self._stop_test_button = ttk.Button(test_context_row, text="Stop test", command=self._on_stop_test)
        self._stop_test_button.pack(side="left", padx=(8, 0))
        self._stop_test_button.state(["disabled"])  # enabled once a test_id is actually known - see _apply_update
        self._stop_test_status_var = tk.StringVar(value="")
        ttk.Label(test_context_row, textvariable=self._stop_test_status_var, foreground="gray").pack(
            side="left", padx=(8, 0)
        )

        tagged_row = ttk.Frame(left)
        tagged_row.pack(fill="x", pady=(0, 4))
        ttk.Label(tagged_row, text="tagged endpoint:").pack(side="left")
        self._tagged_endpoint_var = tk.StringVar(value=DEFAULT_TAGGED_TELEMETRY_ENDPOINT)
        ttk.Entry(tagged_row, textvariable=self._tagged_endpoint_var, width=22).pack(side="left", padx=4)
        ttk.Button(tagged_row, text="Reconnect", command=self._reconnect_tagged).pack(side="left")

        self._build_add_rows(left)
        self._build_channel_list(left)

        right = ttk.Frame(main)
        right.pack(side="left", fill="both", expand=True, padx=(4, 0))
        self._build_graph_panel(right)
        self._build_command_panel_area(right)

    def _build_add_rows(self, parent: tk.Widget) -> None:
        add_frame = ttk.LabelFrame(parent, text="Add devices")
        add_frame.pack(fill="x", pady=(0, 4))

        telem_row = ttk.Frame(add_frame)
        telem_row.pack(fill="x", padx=4, pady=4)
        ttk.Label(telem_row, text="telemetry endpoint:").pack(side="left")
        self._add_telemetry_var = tk.StringVar(value="tcp://127.0.0.1:5581")
        ttk.Entry(telem_row, textvariable=self._add_telemetry_var, width=26).pack(side="left", padx=4)
        ttk.Button(telem_row, text="Add", command=self._on_add_telemetry_device).pack(side="left")

        cmd_row = ttk.Frame(add_frame)
        cmd_row.pack(fill="x", padx=4, pady=4)
        ttk.Label(cmd_row, text="command endpoint:").pack(side="left")
        self._add_command_var = tk.StringVar(value="tcp://127.0.0.1:5580")
        ttk.Entry(cmd_row, textvariable=self._add_command_var, width=26).pack(side="left", padx=4)
        ttk.Button(cmd_row, text="Add", command=self._on_add_command_device).pack(side="left")

        self._add_error_var = tk.StringVar(value="")
        ttk.Label(add_frame, textvariable=self._add_error_var, foreground="red").pack(anchor="w", padx=4, pady=(0, 4))

        search_row = ttk.Frame(parent)
        search_row.pack(fill="x", pady=(0, 4))
        ttk.Label(search_row, text="search:").pack(side="left")
        self._search_var = tk.StringVar(value="")
        ttk.Entry(search_row, textvariable=self._search_var, width=30).pack(
            side="left", padx=4, fill="x", expand=True
        )
        self._search_var.trace_add("write", lambda *args: self._rebuild_channel_list())

    def _build_channel_list(self, parent: tk.Widget) -> None:
        list_frame = ttk.LabelFrame(parent, text="available channels")
        list_frame.pack(fill="both", expand=True)
        canvas = tk.Canvas(list_frame, highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=canvas.yview)
        self._checklist_inner = ttk.Frame(canvas)
        self._checklist_inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self._checklist_inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self._bind_mousewheel(canvas)
        self._rebuild_channel_list()

    def _bind_mousewheel(self, canvas: tk.Canvas) -> None:
        """Mouse-wheel/trackpad scrolling for the channel list. Bound
        via bind_all only while the pointer is actually over this
        canvas (Enter/Leave), not permanently - a plain canvas.bind()
        alone doesn't work here because wheel events over the checkbox/
        label widgets *inside* the canvas (added via create_window)
        never bubble up to the canvas itself in Tkinter; bind_all is
        the standard way around that, released on Leave so it doesn't
        hijack scrolling anywhere else in the window."""

        def on_wheel(event: tk.Event) -> None:
            if event.num == 4:
                canvas.yview_scroll(-1, "units")
            elif event.num == 5:
                canvas.yview_scroll(1, "units")
            elif event.delta:
                canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

        def bind_global(_event: tk.Event) -> None:
            canvas.bind_all("<MouseWheel>", on_wheel)
            canvas.bind_all("<Button-4>", on_wheel)
            canvas.bind_all("<Button-5>", on_wheel)

        def unbind_global(_event: tk.Event) -> None:
            canvas.unbind_all("<MouseWheel>")
            canvas.unbind_all("<Button-4>")
            canvas.unbind_all("<Button-5>")

        canvas.bind("<Enter>", bind_global)
        canvas.bind("<Leave>", unbind_global)

    def _build_graph_panel(self, parent: tk.Widget) -> None:
        graph_frame = ttk.LabelFrame(parent, text="Telemetry graph")
        graph_frame.pack(fill="both", expand=True)

        controls = ttk.Frame(graph_frame)
        controls.pack(fill="x", padx=4, pady=4)
        ttk.Label(controls, text="window:").pack(side="left")
        for text, seconds in WINDOW_OPTIONS:
            ttk.Radiobutton(controls, text=text, value=seconds, variable=self._window_s).pack(side="left", padx=2)
        self._pause_button = ttk.Button(controls, text="Pause", command=self._on_toggle_pause)
        self._pause_button.pack(side="left", padx=(12, 0))

        self._figure = Figure(figsize=(5, 3.5), dpi=100)
        self._ax1 = self._figure.add_subplot(111)
        self._ax2 = self._ax1.twinx()
        self._ax1.set_xlabel("seconds ago")
        self._ax1.set_ylabel("axis 1")
        self._ax2.set_ylabel("axis 2")
        self._canvas = FigureCanvasTkAgg(self._figure, master=graph_frame)
        self._canvas.get_tk_widget().pack(fill="both", expand=True, padx=4, pady=4)

        self._telemetry_controls_frame = ttk.Frame(graph_frame)
        self._telemetry_controls_frame.pack(fill="x", padx=4, pady=(0, 4))

    def _build_command_panel_area(self, parent: tk.Widget) -> None:
        cmd_frame = ttk.LabelFrame(parent, text="Command channels")
        cmd_frame.pack(fill="both", expand=True, pady=(4, 0))
        self._command_rows_inner = ttk.Frame(cmd_frame)
        self._command_rows_inner.pack(fill="both", expand=True, padx=4, pady=4)

    # --- available-channels rendering ---

    def _matches_search(self, name: str) -> bool:
        query = self._search_var.get().strip().lower()
        return query in name.lower()

    def _toggle_section(self, section: str) -> None:
        self._section_collapsed[section] = not self._section_collapsed[section]
        self._rebuild_channel_list()

    def _toggle_group(self, group: ChannelGroup) -> None:
        group.collapsed = not group.collapsed
        self._rebuild_channel_list()

    def _render_header(self, row: int, text: str, collapsed: bool, toggle: Callable[[], None]) -> None:
        arrow = "▶" if collapsed else "▼"
        ttk.Button(self._checklist_inner, text=f"{arrow} {text}", command=toggle, style="Toolbutton").grid(
            row=row, column=0, sticky="w", pady=(6, 0)
        )

    def _rebuild_channel_list(self) -> None:
        for child in self._checklist_inner.winfo_children():
            child.destroy()

        row = self._render_section(
            0, "telemetry", "Telemetry channels", self._telemetry_groups, self._known_channels, self._on_telemetry_toggle
        )
        self._render_section(
            row, "command", "Command channels", self._command_groups, self._known_commands, self._on_command_toggle
        )

    def _render_section(
        self,
        row: int,
        section_key: str,
        title: str,
        groups: Dict[str, ChannelGroup],
        selection_vars: Dict[Tuple[str, str], tk.BooleanVar],
        on_toggle: Callable[[Tuple[str, str], tk.BooleanVar], None],
    ) -> int:
        self._render_header(row, title, self._section_collapsed[section_key], lambda: self._toggle_section(section_key))
        row += 1
        if self._section_collapsed[section_key]:
            return row

        for label in sorted(groups):
            group = groups[label]
            self._render_header(row, label, group.collapsed, lambda g=group: self._toggle_group(g))
            row += 1
            if group.collapsed:
                continue
            for name in sorted(n for n in group.names if self._matches_search(n)):
                key = (label, name)
                var = selection_vars.setdefault(key, tk.BooleanVar(value=False))
                ttk.Checkbutton(
                    self._checklist_inner, text=name, variable=var, command=lambda k=key, v=var: on_toggle(k, v)
                ).grid(row=row, column=0, sticky="w", padx=(16, 0))
                row += 1
        return row

    # --- telemetry: checking a channel, its graph controls row, and the plot itself ---

    def _on_telemetry_toggle(self, key: Tuple[str, str], var: tk.BooleanVar) -> None:
        if var.get():
            self._telemetry_axis.setdefault(key, 1)
            if key not in self._telemetry_color:
                self._telemetry_color[key] = next(self._color_cycle)
            self._telemetry_history.setdefault(key, deque())
        else:
            self._telemetry_history.pop(key, None)
        self._rebuild_telemetry_controls()

    def _toggle_axis(self, key: Tuple[str, str]) -> None:
        self._telemetry_axis[key] = 2 if self._telemetry_axis.get(key, 1) == 1 else 1
        self._rebuild_telemetry_controls()

    def _rebuild_telemetry_controls(self) -> None:
        for child in self._telemetry_controls_frame.winfo_children():
            child.destroy()
        checked = sorted(key for key, var in self._known_channels.items() if var.get())
        for row, key in enumerate(checked):
            label, name = key
            color = self._telemetry_color[key]
            tk.Label(self._telemetry_controls_frame, text="  ", background=color, relief="solid", borderwidth=1).grid(
                row=row, column=0, padx=(0, 4), pady=1
            )
            ttk.Label(self._telemetry_controls_frame, text=f"{label}: {name}").grid(row=row, column=1, sticky="w")
            axis = self._telemetry_axis[key]
            ttk.Button(
                self._telemetry_controls_frame, text=f"axis {axis}", command=lambda k=key: self._toggle_axis(k)
            ).grid(row=row, column=2, padx=(8, 0))

    def _on_toggle_pause(self) -> None:
        self._paused = not self._paused
        self._pause_button.config(text="Resume" if self._paused else "Pause")

    def _redraw_plot(self) -> None:
        if not self._paused:
            for ax in (self._ax1, self._ax2):
                for line in list(ax.lines):
                    line.remove()

            window = self._window_s.get()
            now = time.time()
            for key, hist in self._telemetry_history.items():
                if not hist:
                    continue
                # hist is time-ordered ascending (always appended to the
                # right - see _apply_update), so scanning from the most
                # recent end and stopping at the first point outside the
                # window avoids rescanning the full 10-minute buffer every
                # redraw when only a short window is being displayed.
                pts = []
                for t, v in reversed(hist):
                    if now - t > window:
                        break
                    pts.append((t - now, v))
                if not pts:
                    continue
                pts.reverse()
                xs, ys = zip(*pts)
                axis = self._telemetry_axis.get(key, 1)
                target_ax = self._ax1 if axis == 1 else self._ax2
                target_ax.plot(xs, ys, color=self._telemetry_color.get(key, "#000000"))

            self._ax1.set_xlim(-window, 0)
            for ax in (self._ax1, self._ax2):
                ax.relim()
                ax.autoscale_view(scalex=False, scaley=True)
            self._ax1.grid(True, alpha=0.3)
            self._canvas.draw_idle()

        self._root.after(PLOT_REDRAW_MS, self._redraw_plot)

    # --- commands: checking a channel and its editable row ---

    def _on_command_toggle(self, key: Tuple[str, str], var: tk.BooleanVar) -> None:
        self._rebuild_command_rows()

    def _rebuild_command_rows(self) -> None:
        for child in self._command_rows_inner.winfo_children():
            child.destroy()
        self._command_send_buttons.clear()
        row = 0
        for endpoint, name in sorted(key for key, var in self._known_commands.items() if var.get()):
            key = (endpoint, name)
            ttk.Label(self._command_rows_inner, text=f"{endpoint}: {name}").grid(row=row, column=0, sticky="w")
            value_var = self._command_value_vars.setdefault(key, tk.StringVar(value="{}"))
            ttk.Entry(self._command_rows_inner, textvariable=value_var, width=20).grid(
                row=row, column=1, sticky="w", padx=(8, 0)
            )
            send_button = ttk.Button(
                self._command_rows_inner, text="Send", command=lambda e=endpoint, n=name: self._on_send_selected(e, n)
            )
            send_button.grid(row=row, column=2, padx=(4, 0))
            if key in self._command_sending:
                send_button.state(["disabled"])
            self._command_send_buttons[key] = send_button
            row += 1
            result_var = self._command_result_vars.setdefault(key, tk.StringVar(value=""))
            ttk.Label(self._command_rows_inner, textvariable=result_var, foreground="gray").grid(
                row=row, column=0, columnspan=3, sticky="w", padx=(16, 0)
            )
            row += 1

    def _on_send_selected(self, endpoint: str, action: str) -> None:
        key = (endpoint, action)
        if key in self._command_sending:
            return  # a send for this exact channel is already in flight - see _send_command's docstring

        raw_params = self._command_value_vars[key].get().strip() or "{}"
        try:
            params = json.loads(raw_params)
            if not isinstance(params, dict):
                raise ValueError('params must be a JSON object, e.g. {"value": 1.5}')
        except (json.JSONDecodeError, ValueError) as exc:
            self._command_result_vars[key].set(f"bad params: {exc}")
            return

        client = self._command_clients[endpoint]
        result_var = self._command_result_vars[key]
        result_var.set(f"sending {action}...")
        self._command_sending.add(key)
        button = self._command_send_buttons.get(key)
        if button is not None:
            button.state(["disabled"])

        def on_result(ok: bool, result: Any) -> None:
            self._command_sending.discard(key)
            btn = self._command_send_buttons.get(key)
            if btn is not None:
                btn.state(["!disabled"])
            result_var.set(f"ok: {result}" if ok else f"error: {result}")

        self._send_command(client, action, params, on_result)

    # --- adding devices ---

    def _reconnect_tagged(self) -> None:
        """Stops and restarts the tagged-stream subscription against
        whatever endpoint is currently in the field - there's only ever
        one of these (see this module's docstring), so unlike raw
        telemetry/command devices this replaces the existing
        subscription rather than adding another one."""
        endpoint = self._tagged_endpoint_var.get().strip()
        if not endpoint:
            return
        try:
            subscriber = TaggedTelemetrySubscriber(endpoint, self._update_queue)
            subscriber.start()
        except Exception as exc:
            self._add_error_var.set(f"couldn't reconnect tagged stream to {endpoint}: {exc}")
            return
        old = self._telemetry_subscribers.get(TAGGED_STREAM_LABEL)
        if old is not None:
            old.stop()
        self._telemetry_subscribers[TAGGED_STREAM_LABEL] = subscriber
        self._add_error_var.set("")

    def _on_stop_test(self) -> None:
        """Requests a clean stop of whichever test_id the tagged stream
        last reported - reuses tools/stop_test.py's own request_stop()
        rather than re-implementing the marker-file convention here, so
        this button and the standalone stop_test.py CLI can never drift
        apart on what "stop" actually means. Disabled until a test_id is
        actually known (see _apply_update) - nothing to stop otherwise."""
        if self._current_test_id is None:
            return
        path = request_stop(self._current_test_id)
        self._stop_test_status_var.set(f"stop requested for {self._current_test_id} ({path})")

    def _on_add_telemetry_device(self) -> None:
        endpoint = self._add_telemetry_var.get().strip()
        if not endpoint:
            return
        if endpoint in self._telemetry_groups:
            self._add_error_var.set(f"{endpoint} already added")
            return
        try:
            # A SUB socket never errors just because nothing's publishing yet
            # (unlike the command side's list_actions() check), but a
            # malformed endpoint string itself still raises immediately from
            # connect() - catch that here instead of letting it escape an
            # uncaught out of a button callback.
            subscriber = RawTelemetrySubscriber(endpoint, self._update_queue)
            subscriber.start()
        except Exception as exc:
            self._add_error_var.set(f"couldn't subscribe to {endpoint}: {exc}")
            return
        self._telemetry_subscribers[endpoint] = subscriber
        self._telemetry_groups[endpoint] = ChannelGroup(label=endpoint)
        self._add_error_var.set("")
        self._rebuild_channel_list()

    def _on_add_command_device(self) -> None:
        endpoint = self._add_command_var.get().strip()
        if not endpoint:
            return
        if endpoint in self._command_clients:
            self._add_error_var.set(f"{endpoint} already added")
            return
        try:
            client = CommandClient(endpoint=endpoint, timeout_ms=COMMAND_TIMEOUT_MS)
            # Confirms the endpoint actually answers before adding a group for
            # it - a bad/unreachable endpoint shows an error here instead of
            # a group that never populates. Blocks the GUI for up to
            # COMMAND_TIMEOUT_MS on a bad endpoint - acceptable since Add is
            # an occasional, deliberate action, unlike Send.
            actions = client.list_actions()
        except Exception as exc:
            self._add_error_var.set(f"couldn't connect to {endpoint}: {exc}")
            return
        self._add_error_var.set("")
        self._command_clients[endpoint] = client
        self._command_groups[endpoint] = ChannelGroup(label=endpoint, names=set(actions))
        self._rebuild_channel_list()

    def _send_command(
        self, client: CommandClient, action: str, params: Dict[str, Any], on_result: Callable[[bool, Any], None]
    ) -> None:
        """Runs execute() on a background thread and posts the result
        onto result_queue rather than calling on_result directly - a
        slow/unresponsive device's command timeout would otherwise
        freeze the whole GUI, not just this one send.

        Callers must not invoke this a second time for the same
        (endpoint, action) before the first send's result comes back -
        CommandClient's REQ socket enforces strict alternating
        send/recv and isn't safe for two threads to use concurrently.
        _on_send_selected enforces this via _command_sending; this
        method itself doesn't guard against it."""

        def worker() -> None:
            try:
                result = client.execute(action, **params)
                self._result_queue.put((on_result, True, result))
            except CommandClientError as exc:
                self._result_queue.put((on_result, False, str(exc)))
            except Exception as exc:
                self._result_queue.put((on_result, False, repr(exc)))

        threading.Thread(target=worker, daemon=True).start()

    # --- main loop bridge ---

    def _apply_update(self, update: TelemetryUpdate) -> None:
        group = self._telemetry_groups.get(update.device_label)
        if group is None:
            return  # stale update for a device that's since been removed - can't happen today, no removal UI yet
        new_names = set(update.channels) - group.names
        if new_names:
            group.names |= new_names
            self._rebuild_channel_list()

        now = time.time()
        cutoff = now - MAX_HISTORY_S
        for name, value in update.channels.items():
            key = (update.device_label, name)
            hist = self._telemetry_history.get(key)
            if hist is None:
                continue  # not currently checked - see _on_telemetry_toggle
            numeric = _to_plot_value(value)
            if numeric is None:
                continue
            hist.append((now, numeric))
            while hist and hist[0][0] < cutoff:
                hist.popleft()

        if update.test_context is not None:
            test_id, test_name = update.test_context
            self._test_context_var.set(f"test: {test_name} (test_id={test_id})")
            if test_id != self._current_test_id:
                self._current_test_id = test_id
                self._stop_test_status_var.set("")
                self._stop_test_button.state(["!disabled"])

    def _poll(self) -> None:
        try:
            while True:
                update = self._update_queue.get_nowait()
                self._apply_update(update)
        except queue.Empty:
            pass

        try:
            while True:
                on_result, ok, result = self._result_queue.get_nowait()
                on_result(ok, result)
        except queue.Empty:
            pass

        self._root.after(POLL_INTERVAL_MS, self._poll)

    def _on_close(self) -> None:
        for subscriber in self._telemetry_subscribers.values():
            subscriber.stop()
        for client in self._command_clients.values():
            client.close()
        self._root.destroy()


def main() -> None:
    root = tk.Tk()
    ManualGuiApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
