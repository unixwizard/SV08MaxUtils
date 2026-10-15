# SPDX-License-Identifier: GPL-3.0-or-later
# Patch anchors derived from Klipper (Kevin O'Connor) and Sovol's fork.
"""Build bounded-batch patches against exact supported Sovol sources."""

import argparse
import ast
import difflib
import hashlib
import json
from pathlib import Path

BASE_SHA = "45ba5045cfbfcd765fbdef2e3905145a6192fc4cb75b614b3ff612a55f325511"

MCU_SHA = "9733f1e2e4a7b8921fc7f532a23514167e8d619558a7d3dffbbb063ef665b1e6"
CHECKPOINT_OLD = """        if 'G' in params and 'X' in params and 'Y' in params and 'Z' in params:
            if self.v_sd.cmd_from_sd:
                content = {
                    'commandline': gcmd.get_commandline(),
                    'Z': params['Z']
                }
                with open("/home/sovol/sovol_plr_height", 'w') as height:
                    json.dump(content, height)
                    height.flush()
                    os.fsync(height.fileno())"""
MCU_OLD = """        move_count = config_params['move_count']
        if move_count < self._reserved_move_slots:"""
MCU_NEW = """        # Owner: at most 512 outstanding entries and no more than half
        # the advertised MCU capacity. Existing non-stepper reservations remain.
        move_count = min(512, config_params['move_count'] // 2)
        if move_count <= self._reserved_move_slots:"""
LOG_OLD = (
    """        move_msg = "Configured MCU '%s' (%d moves)" % (self._name, move_count)"""
)
LOG_NEW = """        move_msg = ("Configured MCU '%s' (%d moves advertised; %d host budget, %d reserved)"
                    % (self._name, config_params['move_count'], move_count,
                       self._reserved_move_slots))"""


def replace_once(text, before, after):
    if text.count(before) != 1:
        raise ValueError("Expected exactly one source anchor: " + before[:90])
    return text.replace(before, after, 1)


def patch(source, expected, changes, name):
    if hashlib.sha256(source).hexdigest() != expected:
        raise ValueError("Baseline hash mismatch: " + name)
    newline = "\r\n" if b"\r\n" in source else "\n"
    original = source.decode().replace("\r\n", "\n")
    changed = original
    for before, after in changes:
        changed = replace_once(changed, before, after)
    ast.parse(changed, feature_version=(3, 9))
    diff = "".join(
        difflib.unified_diff(
            original.splitlines(True),
            changed.splitlines(True),
            fromfile="installed/" + name,
            tofile="patched/" + name,
        )
    )
    return changed.replace("\n", newline).encode(), diff


def patch_gcode(source):
    return patch(
        source,
        BASE_SHA,
        [
            ("import os\n", "import os\nfrom .plr_batch import BatchedCheckpoint\n"),
            (
                "        self.is_printer_ready = False\n        # Register g-code commands",
                "        self.is_printer_ready = False\n"
                "        self._plr_checkpoint = BatchedCheckpoint(printer)\n        # Register g-code commands",
            ),
            (CHECKPOINT_OLD, "        _plr_start = tuple(self.last_position)"),
            (
                "        self.move_with_transform(self.last_position, self.speed)\n    # G-Code coordinate manipulation",
                "        self.move_with_transform(self.last_position, self.speed)\n"
                "        self._plr_checkpoint.after_move(self, gcmd, _plr_start)\n    # G-Code coordinate manipulation",
            ),
        ],
        "gcode_move.py",
    )


def patch_mcu(source):
    return patch(source, MCU_SHA, [(MCU_OLD, MCU_NEW), (LOG_OLD, LOG_NEW)], "mcu.py")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gcode-baseline", required=True)
    parser.add_argument("--mcu-baseline", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    destination = Path(args.output)
    destination.mkdir(exist_ok=True, parents=True)
    payloads = {}
    for name, baseline, function in [
        ("gcode_move.py", args.gcode_baseline, patch_gcode),
        ("mcu.py", args.mcu_baseline, patch_mcu),
    ]:
        data, diff = function(Path(baseline).read_bytes())
        (destination / name).write_bytes(data)
        (destination / (name + ".patch")).write_text(diff)
        payloads[name] = data
    for name in ("plr_checkpoint.py", "plr_batch.py"):
        data = (
            Path(__file__).resolve().parents[1] / "klippy/extras" / name
        ).read_bytes()
        (destination / name).write_bytes(data)
        payloads[name] = data
    destinations = {
        name: "/home/sovol/klipper/klippy/extras/" + name
        for name in ("plr_checkpoint.py", "plr_batch.py", "gcode_move.py")
    }
    destinations["mcu.py"] = "/home/sovol/klipper/klippy/mcu.py"
    manifest = dict(
        release="bfp-motion-time-batch-100ms-512slots-20261015-v2",
        originals={
            "gcode_move.py": BASE_SHA,
            "mcu.py": MCU_SHA,
            "plr_checkpoint.py": None,
            "plr_batch.py": None,
        },
        destinations=destinations,
        files={
            name: hashlib.sha256(data).hexdigest() for name, data in payloads.items()
        },
        scope="100 ms motion-endpoint checkpoint batches; worker atomic durable writes; min(512, half capacity) MCU host budget minus existing reservations",
        not_guaranteed=[
            "hardware execution acknowledgements",
            "100 ms bound during storage failure or an indivisible long move",
            "complete compatibility of legacy recovery with all saved state",
            "proven elimination of the original overflow",
        ],
    )
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
