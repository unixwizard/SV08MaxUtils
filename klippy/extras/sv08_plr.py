"""SV08 MAX assisted PLR commands. No actions during configuration/startup.

Public macros in config/plr.cfg call these private handlers. Every recovery is
bound to a reviewed bundle, original file hash, exact byte and one-shot ledger.
"""

import hashlib
import json
import math
import os
import threading
import time

from . import plr_home
from .plr_recovery import (
    validate_bundle,
    verify_source,
    setup_commands,
    final_modal_commands,
    claim_attempt,
)


class SV08PLR:
    def __init__(self, config):
        from . import bed_mesh

        self.mesh_class = bed_mesh.ZMesh
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object("gcode")
        self.bundle_path = os.path.expanduser(config.get("bundle_path"))
        self.expected_sha = config.get("bundle_sha256", "")
        self.geometry_confirmed = config.getboolean("stock_geometry_confirmed", False)
        self.cfg = dict(plr_home.DEFAULTS)
        self.cfg["front_fallback"] = int(config.getboolean("front_fallback", True))
        self.phase = "idle"
        self.home_token = None
        self.last_plan = None
        self.progress = {}
        self.busy = False
        self.consumed = False
        self.gcode.register_command("_SV08_PLR_HOME", self.cmd_home)
        self.gcode.register_command("_SV08_PLR_RESUME", self.cmd_resume)

    def status(self, name):
        return self.printer.lookup_object(name).get_status(
            self.printer.get_reactor().monotonic()
        )

    def guard_idle(self):
        if self.printer.is_shutdown():
            raise ValueError(
                "Controller shut down; reset/activation is a separate operation"
            )
        sd = self.printer.lookup_object("virtual_sdcard")
        if (
            sd.is_active()
            or sd.cmd_from_sd
            or self.status("print_stats")["state"] == "printing"
        ):
            raise ValueError(
                "Recovery forbidden during an active print or from SD G-code"
            )
        pause = self.printer.lookup_object("pause_resume", None)
        if (
            pause
            and pause.get_status(self.printer.get_reactor().monotonic())["is_paused"]
        ):
            raise ValueError("Ordinary PAUSE/RESUME is not power-loss recovery")
        excluded = self.printer.lookup_object("exclude_object", None)
        if (
            excluded
            and excluded.get_status(self.printer.get_reactor().monotonic())[
                "excluded_objects"
            ]
        ):
            raise ValueError("Live object exclusions require reconstruction")

    def load_bundle(self):
        with open(self.bundle_path, "rb") as stream:
            raw = stream.read(4 * 1024 * 1024 + 1)
        if (
            len(raw) > 4 * 1024 * 1024
            or hashlib.sha256(raw).hexdigest() != self.expected_sha
        ):
            raise ValueError(
                "Recovery bundle size/SHA256 mismatch; configure its reviewed hash"
            )
        return validate_bundle(json.loads(raw))

    def update_progress(self, update):
        self.progress = dict(self.progress, **update)

    def report(self, gcmd, now):
        total = self.progress.get("total_bytes", 0)
        done = self.progress.get("bytes_processed", 0)
        pct = 100.0 * done / total if total else 0.0
        gcmd.respond_info(
            "PLR: %s; %.1f%% (%s/%s bytes); %.0fs elapsed"
            % (
                self.progress.get("phase", self.phase),
                pct,
                format(done, ","),
                format(total, ","),
                time.monotonic() - self.started,
            )
        )
        return now + 5.0

    def mesh(self, params, points, name):
        result = self.mesh_class(params, name)
        result.build_mesh(points)
        return result

    def calibration_matches(self, data):
        probe = self.printer.lookup_object("probe_eddy_current eddy")
        current = list(zip(probe.calibration.cal_freqs, probe.calibration.cal_zpos))
        return current == sorted((freq, z) for z, freq in data["calibration"])

    def validate_machine(self, data):
        probe = self.printer.lookup_object("probe")
        px, py = probe.get_offsets()[:2]
        if (
            abs(px - self.cfg["probe_x"]) > 0.001
            or abs(py - self.cfg["probe_y"]) > 0.001
        ):
            raise ValueError("Probe offsets differ from the supported stock geometry")
        settings = self.status("configfile")["settings"]
        native_commands = (
            "g0",
            "g1",
            "g90",
            "g92",
            "m82",
            "m83",
            "m220",
            "m221",
            "set_gcode_offset",
            "set_pressure_advance",
            "set_velocity_limit",
            "set_fan_speed",
            "set_print_stats_info",
            "exclude_object_define",
            "exclude_object_start",
        )
        sections = {key.lower() for key in settings}
        if any("gcode_macro " + name in sections for name in native_commands):
            raise ValueError(
                "A restoration/motion command is overridden by a macro; review it first"
            )
        handlers = self.printer.event_handlers.get("klippy:start_print", [])
        idle = self.printer.lookup_object("idle_timeout")
        if any(
            getattr(fn, "__self__", None) is not idle
            or getattr(fn, "__name__", "") != "handle_start_print"
            for fn in handlers
        ):
            raise ValueError(
                "Unaudited klippy:start_print handler; refusing hidden startup actions"
            )
        if (
            settings["probe_eddy_current eddy"]["reg_drive_current"]
            != data["reg_drive_current"]
        ):
            raise ValueError("Eddy drive current differs from preserved print")
        for axis, maximum in (("x", 500), ("y", 500), ("z", 505)):
            conf = settings["stepper_" + axis]
            if conf["position_max"] < maximum or conf.get("position_min", 0) > 0:
                raise ValueError("Unsupported axis travel " + axis)
        if not self.geometry_confirmed:
            raise ValueError(
                "Confirm stock geometry, cable/tube routing and plate datum in configuration first"
            )
        # Build both meshes before ANY motion; invalid calibration/meshes must
        # not first become apparent after homing the occupied plate.
        home = data["home_reference"]
        home_mesh = self.mesh(home["mesh_params"], home["points"], "plr-home-reference")
        saved = data["mesh"]
        print_mesh = self.mesh(saved["params"], saved["points"], "plr-restored-print")
        matrix = print_mesh.get_mesh_matrix()
        if len(matrix) != len(saved["matrix"]) or any(
            len(a) != len(b) or any(abs(x - y) > 1.0e-9 for x, y in zip(a, b))
            for a, b in zip(matrix, saved["matrix"])
        ):
            raise ValueError("Saved print mesh cannot be reconstructed exactly")
        for fan in data["modal"]["fans"]:
            self.printer.lookup_object("fan_generic fan" + fan)
        return home_mesh, print_mesh

    def thermal_guard(self, data):
        for name, target, tolerance in (
            ("heater_bed", data["modal"]["bed"], 2.0),
            ("extruder", data["modal"]["nozzle"], 3.0),
        ):
            status = self.status(name)
            if (
                abs(status["target"] - target) > 0.01
                or abs(status["temperature"] - target) > tolerance
            ):
                raise ValueError(
                    "%s must already be stable at saved %.1f C; no automatic heating or soak reset"
                    % (name, target)
                )
        if not self.status("extruder").get("can_extrude", False):
            raise ValueError("Extruder is not ready")

    def plan(self, gcmd, data):
        reactor = self.printer.get_reactor()
        completion = reactor.completion()
        root = self.printer.lookup_object("virtual_sdcard").sdcard_dirname
        cfg = dict(self.cfg)
        cfg["height_allowance"] = max(
            abs(v) for row in data["mesh"]["matrix"] for v in row
        ) + abs(data["runtime_z_offset"])
        hp = data["home_reference"]["mesh_params"]
        cfg["probe_bounds"] = [hp["min_x"], hp["min_y"], hp["max_x"], hp["max_y"]]
        start_z = data["estimated_physical_z"]
        start_basis = "reviewed estimate after loss of homing"
        if not self.already_homed(data):
            head = self.status("toolhead")
            if "z" in head["homed_axes"]:
                # A known current Z is better than an old interruption estimate.
                # Preserve a matching completed home plan to avoid lifting and
                # homing again when PLR_RESUME follows PLR_HOME normally.
                start_z = head["position"][2]
                start_basis = "currently homed toolhead Z"

        def worker():
            try:
                path, identity = verify_source(root, data, self.update_progress)
                result = plr_home.make_plan(
                    path,
                    start_z,
                    data["resume"]["byte"],
                    settings=cfg,
                    progress=self.update_progress,
                )
                if result["sha256"] != data["source_sha256"]:
                    raise ValueError("Source changed between verification and planning")
                result["identity"] = identity
                result["start_z_basis"] = start_basis
                allowance = cfg["height_allowance"]
                if (
                    max(result["travel_z"] + allowance, data["modal"]["xyz"][2] + 10)
                    + allowance
                    > cfg["z_max"] - cfg["top_margin"]
                ):
                    raise ValueError("Insufficient Z headroom for restored mesh/offset")
            except Exception as exc:
                result = exc
            reactor.async_complete(completion, result)

        threading.Thread(target=worker, name="plr-planner", daemon=True).start()
        result = completion.wait()
        if isinstance(result, Exception):
            raise result
        self.last_plan = result
        gcmd.respond_info(
            "PLR: %s home X%.2f Y%.2f; Z estimate %.3f -> clearance %.3f; source SHA256 %s"
            % (
                result["homing_zone"],
                *result["nozzle_xy"],
                result["estimated_start_z"],
                result["travel_z"],
                result["sha256"],
            )
        )
        return result

    def check_source_identity(self, plan):
        st = os.stat(plan["file"])
        if (st.st_size, st.st_mtime_ns) != tuple(plan["identity"]):
            raise ValueError("G-code changed after planning")

    def already_homed(self, data):
        if self.home_token != self.expected_sha or not self.last_plan:
            return False
        head = self.status("toolhead")
        target = self.last_plan["nozzle_xy"] + [self.last_plan["travel_z"]]
        return (
            head["homed_axes"] == "xyz"
            and math.dist(head["position"][:3], target) < 0.01
            and self.calibration_matches(data)
        )

    def home(self, gcmd, data, plan, home_mesh):
        self.guard_idle()
        self.check_source_identity(plan)
        if self.already_homed(data):
            gcmd.respond_info(
                "PLR_HOME: matching recovery home is still valid; no repeat homing"
            )
            return
        self.home_token = None
        bed = self.printer.lookup_object("bed_mesh")
        self.printer.lookup_object(
            "probe_eddy_current eddy"
        ).calibration.load_calibration(data["calibration"])
        bed.set_mesh(home_mesh)
        self.phase = "homing"

        def update(value):
            self.update_progress(value)
            self.report(gcmd, self.printer.get_reactor().monotonic())

        plr_home.execute_home(self.printer, self.gcode, plan, progress=update)
        self.home_token = self.expected_sha
        self.phase = "homed"
        gcmd.respond_info("PLR_HOME complete; parked above print. No print started.")

    def resume(self, gcmd, data, plan, meshes):
        self.thermal_guard(data)
        if self.consumed:
            raise ValueError("Recovery already attempted; inspect rather than retry")
        claim_attempt(
            self.bundle_path + "." + self.expected_sha[:16] + ".attempt.json",
            self.expected_sha,
        )
        self.consumed = True
        # Same routine as PLR_HOME, but only when its exact home/park is absent.
        self.home(gcmd, data, plan, meshes[0])
        self.guard_idle()
        self.thermal_guard(data)
        self.check_source_identity(plan)
        self.phase = "restoring_print_state"
        bed = self.printer.lookup_object("bed_mesh")
        saved = data["mesh"]
        bed.fade_start, bed.fade_end = saved["fade_start"], saved["fade_end"]
        bed.fade_dist = bed.fade_end - bed.fade_start
        bed.base_fade_target = None
        bed.tool_offset = 0.0
        bed.set_mesh(meshes[1])
        # Open/reset BEFORE restoring object metadata: reset_file clears it.
        # Direct native methods bypass START_PRINT and vendor RESUME macros.
        sd = self.printer.lookup_object("virtual_sdcard")
        sd._reset_file()
        sd._load_file(gcmd, data["source_file"], check_subdirs=True)
        if os.path.realpath(sd.current_file.name) != plan["file"]:
            raise ValueError("Native SD loader selected a different source")
        st = os.fstat(sd.current_file.fileno())
        if (st.st_size, st.st_mtime_ns) != tuple(plan["identity"]):
            raise ValueError("Loaded source changed")
        move = self.printer.lookup_object("gcode_move")
        # Remove any operator's intervening G92 XYZ rebase without moving.
        # SET_GCODE_OFFSET alone adjusts the homing offset, not a G92 rebase.
        move.base_position[:3] = move.homing_position[:3]
        move.reset_last_position()
        for command in setup_commands(data):
            self.gcode.run_script_from_command(command)
        head = self.printer.lookup_object("toolhead")
        mesh_allowance = max(abs(v) for row in saved["matrix"] for v in row) + abs(
            data["runtime_z_offset"]
        )
        safe_z = max(
            plan["travel_z"] + mesh_allowance,
            data["modal"]["xyz"][2] + 10,
            move._get_gcode_position()[2],
        )
        if safe_z + mesh_allowance > self.cfg["z_max"] - self.cfg["top_margin"]:
            raise ValueError("Insufficient Z headroom for restored mesh/offset")
        self.phase = "positioning"
        self.update_progress(dict(phase=self.phase))
        self.report(gcmd, self.printer.get_reactor().monotonic())
        x, y, z = data["modal"]["xyz"]
        # No E appears in these moves. Original next command owns unretraction.
        for command in (
            "G1 Z%.9f F300" % safe_z,
            "G1 X%.9f Y%.9f F3000" % (x, y),
            "G1 Z%.9f F300" % z,
        ):
            self.gcode.run_script_from_command(command)
        head.wait_moves()
        if math.dist(move._get_gcode_position()[:3], [x, y, z]) > 0.001:
            raise ValueError("Resume position readback mismatch")
        for command in final_modal_commands(data):
            self.gcode.run_script_from_command(command)
        self.guard_idle()
        self.thermal_guard(data)
        self.check_source_identity(plan)
        sd.file_position = sd.next_file_position = data["resume"]["byte"]
        self.phase = "start_attempted"
        # Supported Sovol start_print handler only updates idle-time bookkeeping.
        # Installer pins virtual_sdcard/idle_timeout; added plugins need review.
        self.printer.send_event("klippy:start_print")
        sd.do_resume()  # one dispatch, no retry; work_handler seeks exact byte
        self.phase = "started"
        gcmd.respond_info(
            "PLR_RESUME: started once at original byte %d; no purge or extra extrusion"
            % sd.file_position
        )

    def run(self, gcmd, resume=False):
        if self.busy:
            raise gcmd.error("PLR operation already in progress")
        self.busy = True
        timer = None
        self.started = time.monotonic()
        self.progress = dict(phase="validating", bytes_processed=0, total_bytes=0)
        try:
            self.guard_idle()
            dry = gcmd.get_int("DRY_RUN", 0, minval=0, maxval=1)
            if not dry and gcmd.get_int("CONFIRM", 0, minval=0, maxval=1) != 1:
                raise ValueError(
                    "Use DRY_RUN=1 first; CONFIRM=1 acknowledges the documented physical assumptions"
                )
            data = self.load_bundle()
            meshes = self.validate_machine(data)
            reactor = self.printer.get_reactor()
            timer = reactor.register_timer(
                lambda t: self.report(gcmd, t), reactor.monotonic() + 5.0
            )
            gcmd.respond_info(
                "PLR: checking bundle/source and scanning occupied geometry; no motion yet"
            )
            # Preserve the completed home plan for a no-repeat resume. The file
            # is still verified in full; the deterministic regenerated plan is
            # required to have the same park/identity.
            plan = self.plan(gcmd, data)
            self.guard_idle()
            if dry:
                self.phase = "dry_run_complete"
                gcmd.respond_info(
                    "PLR dry run complete. No heat, state restoration, homing, motion or start."
                )
            elif resume:
                self.resume(gcmd, data, plan, meshes)
            else:
                self.thermal_guard(data)
                self.home(gcmd, data, plan, meshes[0])
        except Exception as exc:
            self.phase = "failed"
            raise gcmd.error("PLR: " + str(exc))
        finally:
            if timer is not None:
                self.printer.get_reactor().unregister_timer(timer)
            self.busy = False

    def cmd_home(self, gcmd):
        self.run(gcmd)

    def cmd_resume(self, gcmd):
        self.run(gcmd, resume=True)

    def get_status(self, eventtime):
        return dict(
            phase=self.phase,
            progress=dict(self.progress),
            busy=self.busy,
            consumed=self.consumed,
            last_plan=self.last_plan,
        )


def load_config(config):
    return SV08PLR(config)
