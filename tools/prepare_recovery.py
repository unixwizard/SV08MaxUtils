"""Validate a reviewed recovery recipe; bind exact source bytes and write bundle.

No printer connection. This does NOT infer execution from a parser cursor.
Also imports the original bfp-recovery-state-v1 assisted-recovery format.
"""

import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from klippy.extras.plr_recovery import SCHEMA, validate_bundle, verify_source


def prepare(recipe, source, reviewed=False):
    if not reviewed:
        raise ValueError("Explicit --reviewed is required after boundary/state review")
    keys = (
        "resume",
        "modal",
        "runtime_z_offset",
        "estimated_physical_z",
        "mesh",
        "home_reference",
        "calibration",
        "reg_drive_current",
        "square_corner_velocity",
        "max_velocity",
        "smooth_time",
    )
    data = {key: recipe[key] for key in keys}
    data.update(
        schema=SCHEMA,
        reviewed=True,
        source_file=source.name,
        source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        excluded_objects=recipe.get("excluded_objects", []),
        unsupported_state=recipe.get("unsupported_state", []),
    )
    if recipe.get("source_sha256", data["source_sha256"]) != data["source_sha256"]:
        raise ValueError("Recipe belongs to a different source")
    validate_bundle(data)
    verify_source(str(source.parent), data)
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recipe", type=Path, required=True)
    parser.add_argument("--gcode", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reviewed", action="store_true")
    args = parser.parse_args()
    data = prepare(json.loads(args.recipe.read_text()), args.gcode, args.reviewed)
    raw = (json.dumps(data, indent=2, allow_nan=False) + "\n").encode()
    with args.output.open("xb") as stream:  # never replace another recovery
        stream.write(raw)
    print("Bundle: " + str(args.output.resolve()))
    print("bundle_sha256: " + hashlib.sha256(raw).hexdigest())
    print("No printer contact, motion, heating, upload or start.")


if __name__ == "__main__":
    main()
