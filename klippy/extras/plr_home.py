"""G-code-informed recovery homing for the Sovol SV08 MAX (Python 3.9+).

Standalone plan: python plr_home.py job.gcode --z ESTIMATE --byte EXACT_OFFSET
Klipper integration: [sv08_plr] and the macros in config/plr.cfg.
This establishes coordinates only. It never heats, extrudes, resumes, runs QGL,
or changes the recovery checkpoint. Existing PLR write/checkpoint logic is not
replaced. Saved Z is an estimate for the initial upward move, not a measurement.
Rear-edge homing is preferred. Front fallback checks the full-width X rail,
gantry-mounted PTFE/wiring bracket, its mounting foot, and a conservative
flexible-umbilical envelope derived from Sovol's official STEP assembly.
"""

import argparse
import hashlib
import json
import math
import os
import re
import threading
import time


DEFAULTS = dict(
    bed_x=500.0,
    bed_y=500.0,
    cell=2.0,
    search_step=5.0,
    head_radius=75.0,
    margin=5.0,
    probe_x=-19.8,
    probe_y=-0.75,
    rear_inset=10.0,
    front_inset=15.0,
    lift=10.0,
    descent_floor=-5.0,
    front_fallback=1.0,
    height_allowance=0.0,
    z_max=505.0,
    top_margin=5.0,
    xy_speed=50.0,
    z_speed=5.0,
    reference_probe_x=251.2,
    reference_probe_y=250.25,
)
PARAM = re.compile(r"([XYZEFIJKR])\s*([-+]?(?:\d*\.\d+|\d+\.?\d*))")
COMMAND = re.compile(r"^(?:G\d+(?:\.\d+)?|M\d+(?:\.\d+)?)(?=\s|[A-Z]|$)")
WIDTH = re.compile(r"^;\s*(?:WIDTH:|.*extrusion width =\s*)([\d.]+)")


class PlanError(ValueError):
    pass


def _distance_transform(values):
    """Exact squared Euclidean distance transform of a finite 1-D array."""
    n = len(values)
    sites = [0] * n
    boundaries = [0.0] * (n + 1)
    boundaries[0], boundaries[1] = -float("inf"), float("inf")
    k = 0
    for q in range(1, n):
        s = ((values[q] + q * q) - (values[sites[k]] + sites[k] * sites[k])) / (
            2.0 * (q - sites[k])
        )
        while s <= boundaries[k]:
            k -= 1
            s = ((values[q] + q * q) - (values[sites[k]] + sites[k] * sites[k])) / (
                2.0 * (q - sites[k])
            )
        k += 1
        sites[k], boundaries[k], boundaries[k + 1] = q, s, float("inf")
    out, k = [], 0
    for q in range(n):
        while boundaries[k + 1] < q:
            k += 1
        out.append((q - sites[k]) ** 2 + values[sites[k]])
    return out


class Footprint:
    def __init__(self, settings):
        self.cfg = settings
        self.cell = settings["cell"]
        self.nx = int(math.ceil(settings["bed_x"] / self.cell))
        self.ny = int(math.ceil(settings["bed_y"] / self.cell))
        self.heights = [-1.0] * (self.nx * self.ny)
        self.max_width = 0.8  # conservative fallback for missing WIDTH comments
        self.segments = 0

    def add_line(self, start, end):
        self.segments += 1
        dx, dy = end[0] - start[0], end[1] - start[1]
        count = max(1, int(math.ceil(math.hypot(dx, dy) / self.cell)))
        z = max(start[2], end[2])
        for step in range(count + 1):
            t = step / count
            x, y = start[0] + dx * t, start[1] + dy * t
            # Outside-bed deposited material could strike gantry end hardware;
            # this stock-bed model does not certify it by silently clipping it.
            if not (0 <= x <= self.cfg["bed_x"] and 0 <= y <= self.cfg["bed_y"]):
                raise PlanError("Deposited toolpath outside modeled bed bounds")
            ix, iy = (
                min(self.nx - 1, int(x / self.cell)),
                min(self.ny - 1, int(y / self.cell)),
            )
            index = iy * self.nx + ix
            if z > self.heights[index]:
                self.heights[index] = z

    def distances(self):
        far = 1.0e12
        columns = [
            [0.0 if self.heights[y * self.nx + x] >= 0 else far for y in range(self.ny)]
            for x in range(self.nx)
        ]
        columns = [_distance_transform(col) for col in columns]
        return [
            _distance_transform([columns[x][y] for x in range(self.nx)])
            for y in range(self.ny)
        ]


