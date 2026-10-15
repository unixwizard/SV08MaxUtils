"""Read-only Moonraker state capture; not an executed-position checkpoint.

Run before state is lost (and after calibration/mesh changes). Capturing a
running printer is read-only. Never publishes the output; keep it private.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

OBJECTS = (
    "configfile",
    "bed_mesh",
    "gcode_move",
    "toolhead",
    "extruder",
    "heater_bed",
    "virtual_sdcard",
    "print_stats",
    "exclude_object",
    "motion_report",
    "fan_generic fan0",
    "fan_generic fan1",
    "fan_generic fan2",
    "fan_generic fan3",
    "probe_eddy_current eddy",
)


def capture(base):
    parsed = urlsplit(base)
    if parsed.scheme not in ("http", "https") or parsed.username or parsed.password:
        raise ValueError(
            "Provide an HTTP(S) Moonraker base URL without embedded credentials"
        )
    request = Request(
        base.rstrip("/")
        + "/printer/objects/query?"
        + "&".join(quote(n) for n in OBJECTS),
        method="GET",
    )
    api_key = os.environ.get("SV08_MOONRAKER_API_KEY")
    if api_key:
        request.add_header("X-Api-Key", api_key)
    with urlopen(request, timeout=15) as response:
        data = json.load(response)
    if "result" not in data or "status" not in data["result"]:
        raise ValueError("Not a Moonraker status response")
    return dict(
        schema="sv08max.readonly-context.v1",
        utc=datetime.now(timezone.utc).isoformat(),
        warning="Parser state may be ahead of executed motion. Not a restart cursor or ready recovery bundle.",
        result=data["result"],
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = capture(args.url)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    print(
        "Saved private context to "
        + str(args.output.resolve())
        + "; GET only, no printer command sent."
    )
