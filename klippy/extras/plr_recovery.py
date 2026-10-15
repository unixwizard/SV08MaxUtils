"""Validated assisted-recovery bundles and fixed, non-extruding resume setup.

This is deliberately NOT a converter from an unverified legacy checkpoint.
The operator/reconstruction tool must establish the exact byte boundary and
modal/probe/mesh state first. No printer or network I/O in this module.
"""

import hashlib
import json
import math
import os
import re

SCHEMA = "sv08max.recovery.v1"
SHA = re.compile(r"^[0-9a-f]{64}$")
NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


def number(value, low, high, label):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not low <= value <= high
    ):
        raise ValueError("Invalid " + label)
    return value


def validate_mesh(mesh, home=False):
    params = mesh["mesh_params" if home else "params"]
    points = mesh["points"]
    for key in ("x_count", "y_count"):
        number(params[key], 3, 121, key)
        if int(params[key]) != params[key]:
            raise ValueError("Noninteger mesh dimensions")
    if len(points) != params["y_count"] or any(
        len(row) != params["x_count"] for row in points
    ):
        raise ValueError("Mesh dimensions differ")
    for axis in "xy":
        number(params["min_" + axis], -50, 550, "mesh minimum")
        number(params["max_" + axis], -50, 550, "mesh maximum")
        if params["min_" + axis] >= params["max_" + axis]:
            raise ValueError("Reversed mesh bounds")
    for row in points:
        for value in row:
            number(value, -10, 10, "mesh value")
    if params["algo"] not in ("bicubic", "lagrange"):
        raise ValueError("Unsupported mesh interpolation")
    for key in ("mesh_x_pps", "mesh_y_pps"):
        number(params[key], 0, 6, key)
        if int(params[key]) != params[key]:
            raise ValueError("Noninteger mesh interpolation")
    number(params["tension"], 0, 2, "mesh tension")
    if not home:
        number(mesh["fade_start"], 0, 500, "fade start")
        number(mesh["fade_end"], 0, 500, "fade end")
        if mesh["fade_end"] <= mesh["fade_start"]:
            raise ValueError("v1 requires an enabled, positive fade interval")
        # v1 reproduces the tested automatic-average fade target, no extra
        # mesh/tool offset. Other configurations need explicit implementation.
        if mesh.get("base_fade_target") is not None or mesh.get("tool_offset", 0) != 0:
            raise ValueError("Custom mesh fade target/tool offset unsupported")
        expected_x = (params["x_count"] - 1) * (params["mesh_x_pps"] + 1) + 1
        expected_y = (params["y_count"] - 1) * (params["mesh_y_pps"] + 1) + 1
        if len(mesh["matrix"]) != expected_y or any(
            len(row) != expected_x for row in mesh["matrix"]
        ):
            raise ValueError("Interpolated mesh dimensions differ")
        for row in mesh["matrix"]:
            for value in row:
                number(value, -10, 10, "interpolated mesh value")


def validate_bundle(data):
    if data["schema"] != SCHEMA or data.get("reviewed") is not True:
        raise ValueError("A reviewed sv08max.recovery.v1 bundle is required")
    if not SHA.fullmatch(data["source_sha256"]):
        raise ValueError("Invalid source SHA256")
    filename = data["source_file"]
    if (
        not filename
        or any(c in filename for c in "\r\n\x00")
        or os.path.isabs(filename)
        or ".." in filename.replace("\\", "/").split("/")
    ):
        raise ValueError("source_file must be relative to virtual SD root")
    byte = data["resume"]["byte"]
    if isinstance(byte, bool) or not isinstance(byte, int) or byte <= 0:
        raise ValueError("Invalid resume byte")
    cmd = data["resume"]["command"]
    if (
        not cmd
        or any(c in cmd for c in "\r\n\x00")
        or not re.match(r"^G[01](?:\s|[XYZEF])", cmd)
    ):
        raise ValueError("First resumed command must be one reviewed G0/G1 line")
    number(data["estimated_physical_z"], 0, 490, "estimated physical Z")
    number(data["runtime_z_offset"], -2, 2, "runtime Z offset")
    number(data["reg_drive_current"], 0, 31, "Eddy drive current")
    cal = data["calibration"]
    if len(cal) < 3 or any(len(p) != 2 for p in cal):
        raise ValueError("Invalid Eddy calibration")
    for z, frequency in cal:
        number(z, -5, 50, "calibration Z")
        number(frequency, 1, 100000000, "calibration frequency")
    if not all(a[0] < b[0] and a[1] > b[1] for a, b in zip(cal, cal[1:])):
        raise ValueError("Nonmonotonic Eddy calibration")
    validate_mesh(data["mesh"])
    validate_mesh(data["home_reference"], home=True)
    modal = data["modal"]
    if modal["absolute"] is not True or type(modal["absolute_e"]) is not bool:
        raise ValueError("v1 requires absolute XYZ at the restart boundary")
    if len(modal["xyz"]) != 3:
        raise ValueError("Invalid resume XYZ")
    for v in modal["xyz"]:
        number(v, 0, 500, "resume XYZ")
    number(modal["e"], -10000000, 10000000, "resume E")
    number(modal["feed"], 1, 60000, "feed rate")
    number(modal["speed_factor"], 0.01, 3, "speed factor")
    number(modal["extrude_factor"], 0.1, 3, "extrusion factor")
    number(modal["bed"], 20, 120, "bed target")
    number(modal["nozzle"], 170, 300, "nozzle target")
    number(modal["pa"]["ADVANCE"], 0, 1, "pressure advance")
    number(data["smooth_time"], 0.001, 0.2, "pressure advance smooth time")
    number(data["max_velocity"], 1, 1000, "maximum velocity")
    number(data["square_corner_velocity"], 0, 20, "square corner velocity")
    number(modal["limits"]["ACCEL"], 1, 50000, "acceleration")
    number(
        modal["limits"]["ACCEL_TO_DECEL"], 0, modal["limits"]["ACCEL"], "accel to decel"
    )
    number(modal["layer"], 0, 100000, "layer")
    for fan, speed in modal["fans"].items():
        if fan not in ("0", "1", "2", "3"):
            raise ValueError("Unsupported fan")
        number(speed, 0, 1, "fan speed")
    # Preserve object metadata without permitting arbitrary injected commands.
    names = set()
    for command in modal["objects"]:
        match = re.fullmatch(
            r"EXCLUDE_OBJECT_DEFINE NAME=([A-Za-z0-9_.-]+)(?: CENTER=([-+0-9.,]+))?(?: POLYGON=(\[[-+0-9.,\[\] ]+\]))?",
            command,
        )
        if not match:
            raise ValueError("Invalid object definition")
        if match[1] in names:
            raise ValueError("Duplicate object name")
        names.add(match[1])
        if match[2] is not None:
            values = [float(v) for v in match[2].split(",")]
            if len(values) != 2:
                raise ValueError("Invalid object center")
            for v in values:
                number(v, -50, 550, "object center")
        if match[3] is not None:
            polygon = json.loads(match[3])
            if len(polygon) < 3 or any(len(point) != 2 for point in polygon):
                raise ValueError("Invalid object polygon")
            for point in polygon:
                for v in point:
                    number(v, -50, 550, "object polygon")
    active = modal["active_object"]
    if active is not None:
        prefix = "EXCLUDE_OBJECT_START NAME="
        if not active.startswith(prefix) or active[len(prefix) :] not in names:
            raise ValueError("Unknown active object")
    if data.get("excluded_objects", []) or data.get("unsupported_state", []):
        raise ValueError(
            "Excluded objects or unsupported saved state require reconstruction"
        )
    return data