def _arc_points(start, end, p, clockwise, spacing):
    if "K" in p:
        raise PlanError("Non-XY arc is unsupported")
    if "R" in p:
        dx, dy = end[0] - start[0], end[1] - start[1]
        chord, radius = math.hypot(dx, dy), abs(p["R"])
        if chord < 1.0e-9 or chord > 2 * radius + 1.0e-6:
            raise PlanError("Invalid R arc")
        sign = (-1.0 if clockwise else 1.0) * (-1.0 if p["R"] < 0 else 1.0)
        h = math.sqrt(max(0.0, radius * radius - chord * chord / 4))
        cx = (start[0] + end[0]) / 2 - sign * dy * h / chord
        cy = (start[1] + end[1]) / 2 + sign * dx * h / chord
    else:
        if not ("I" in p or "J" in p):
            raise PlanError("Arc lacks center or radius")
        cx, cy = start[0] + p.get("I", 0.0), start[1] + p.get("J", 0.0)
        radius = math.hypot(start[0] - cx, start[1] - cy)
        if abs(math.hypot(end[0] - cx, end[1] - cy) - radius) > 0.1:
            raise PlanError("Inconsistent arc radius")
    a = math.atan2(start[1] - cy, start[0] - cx)
    b = math.atan2(end[1] - cy, end[0] - cx)
    sweep = (b - a) % (2 * math.pi)
    if clockwise:
        sweep = sweep - 2 * math.pi
    elif abs(sweep) < 1.0e-10:
        sweep = 2 * math.pi
    count = max(1, int(math.ceil(abs(sweep) * radius / spacing)))
    if count > 100000:
        raise PlanError("Unreasonable arc length")
    for i in range(1, count + 1):
        t = i / count
        yield (
            cx + radius * math.cos(a + sweep * t),
            cy + radius * math.sin(a + sweep * t),
            start[2] + (end[2] - start[2]) * t,
        )


