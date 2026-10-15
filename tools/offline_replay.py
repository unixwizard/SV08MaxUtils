"""Verify a recovery bundle against original G-code and pinned native mesh math.

Offline only: no printer imports/connections, no motion, no generated G-code.
Prints scan progress every five seconds. Source and bundle remain unmodified.
"""

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
import types

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from klippy.extras import plr_home
from klippy.extras.plr_recovery import (
    validate_bundle,
    verify_source,
    setup_commands,
    final_modal_commands,
)


def mesh_class():
    package = types.ModuleType("plr_offline_native")
    package.__path__ = []
    sys.modules[package.__name__] = package
    sys.modules[package.__name__ + ".probe"] = types.ModuleType(
        package.__name__ + ".probe"
    )
    module = types.ModuleType(package.__name__ + ".bed_mesh")
    module.__package__ = package.__name__
    path = ROOT / "tests/fixtures/bed_mesh.py.txt"
    exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
    return module.ZMesh


def replay(bundle, root):
    data = validate_bundle(json.loads(bundle.read_bytes()))
    last = [0.0]

    def progress(p):
        now = time.monotonic()
        if now - last[0] >= 5 or p.get("bytes_processed") == p.get("total_bytes"):
            last[0] = now
            print(
                "%s: %s/%s bytes"
                % (p["phase"], p.get("bytes_processed", 0), p.get("total_bytes", 0)),
                flush=True,
            )

    source, identity = verify_source(str(root), data, progress)
    params = data["home_reference"]["mesh_params"]
    allowance = abs(data["runtime_z_offset"]) + max(
        abs(v) for row in data["mesh"]["matrix"] for v in row
    )
    plan = plr_home.make_plan(
        source,
        data["estimated_physical_z"],
        data["resume"]["byte"],
        settings=dict(
            probe_bounds=[
                params["min_x"],
                params["min_y"],
                params["max_x"],
                params["max_y"],
            ],
            height_allowance=allowance,
        ),
        progress=progress,
    )
    cls = mesh_class()
    saved = data["mesh"]
    mesh = cls(saved["params"], "offline-print")
    mesh.build_mesh(saved["points"])
    error = max(
        abs(a - b)
        for aa, bb in zip(mesh.get_mesh_matrix(), saved["matrix"])
        for a, b in zip(aa, bb)
    )
    if error > 1.0e-9:
        raise ValueError("Native saved print mesh reconstruction mismatch")
    home = cls(params, "offline-home")
    home.build_mesh(data["home_reference"]["points"])
    delta = home.calc_z(*plan["probe_xy"]) - home.calc_z(
        plan["settings"]["reference_probe_x"], plan["settings"]["reference_probe_y"]
    )
    result = dict(
        status="offline_replay_passed_not_a_physical_test",
        bundle_sha256=hashlib.sha256(bundle.read_bytes()).hexdigest(),
        source_sha256=data["source_sha256"],
        size_bytes=identity[0],
        restart_byte=data["resume"]["byte"],
        first_command=data["resume"]["command"],
        homing_zone=plan["homing_zone"],
        nozzle_xy=plan["nozzle_xy"],
        travel_z=plan["travel_z"],
        native_mesh_max_error_mm=error,
        homing_datum_delta_mm=delta,
        restoration_commands=setup_commands(data) + final_modal_commands(data),
        printer_connections=0,
        motion_commands_sent=0,
    )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--sd-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = replay(args.bundle, args.sd_root)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, allow_nan=False)
        stream.write("\n")
    print(
        json.dumps(
            {k: v for k, v in result.items() if k != "restoration_commands"}, indent=2
        )
    )