def verify_source(root, data, progress=None):
    root = os.path.realpath(root)
    path = os.path.realpath(os.path.join(root, data["source_file"]))
    if os.path.commonpath([root, path]) != root:
        raise ValueError("Source escapes virtual SD root")
    before = os.stat(path)
    digest = hashlib.sha256()
    done = 0
    with open(path, "rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
            done += len(block)
            if progress:
                progress(
                    dict(
                        phase="verifying_source",
                        bytes_processed=done,
                        total_bytes=before.st_size,
                    )
                )
        byte = data["resume"]["byte"]
        if byte >= before.st_size:
            raise ValueError("Resume byte outside source")
        stream.seek(byte - 1)
        if stream.read(1) != b"\n":
            raise ValueError("Resume byte is not a line boundary")
        if stream.readline().decode("ascii").strip() != data["resume"]["command"]:
            raise ValueError("Resume command differs at exact byte")
    after = os.stat(path)
    if (before.st_size, before.st_mtime_ns) != (
        after.st_size,
        after.st_mtime_ns,
    ) or digest.hexdigest() != data["source_sha256"]:
        raise ValueError("Source changed or SHA256 mismatch")
    return path, (after.st_size, after.st_mtime_ns)


def setup_commands(data):
    """Only non-motion restoration; no heat, purge, retraction or startup macro."""
    m = data["modal"]
    ratio = max(
        0.0, min(1.0, 1.0 - m["limits"]["ACCEL_TO_DECEL"] / m["limits"]["ACCEL"])
    )
    return [
        "G90",
        "M83",
        "M220 S100",
        "M221 S100",
        "SET_GCODE_OFFSET X=0 Y=0 Z=%.9f MOVE=0" % data["runtime_z_offset"],
        "SET_PRESSURE_ADVANCE ADVANCE=%.9f SMOOTH_TIME=%.9f"
        % (m["pa"]["ADVANCE"], data["smooth_time"]),
        "SET_VELOCITY_LIMIT VELOCITY=%.9f ACCEL=%.9f MINIMUM_CRUISE_RATIO=%.9f SQUARE_CORNER_VELOCITY=%.9f"
        % (
            data["max_velocity"],
            m["limits"]["ACCEL"],
            ratio,
            data["square_corner_velocity"],
        ),
        "SET_PRINT_STATS_INFO CURRENT_LAYER=%d" % m["layer"],
        *m["objects"],
        *([m["active_object"]] if m["active_object"] else []),
        *[
            "SET_FAN_SPEED FAN=fan%s SPEED=%.9f" % (k, v)
            for k, v in sorted(m["fans"].items())
        ],
    ]


def final_modal_commands(data):
    m = data["modal"]
    return [
        "M221 S%.9f" % (m["extrude_factor"] * 100),
        "G92 E%.9f" % m["e"],
        "M82" if m["absolute_e"] else "M83",
        "M220 S%.9f" % (m["speed_factor"] * 100),
        "G1 F%.9f" % m["feed"],
    ]


def claim_attempt(path, bundle_sha):
    # Durable, exclusive one-shot record BEFORE any resume action. An error or
    # ambiguous start keeps the record; operator inspection is then required.
    with open(path, "x", encoding="utf-8") as stream:
        json.dump(
            dict(bundle_sha256=bundle_sha, status="attempted_do_not_retry"), stream
        )
        stream.flush()
        os.fsync(stream.fileno())
    if hasattr(os, "O_DIRECTORY"):
        fd = os.open(os.path.dirname(path), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