def scan_file(path, settings, byte_limit=None, checkpoint=None, progress=None):
    fp = Footprint(settings)
    pos, e, absolute, absolute_e = [0.0, 0.0, 0.0], 0.0, True, True
    known_xy = [False, False]
    max_z = prefix_z = 0.0
    checkpoint_z = None
    matches, offset, line_no = 0, 0, 0
    digest = hashlib.sha256()
    stat_before = os.stat(path)
    next_report = 256 * 1024
    if progress is not None:
        progress(
            dict(
                phase="scanning",
                bytes_processed=0,
                total_bytes=stat_before.st_size,
                lines_processed=0,
            )
        )
    with open(path, "rb") as stream:
        for raw in stream:
            # Report completed lines, including comments, without a clock call
            # or reactor interaction for every G-code line.
            if progress is not None and offset >= next_report:
                progress(
                    dict(
                        phase="scanning",
                        bytes_processed=offset,
                        total_bytes=stat_before.st_size,
                        lines_processed=line_no,
                    )
                )
                next_report = offset + 256 * 1024
            digest.update(raw)
            line_no += 1
            offset += len(raw)
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith(";"):
                match = WIDTH.match(line)
                if match:
                    fp.max_width = max(fp.max_width, float(match.group(1)))
                continue
            executable = line.split(";", 1)[0].strip().upper()
            if not executable:
                continue
            motion_command = COMMAND.match(executable)
            cmd = (
                motion_command.group(0)
                if motion_command
                else executable.split(None, 1)[0]
            )
            # All coordinates in the supported files are bed-coordinate mm.
            if cmd in (
                "G20",
                "G18",
                "G19",
                "G90.1",
                "G53",
                "G54",
                "G55",
                "G56",
                "G57",
                "G58",
                "G59",
            ):
                raise PlanError(
                    "Unsupported coordinate mode %s at line %d" % (cmd, line_no)
                )
            if cmd == "G90":
                absolute = True
            elif cmd == "G91":
                absolute = False
            elif cmd == "M82":
                absolute_e = True
            elif cmd == "M83":
                absolute_e = False
            elif cmd == "G28":
                if fp.segments:
                    raise PlanError("Mid-print homing is unsupported")
                known_xy = [False, False]
            elif cmd == "G92":
                p = dict((k, float(v)) for k, v in PARAM.findall(executable[3:]))
                if any(a in p for a in "XYZ"):
                    raise PlanError("G92 XYZ coordinate shift is unsupported")
                e = p.get("E", e)
            elif cmd == "SET_GCODE_OFFSET" and re.search(
                r"\b[XY](?:_ADJUST)?=", executable
            ):
                raise PlanError("XY G-code offset is unsupported")
            elif cmd in ("G0", "G00", "G1", "G01", "G2", "G02", "G3", "G03"):
                p = dict(
                    (k, float(v)) for k, v in PARAM.findall(executable[len(cmd) :])
                )
                end = list(pos)
                for i, axis in enumerate("XYZ"):
                    if axis in p:
                        end[i] = p[axis] if absolute else pos[i] + p[axis]
                        if i < 2 and absolute:
                            known_xy[i] = True
                new_e = (
                    p.get("E", e) if absolute and absolute_e else e + p.get("E", 0.0)
                )
                if not all(math.isfinite(v) for v in list(p.values()) + end + [new_e]):
                    raise PlanError("Non-finite G-code coordinate")
                if any(abs(v) > 10000 for v in end):
                    raise PlanError("Unreasonable G-code coordinate")
                if new_e > e + 1.0e-9 and (
                    end[:2] != pos[:2] or cmd in ("G2", "G02", "G3", "G03")
                ):
                    if not all(known_xy):
                        raise PlanError("Extrusion before known XY coordinates")
                    max_z = max(max_z, pos[2], end[2])
                    if cmd in ("G2", "G02", "G3", "G03"):
                        last = pos
                        for point in _arc_points(
                            pos, end, p, cmd in ("G2", "G02"), settings["cell"] / 2
                        ):
                            fp.add_line(last, point)
                            last = point
                    else:
                        fp.add_line(pos, end)
                pos, e = end, new_e
            if byte_limit is not None and offset - len(raw) < byte_limit:
                prefix_z = max_z
            if checkpoint and line == checkpoint.strip():
                matches += 1
                checkpoint_z = max_z
    stat_after = os.stat(path)
    if (stat_before.st_size, stat_before.st_mtime_ns) != (
        stat_after.st_size,
        stat_after.st_mtime_ns,
    ):
        raise PlanError("G-code changed while planning")
    if not fp.segments or max(fp.heights) < 0:
        raise PlanError("No in-bed deposited toolpaths found")
    if byte_limit is not None:
        if not 0 < byte_limit <= offset:
            raise PlanError("BYTE is outside the G-code file")
        printed_z, height_source = prefix_z, "byte prefix (conservative read-ahead)"
    elif checkpoint_z is not None:
        printed_z, height_source = checkpoint_z, "last matching saved command"
    else:
        printed_z, height_source = max_z, "whole-file maximum; no matching checkpoint"
    if progress is not None:
        progress(
            dict(
                phase="scanning",
                bytes_processed=offset,
                total_bytes=stat_before.st_size,
                lines_processed=line_no,
            )
        )
    return fp, dict(
        sha256=digest.hexdigest(),
        size_bytes=offset,
        max_printed_z=printed_z,
        max_file_z=max_z,
        height_source=height_source,
        checkpoint_matches=matches,
        extrusion_segments=fp.segments,
    )


