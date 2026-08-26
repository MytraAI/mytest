"""Entry point for the vision-home camera driver process.

    python -m hardware.vision_home.main --scan
    python -m hardware.vision_home.main --camera-source 0
    python -m hardware.vision_home.main --camera-source rtsp://bench-cam/stream
    python -m hardware.vision_home.main --teach-default   # park the fixture first

--camera-source is an index or an address. IT IS BENCH CONFIGURATION, NOT A
CONSTANT: index numbering is per-machine and per-OS, and a built-in camera or a
docking station can push a USB webcam to an unpredictable and sometimes high
index. Windows is the worst for this. --scan prints what this machine answers on,
which is the fastest way to find the right number after anything is replugged.

--teach-default captures the reference view that SHIPS WITH THE DRIVER, so
teaching is something that happened once rather than a step in every run. Park
the fixture at the marker, run it, commit the two files it writes. It takes no
position: the driver deals in pixels and templates, and what the axis reads at
the marker is the test case's business. A `teach` command at runtime still overrides it for that run and
persists to --template, which is what a re-teach after moving something is for.

--template is that per-bench override. Rig calibration, so it lives outside the
repository, and it outranks the committed default when it exists.

There is no --mock. What would be mocked is OpenCV's own capture and matching,
and a fake of those tests nothing that fails in practice - the failures this
driver exists to survive are a real camera's (see camera.py). tests/ covers the
scoring and the backend-probing decisions against synthetic frames instead.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from protocol import asyncio_compat
from protocol.wire import (
    DEFAULT_VISION_HOME_COMMAND_ENDPOINT,
    DEFAULT_VISION_HOME_TELEMETRY_ENDPOINT,
    DEVICE_VISION_HOME,
)

from ..driver_logging import add_logging_args, configure as configure_logging
from ..runner import run
from . import camera
from .vision_home_backend import PACKAGED_TEMPLATE, VisionHomeBackend

logger = logging.getLogger(__name__)

DEFAULT_TEMPLATE_PATH = Path.home() / ".mytest" / "vision_home_template.json"
"""Where a taught reference view is kept. Outside the repository because it is a
property of this bench's camera mount, not of the software - the same reason the
ODrive's saved configuration is left alone."""


def teach_default(camera_source: str) -> int:
    """Capture the reference view that ships with the driver. THE FIXTURE MUST BE AT THE
    MARKER: nothing here can check that, and a view taught elsewhere is worse than none."""
    if not camera.CV2_AVAILABLE:
        print("opencv is not installed - `pip install opencv-python-headless`", file=sys.stderr)
        return 1
    source = camera.resolve_source(camera_source)
    cap, backend_name = camera.open_capture(source)
    if cap is None:
        print(f"camera {camera_source}: {backend_name}", file=sys.stderr)
        return 1
    frame = None
    for _ in range(5):  # let exposure settle before this becomes the reference
        ok, candidate = cap.read()
        if ok:
            frame = candidate
        time.sleep(0.2)
    cap.release()
    if frame is None:
        print(f"camera {camera_source} delivered no frame", file=sys.stderr)
        return 1

    template = camera.center_crop(frame)
    camera.cv2.imwrite(str(PACKAGED_TEMPLATE), template)
    PACKAGED_TEMPLATE.with_suffix(".json").write_text(json.dumps({
        "taught_from_camera_source": camera_source,
        "taught_on_backend": backend_name,
        "roi_frac": camera.ROI_FRAC,
        "template_shape": list(template.shape),
    }, indent=2) + "\n")
    print(f"wrote {PACKAGED_TEMPLATE.name} and {PACKAGED_TEMPLATE.with_suffix('.json').name} "
          f"- {template.shape[1]}x{template.shape[0]} via {backend_name}")
    print("commit both; they are only true while the camera stays where it is")
    return 0


def scan(max_index: int = 6) -> int:
    """Print which camera indices this machine delivers frames from - delivers, not opens."""
    if not camera.CV2_AVAILABLE:
        print("opencv is not installed - `pip install opencv-python-headless`", file=sys.stderr)
        return 1
    found = 0
    for index in range(max_index):
        cap, name = camera.open_capture(index)
        if cap is None:
            print(f"{index}\t-\t{name}")
            continue
        ok, frame = cap.read()
        cap.release()
        if ok:
            print(f"{index}\t{name}\t{frame.shape[1]}x{frame.shape[0]}")
            found += 1
        else:
            print(f"{index}\t{name}\topened, then stopped delivering")
    return 0 if found else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--camera-source", default="0",
                        help="device index, or an RTSP/HTTP address or device path")
    parser.add_argument("--template", default=str(DEFAULT_TEMPLATE_PATH),
                        help="where the taught reference view is kept")
    parser.add_argument("--teach-default", action="store_true",
                        help="capture the reference view that ships with the driver and exit - "
                             "park the fixture at the marker first")
    parser.add_argument("--scan", action="store_true",
                        help="print the camera indices this machine delivers frames from, and exit")
    add_logging_args(parser)
    args = parser.parse_args()

    if args.scan:
        sys.exit(scan())

    if args.teach_default:
        sys.exit(teach_default(args.camera_source))

    configure_logging(args.log_file, device=DEVICE_VISION_HOME)
    backend = VisionHomeBackend(camera_source=args.camera_source, template_path=Path(args.template))
    logger.info("REAL HARDWARE - camera at %s, template %s", args.camera_source, args.template)
    asyncio_compat.run(
        run(backend, DEFAULT_VISION_HOME_COMMAND_ENDPOINT, DEFAULT_VISION_HOME_TELEMETRY_ENDPOINT)
    )