# Bounds are in printer coordinates. X is bed-fixed (not relative to nozzle).
# Y and lower Z are relative to the nozzle. Rounded OUTWARD from official CAD;
# see docs/GEOMETRY.md. Flexible routing is an intentionally wider assumption.
GANTRY_ENVELOPES = (
    ("x_beam_and_rail", 0.0, 500.0, 30.0, 65.0, 43.0),
    ("ptfe_bracket_mount", 355.0, 420.0, 58.0, 89.0, 43.0),
    ("ptfe_wiring_bracket", 320.0, 500.0, 65.0, 88.0, 63.0),
    ("flexible_tube_and_wiring", 0.0, 500.0, -10.0, 300.0, 90.0),
)


def gantry_conflicts(fp, cfg, y, printed_z):
    """Check the entire vertical descent prism, not just the probe point.

    Every occupied raster cell is a solid column to its conservative height.
    All-file footprints reserve future locations; heights are capped by the
    greatest possible already-deposited prefix height. This can overreject.
    XY homing/travel occurs above that GLOBAL height, so its swept prism is
    clear under the same undisturbed-print / correct-Z-estimate assumptions.
    """
    pad = cfg["margin"] + 2 * math.sqrt(2) * fp.cell + fp.max_width / 2
    conflicts = []
    for name, xmin, xmax, ymin, ymax, bottom in GANTRY_ENVELOPES:
        low = max(0, int(math.floor((y + ymin - pad) / fp.cell)))
        high = min(fp.ny - 1, int(math.floor((y + ymax + pad) / fp.cell)))
        left = max(0, int(math.floor((xmin - pad) / fp.cell)))
        right = min(fp.nx - 1, int(math.floor((xmax + pad) / fp.cell)))
        ceiling = bottom + cfg["descent_floor"] - cfg["margin"]
        if any(
            fp.heights[j * fp.nx + i] >= 0
            and min(fp.heights[j * fp.nx + i], printed_z)
            + cfg.get("height_allowance", 0.0)
            >= ceiling
            for j in range(low, high + 1)
            for i in range(left, right + 1)
        ):
            conflicts.append(name)
    return conflicts


def choose_location(fp, cfg, printed_z=None):
    dist = fp.distances()
    # Sampling, cell-center lookup and line-width uncertainty are all included.
    discretization = 2 * math.sqrt(2) * cfg["cell"] + fp.max_width / 2
    required = cfg["head_radius"] + cfg["margin"] + discretization
    printed_z = max(fp.heights) if printed_z is None else printed_z
    border = cfg["head_radius"] + cfg["margin"]
    rear_limit = min(
        cfg["bed_y"] - max(cfg["margin"], cfg["rear_inset"]),
        cfg["bed_y"] - cfg["margin"] - cfg["probe_y"],
    )
    rear = math.floor(rear_limit / cfg["search_step"]) * cfg["search_step"]
    limits = cfg.get("probe_bounds", [0.0, 0.0, cfg["bed_x"], cfg["bed_y"]])
    front = (
        math.ceil(
            max(
                cfg["front_inset"],
                cfg["margin"] - cfg["probe_y"],
                limits[1] - cfg["probe_y"],
            )
            / cfg["search_step"]
        )
        * cfg["search_step"]
    )
    rows = [("rear", rear)]
    if cfg.get("front_fallback", 1):
        rows.append(("front_fallback", front))
    rejected = []
    for zone, y in rows:
        best = None
        conflicts = gantry_conflicts(fp, cfg, y, printed_z)
        if conflicts:
            rejected.append(zone + ": " + ", ".join(conflicts))
            continue
        for xi in range(
            int(math.ceil(border / cfg["search_step"])),
            int(math.floor((cfg["bed_x"] - border) / cfg["search_step"])) + 1,
        ):
            x = xi * cfg["search_step"]
            px, py = x + cfg["probe_x"], y + cfg["probe_y"]
            limits = cfg.get("probe_bounds", [0.0, 0.0, cfg["bed_x"], cfg["bed_y"]])
            if not (limits[0] <= px <= limits[2] and limits[1] <= py <= limits[3]):
                continue
            if not (
                cfg["margin"] <= y <= cfg["bed_y"] - cfg["margin"]
                and cfg["margin"] <= px <= cfg["bed_x"] - cfg["margin"]
                and cfg["margin"] <= py <= cfg["bed_y"] - cfg["margin"]
            ):
                continue
            d = (
                math.sqrt(
                    dist[min(fp.ny - 1, int(y / cfg["cell"]))][
                        min(fp.nx - 1, int(x / cfg["cell"]))
                    ]
                )
                * cfg["cell"]
            )
            pd = (
                math.sqrt(
                    dist[min(fp.ny - 1, int(py / cfg["cell"]))][
                        min(fp.nx - 1, int(px / cfg["cell"]))
                    ]
                )
                * cfg["cell"]
            )
            if d < required or pd < cfg["margin"] + discretization:
                continue
            side_clearance = min(x, cfg["bed_x"] - x)
            score = (min(d - discretization, side_clearance) - cfg["head_radius"], -x)
            if best is None or score > best[0]:
                best = (
                    score,
                    dict(
                        nozzle_xy=[x, y],
                        probe_xy=[px, py],
                        toolpath_clearance_mm=round(d - discretization, 3),
                        edge_clearance_mm=min(side_clearance, y, cfg["bed_y"] - y),
                        head_radius_mm=cfg["head_radius"],
                        homing_zone=zone,
                        checked_envelopes=[v[0] for v in GANTRY_ENVELOPES],
                        rear_edge_inset_mm=cfg["bed_y"] - y,
                    ),
                )
        if best is not None:
            return best[1]  # Rear wins whenever available, regardless of front score.
        rejected.append(zone + ": no toolhead/probe/reference-mesh clearance")
    raise PlanError("No clear homing location; " + "; ".join(rejected))


def make_plan(path, z, byte_limit=None, checkpoint=None, settings=None, progress=None):
    cfg = dict(DEFAULTS)
    cfg.update(settings or {})
    if not math.isfinite(z) or z < 0:
        raise PlanError("A nonnegative estimated current Z is required")
    fp, info = scan_file(path, cfg, byte_limit, checkpoint, progress=progress)
    travel_z = max(
        z + cfg["lift"],
        info["max_printed_z"] + cfg["margin"] + cfg.get("height_allowance", 0.0),
    )
    if travel_z > cfg["z_max"] - cfg["top_margin"]:
        raise PlanError("Insufficient estimated upper Z clearance for recovery lift")
    if progress is not None:
        progress(dict(phase="selecting_location"))
    info.update(choose_location(fp, cfg, info["max_printed_z"]))
    info.update(
        file=os.path.realpath(path),
        file_mtime_ns=os.stat(path).st_mtime_ns,
        estimated_start_z=z,
        travel_z=travel_z,
        lift_mm=travel_z - z,
        settings=cfg,
        assumption="Print has not moved; saved Z approximates current height. No physical guarantee.",
    )
    return info


def execute_home(printer, gcode, plan, progress=None):
    """Only the explicit non-dry-run command calls this motion sequence."""
    toolhead = printer.lookup_object("toolhead")
    homing = printer.lookup_object("homing")
    bed_mesh = printer.lookup_object("bed_mesh", None)
    mesh = bed_mesh.z_mesh if bed_mesh is not None else None
    cfg = plan["settings"]
    delta = 0.0
    if mesh is not None:
        px, py = plan["probe_xy"]
        params = mesh.get_mesh_params()
        for mx, my in ((px, py), (cfg["reference_probe_x"], cfg["reference_probe_y"])):
            if not (
                params["min_x"] <= mx <= params["max_x"]
                and params["min_y"] <= my <= params["max_y"]
            ):
                raise PlanError(
                    "Homing datum is outside the loaded reference mesh; refusing clamped correction"
                )
        delta = mesh.calc_z(px, py) - mesh.calc_z(
            cfg["reference_probe_x"], cfg["reference_probe_y"]
        )
    if abs(delta) > cfg["margin"]:
        raise PlanError("Existing mesh datum adjustment exceeds the clearance margin")
    toolhead.wait_moves()
    if bed_mesh is not None:
        bed_mesh.set_mesh(None)
    success = False
    try:
        if progress is not None:
            progress(dict(phase="clearance_lift"))
        pos = toolhead.get_position()
        pos[2] = plan["estimated_start_z"]
        # Mark ONLY Z provisionally positioned, then lift vertically. No XY is
        # assumed homed. Actual Z homing below replaces this temporary estimate.
        toolhead.set_position(pos, homing_axes=(2,))
        toolhead.manual_move([None, None, plan["travel_z"]], cfg["z_speed"])
        toolhead.wait_moves()
        # Direct native handler avoids both the G28 macro and homing_override.
        for axis in ("X", "Y"):
            if progress is not None:
                progress(dict(phase="homing_" + axis.lower()))
            homing.cmd_G28(
                gcode.create_gcode_command("G28", "G28 " + axis, {axis: "0"})
            )
        x, y = plan["nozzle_xy"]
        if progress is not None:
            progress(dict(phase="moving_to_clear_location"))
        toolhead.manual_move([x, y, None], cfg["xy_speed"])
        toolhead.wait_moves()
        if progress is not None:
            progress(dict(phase="homing_z"))
        homing.cmd_G28(gcode.create_gcode_command("G28", "G28 Z", {"Z": "0"}))
        # Transfer the local bed reference back to the normal homing reference
        # using the existing mesh when available; never run a fresh mesh/QGL.
        pos = toolhead.get_position()
        pos[2] += delta
        toolhead.set_position(pos)
        if progress is not None:
            progress(dict(phase="parking"))
        toolhead.manual_move([None, None, plan["travel_z"]], cfg["z_speed"])
        toolhead.wait_moves()
        success = True
    finally:
        if bed_mesh is not None:
            bed_mesh.set_mesh(mesh)
        printer.lookup_object("gcode_move").reset_last_position()
        if not success:
            # Do not leave the provisional estimated Z marked as measured.
            toolhead.get_kinematics().note_z_not_homed()
    return delta


class PLRHome:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object("gcode")
        self.cfg = {}
        signed = (
            "probe_x",
            "probe_y",
            "reference_probe_x",
            "reference_probe_y",
            "descent_floor",
        )
        for key, value in DEFAULTS.items():
            self.cfg[key] = config.getfloat(
                key, value, **({} if key in signed else {"minval": 0.0})
            )
        self.checkpoint_path = config.get(
            "checkpoint_path", "/home/sovol/sovol_plr_height"
        )
        self.last_plan = None
        self._progress = dict(
            phase="idle",
            active=False,
            bytes_processed=0,
            total_bytes=0,
            lines_processed=0,
            scan_percent=0.0,
        )
        self._progress_started = self._progress_finished = None
        self.gcode.register_command(
            "PLR_HOME",
            self.cmd_PLR_HOME,
            desc="Recovery home at a G-code-derived clear location; DRY_RUN=1 plans only",
        )

    def _update_progress(self, update):
        # The worker only replaces an immutable-by-convention snapshot. Console
        # writes and timers stay on the reactor thread, never the scan worker.
        current = dict(self._progress)
        current.update(update)
        total = current["total_bytes"]
        current["scan_percent"] = (
            min(100.0, 100.0 * current["bytes_processed"] / total) if total else 0.0
        )
        if current["phase"] in ("complete", "dry_run_complete", "failed"):
            current["active"] = False
            self._progress_finished = time.monotonic()
        self._progress = current

    def _report_progress(self, gcmd, eventtime):
        progress = self.get_status(eventtime)["progress"]
        gcmd.respond_info(
            "PLR_HOME: %s; scan %.1f%% (%s/%s bytes); %.0fs elapsed"
            % (
                progress["phase"].replace("_", " "),
                progress["scan_percent"],
                format(progress["bytes_processed"], ","),
                format(progress["total_bytes"], ","),
                progress["elapsed_seconds"],
            )
        )
        return eventtime + 5.0

    def _inputs(self, gcmd):
        sd = self.printer.lookup_object("virtual_sdcard")
        variables = self.printer.lookup_object("save_variables").allVariables
        status = sd.get_status(self.printer.get_reactor().monotonic())
        filename = (
            gcmd.get("FILE", None)
            or variables.get("filepath")
            or status.get("file_path")
        )
        if not filename or filename == "default":
            raise PlanError("No saved G-code file; provide FILE=...")
        root = os.path.realpath(sd.sdcard_dirname)
        path = os.path.realpath(os.path.join(root, filename))
        if os.path.commonpath([root, path]) != root or not os.path.isfile(path):
            raise PlanError(
                "FILE must be an existing G-code inside the virtual SD directory"
            )
        same_saved = path == os.path.realpath(str(variables.get("filepath", "")))
        z = gcmd.get_float("Z", None, minval=0.0)
        checkpoint = None
        if same_saved:
            try:
                with open(self.checkpoint_path) as stream:
                    saved = json.load(stream)
                checkpoint = saved.get("commandline")
                if z is None:
                    z = float(saved["Z"])
            except (OSError, ValueError, KeyError):
                pass
        if z is None and same_saved:
            z = float(variables.get("power_resume_z", -1))
        if z is None or not 0 <= z <= self.cfg["z_max"]:
            raise PlanError(
                "No usable saved height; provide Z=estimated_current_height"
            )
        byte_limit = gcmd.get_int("BYTE", None, minval=1)
        if (
            byte_limit is None
            and status.get("file_path") == path
            and status.get("file_position", 0) > 0
        ):
            byte_limit = status["file_position"]
        return path, z, byte_limit, checkpoint

    def _guard(self):
        if self.printer.is_shutdown():
            raise PlanError(
                "Printer is shut down; reset/activation is a separate operation"
            )
        eventtime = self.printer.get_reactor().monotonic()
        sd = self.printer.lookup_object("virtual_sdcard")
        if (
            sd.is_active()
            or self.printer.lookup_object("print_stats").get_status(eventtime)["state"]
            == "printing"
        ):
            raise PlanError("Cannot recovery-home during an active print")
        probe = self.printer.lookup_object("probe")
        if not probe.get_status(eventtime).get("is_calibrated", False):
            raise PlanError(
                "Existing Eddy calibration is required; this command does not recalibrate"
            )
        px, py = probe.get_offsets()[:2]
        if (
            abs(px - self.cfg["probe_x"]) > 0.001
            or abs(py - self.cfg["probe_y"]) > 0.001
        ):
            raise PlanError(
                "Configured recovery probe offsets differ from the installed probe"
            )

    def cmd_PLR_HOME(self, gcmd):
        timer = None
        self._progress_started, self._progress_finished = time.monotonic(), None
        self._progress = dict(
            phase="validating",
            active=True,
            bytes_processed=0,
            total_bytes=0,
            lines_processed=0,
            scan_percent=0.0,
        )
        try:
            dry_run = gcmd.get_int("DRY_RUN", 0, minval=0, maxval=1)
            self._guard()
            args = self._inputs(gcmd)
            reactor = self.printer.get_reactor()
            completion = reactor.completion()
            timer = reactor.register_timer(
                lambda eventtime: self._report_progress(gcmd, eventtime),
                reactor.monotonic() + 5.0,
            )

            # The 100+ MB scan runs off the Klipper reactor, never in G1 or a
            # motion timer. No fsync/checkpoint writes occur in this helper.
            def worker():
                try:
                    result = make_plan(
                        *args, settings=self.cfg, progress=self._update_progress
                    )
                except Exception as exc:
                    result = str(exc)
                reactor.async_complete(completion, result)

            gcmd.respond_info("PLR_HOME: analyzing saved G-code; no motion yet")
            threading.Thread(target=worker, daemon=True).start()
            plan = completion.wait()
            if isinstance(plan, str):
                raise PlanError(plan)
            self.last_plan = plan
            x, y = plan["nozzle_xy"]
            gcmd.respond_info(
                "PLR_HOME: %s; nozzle X%.2f Y%.2f; estimated Z%.3f -> %.3f; clearance %.1f mm; SHA256 %s"
                % (
                    os.path.basename(plan["file"]),
                    x,
                    y,
                    plan["estimated_start_z"],
                    plan["travel_z"],
                    plan["toolpath_clearance_mm"],
                    plan["sha256"],
                )
            )
            if dry_run:
                self._update_progress(dict(phase="dry_run_complete"))
                self._report_progress(gcmd, reactor.monotonic())
                gcmd.respond_info("PLR_HOME dry run complete; nothing moved")
                return
            self._guard()
            st = os.stat(plan["file"])
            if (st.st_size, st.st_mtime_ns) != (
                plan["size_bytes"],
                plan["file_mtime_ns"],
            ):
                raise PlanError("G-code changed after planning")

            def motion_progress(update):
                self._update_progress(update)
                self._report_progress(gcmd, reactor.monotonic())

            delta = execute_home(
                self.printer, self.gcode, plan, progress=motion_progress
            )
            self._update_progress(dict(phase="complete"))
            self._report_progress(gcmd, reactor.monotonic())
            gcmd.respond_info(
                "PLR_HOME complete: XYZ homed; parked above print at Z%.3f; mesh datum adjustment %.4f; print NOT resumed"
                % (plan["travel_z"], delta)
            )
        except Exception as exc:
            self._update_progress(dict(phase="failed", error=str(exc)))
            if isinstance(exc, (PlanError, OSError, ValueError)):
                raise gcmd.error("PLR_HOME: " + str(exc))
            raise
        finally:
            if timer is not None:
                reactor.unregister_timer(timer)

    def get_status(self, eventtime):
        progress = dict(self._progress)
        end = (
            self._progress_finished
            if self._progress_finished is not None
            else time.monotonic()
        )
        progress["elapsed_seconds"] = (
            round(max(0.0, end - self._progress_started), 1)
            if self._progress_started is not None
            else 0.0
        )
        return {"last_plan": self.last_plan, "progress": progress}


def load_config(config):
    raise config.error(
        "Replace legacy [plr_home] with [sv08_plr] and config/plr.cfg; do not register PLR_HOME twice"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file")
    parser.add_argument("--z", type=float, required=True)
    parser.add_argument("--byte", type=int)
    parser.add_argument("--checkpoint")
    parser.add_argument("--output")
    args = parser.parse_args()
    result = make_plan(args.file, args.z, args.byte, args.checkpoint)
    output = json.dumps(result, indent=2) + "\n"
    if args.output:
        with open(args.output, "w", encoding="utf-8") as stream:
            stream.write(output)
    print(output)


if __name__ == "__main__":
    main()
