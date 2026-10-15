import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from klippy.extras import plr_home
from klippy.extras import plr_recovery as recovery
from klippy.extras.sv08_plr import SV08PLR


def test_real_mesh_and_bundle_validation(bundle):
    data, source, _, _ = bundle
    assert recovery.validate_bundle(data) is data
    assert recovery.verify_source(str(source.parent), data)[0] == str(source.resolve())


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.update(reviewed=False),
        lambda d: d.update(source_file="../escape.gcode"),
        lambda d: d["resume"].update(byte=0),
        lambda d: d["resume"].update(command="G1 X1\nSTART_PRINT"),
        lambda d: d["modal"].update(absolute=False),
        lambda d: d["modal"].update(feed=float("nan")),
        lambda d: d["modal"].update(objects=["EXCLUDE_OBJECT_DEFINE NAME=test\nG28"]),
        lambda d: d.update(excluded_objects=["missing"]),
        lambda d: d["calibration"].reverse(),
        lambda d: d["mesh"].update(tool_offset=1),
        lambda d: d["mesh"].update(matrix=[[0]]),
    ],
)
def test_bad_recovery_state_fails_closed(bundle, change):
    data = copy.deepcopy(bundle[0])
    change(data)
    with pytest.raises((ValueError, KeyError)):
        recovery.validate_bundle(data)


def test_cursor_source_and_exact_command_all_bound(bundle):
    data, source, _, _ = bundle
    changed = copy.deepcopy(data)
    changed["resume"]["byte"] += 1
    with pytest.raises(ValueError, match="line boundary"):
        recovery.verify_source(str(source.parent), changed)
    changed = copy.deepcopy(data)
    changed["resume"]["command"] = "G1 E.7 F2400"
    with pytest.raises(ValueError, match="command differs"):
        recovery.verify_source(str(source.parent), changed)
    source.write_bytes(source.read_bytes() + b"; changed\n")
    with pytest.raises(ValueError, match="SHA256"):
        recovery.verify_source(str(source.parent), data)


def test_restore_contains_no_motion_heat_extrusion_or_stock_resume(bundle):
    commands = recovery.setup_commands(bundle[0]) + recovery.final_modal_commands(
        bundle[0]
    )
    for command in commands:
        assert not command.startswith(
            (
                "G28",
                "M109",
                "M104",
                "M140",
                "M190",
                "START_PRINT",
                "RESUME",
                "G1 E",
                "G1 X",
                "G1 Y",
                "G1 Z",
            )
        )
    assert commands[-1].startswith("G1 F")


def test_exclusive_durable_attempt_ledger(tmp_path):
    path = tmp_path / "attempt.json"
    recovery.claim_attempt(str(path), "a" * 64)
    with pytest.raises(FileExistsError):
        recovery.claim_attempt(str(path), "a" * 64)
    assert json.loads(path.read_text())["status"] == "attempted_do_not_retry"


class Gcmd:
    def __init__(self, **values):
        self.values = values
        self.messages = []

    def get_int(self, key, default=0, **kwargs):
        return self.values.get(key, default)

    def respond_info(self, message):
        self.messages.append(message)

    respond_raw = respond_info
    error = ValueError


def controller(bundle, native, monkeypatch):
    from test_writer import Completion

    data, source, saved, digest = bundle
    log = []
    timers = []
    reactor = SimpleNamespace(
        monotonic=lambda: 1.0,
        NOW=0.0,
        NEVER=1.0e30,
        completion=Completion,
        async_complete=lambda c, r: c.complete(r),
        register_timer=lambda cb, when: timers.append((cb, when)) or len(timers),
        unregister_timer=lambda t: None,
        pause=lambda t: None,
    )
    head = SimpleNamespace(
        pos=[100.0, 100.0, 10.3, 0.0], homed="", wait_moves=lambda: log.append("wait")
    )
    head.get_position = lambda: list(head.pos)
    head.get_status = lambda t: dict(position=list(head.pos), homed_axes=head.homed)

    def set_position(pos, homing_axes=()):
        head.pos[:] = pos
        for i in homing_axes:
            if "xyz"[i] not in head.homed:
                head.homed += "xyz"[i]

    head.set_position = set_position

    def manual_move(pos, speed):
        log.append(("manual", list(pos)))
        for i, value in enumerate(pos):
            if value is not None:
                head.pos[i] = value

    head.manual_move = manual_move
    head.get_kinematics = lambda: SimpleNamespace(
        note_z_not_homed=lambda: setattr(head, "homed", head.homed.replace("z", ""))
    )

    def home(command):
        axis = next(iter(command))
        log.append(("home", axis))
        if axis.lower() not in head.homed:
            head.homed += axis.lower()
        head.homed = "".join(c for c in "xyz" if c in head.homed)
        if axis == "Z":
            head.pos[2] = 5.0

    cal = SimpleNamespace(cal_freqs=[], cal_zpos=[])

    def load_calibration(pairs):
        cal.cal_freqs, cal.cal_zpos = map(list, zip(*sorted((f, z) for z, f in pairs)))

    cal.load_calibration = load_calibration
    bed = SimpleNamespace(z_mesh=None)
    bed.set_mesh = lambda m: setattr(bed, "z_mesh", m)
    gm = SimpleNamespace(
        offset=0.0,
        reset_last_position=lambda: None,
        base_position=[0.0, 0.0, 0.0, 0.0],
        homing_position=[0.0, 0.0, 0.0, 0.0],
    )
    gm._get_gcode_position = lambda: (
        head.pos[:2] + [head.pos[2] - gm.offset, head.pos[3]]
    )
    gcode = SimpleNamespace(
        error=ValueError,
        respond_raw=lambda m: None,
        create_gcode_command=lambda cmd, text, params: params,
        get_mutex=lambda: SimpleNamespace(test=lambda: False),
    )

    def run(command):
        log.append(command)
        if command.startswith("SET_GCODE_OFFSET"):
            gm.offset = float(command.split(" Z=")[1].split()[0])
        if command.startswith("G1 "):
            for token in command.split()[1:]:
                if token[0] in "XYZ":
                    index = "XYZ".index(token[0])
                    head.pos[index] = float(token[1:]) + (
                        gm.offset if index == 2 else 0
                    )

    gcode.run_script_from_command = run
    gcode.run_script = run
    stats = SimpleNamespace(
        reset=lambda: None,
        set_current_file=lambda f: None,
        note_start=lambda: None,
        note_complete=lambda: None,
        note_error=Mock(),
        note_pause=lambda: None,
        get_status=lambda t: dict(state="standby"),
    )
    objects = dict(
        toolhead=head,
        gcode=gcode,
        gcode_move=gm,
        bed_mesh=bed,
        homing=SimpleNamespace(cmd_G28=home),
        print_stats=stats,
        probe=SimpleNamespace(get_offsets=lambda: (-19.8, -0.75, 3.5)),
        configfile=SimpleNamespace(
            get_status=lambda t: dict(
                settings={
                    "stepper_x": {"position_max": 500},
                    "stepper_y": {"position_max": 500},
                    "stepper_z": {"position_max": 505},
                    "probe_eddy_current eddy": {"reg_drive_current": 15},
                }
            )
        ),
        heater_bed=SimpleNamespace(
            get_status=lambda t: dict(target=50.0, temperature=50.0)
        ),
        extruder=SimpleNamespace(
            get_status=lambda t: dict(target=207.0, temperature=207.0, can_extrude=True)
        ),
    )
    objects["probe_eddy_current eddy"] = SimpleNamespace(calibration=cal)
    objects.update({"fan_generic fan0": object(), "fan_generic fan1": object()})
    objects["idle_timeout"] = object()

    def lookup(name, *default):
        return objects.get(name, default[0]) if default else objects[name]

    printer = SimpleNamespace(
        lookup_object=lookup,
        get_reactor=lambda: reactor,
        is_shutdown=lambda: False,
        event_handlers={},
        send_event=lambda name: log.append(("event", name)),
    )
    sd = native["virtual_sdcard"].VirtualSD.__new__(native["virtual_sdcard"].VirtualSD)
    sd.printer, sd.reactor, sd.gcode, sd.print_stats = printer, reactor, gcode, stats
    sd.sdcard_dirname = str(source.parent)
    sd.current_file = sd.work_timer = None
    sd.file_position = sd.next_file_position = sd.file_size = 0
    sd.cmd_from_sd = sd.must_pause_work = False
    sd.on_error_gcode = SimpleNamespace(render=lambda: "")
    objects["virtual_sdcard"] = sd
    obj = SV08PLR.__new__(SV08PLR)
    obj.printer, obj.gcode, obj.mesh_class = printer, gcode, native["bed_mesh"].ZMesh
    obj.bundle_path, obj.expected_sha, obj.geometry_confirmed = str(saved), digest, True
    obj.cfg = dict(plr_home.DEFAULTS)
    obj.phase, obj.home_token, obj.last_plan = "idle", None, None
    obj.progress, obj.busy, obj.consumed = {}, False, False
    return obj, log, sd, head, stats


def test_dry_run_has_no_state_mutation_motion_heat_or_start(
    bundle, native, monkeypatch
):
    obj, log, sd, head, _ = controller(bundle, native, monkeypatch)
    obj.cmd_resume(Gcmd(DRY_RUN=1))
    assert log == [] and head.homed == "" and sd.current_file is None
    assert obj.phase == "dry_run_complete" and not obj.consumed


def test_resume_calls_home_then_native_sd_at_exact_byte_once(
    bundle, native, monkeypatch
):
    obj, log, sd, head, stats = controller(bundle, native, monkeypatch)
    obj.cmd_resume(Gcmd(CONFIRM=1))
    assert [v for v in log if isinstance(v, tuple) and v[0] == "home"] == [
        ("home", "X"),
        ("home", "Y"),
        ("home", "Z"),
    ]
    assert sd.file_position == bundle[0]["resume"]["byte"] and sd.is_active()
    assert obj.phase == "started" and obj.consumed
    sd.current_file.seek(sd.file_position)
    assert sd.current_file.readline().strip() == "G1 E.8 F2400"
    assert not any(isinstance(v, str) and v.startswith("G1 E") for v in log)
    assert not any(
        isinstance(v, str) and v.startswith(("START_PRINT", "RESUME", "M109", "G28"))
        for v in log
    )
    before = len(log)
    with pytest.raises(ValueError, match="active print"):
        obj.cmd_resume(Gcmd(CONFIRM=1))
    assert len(log) == before
    # Native work handler seeks the exact byte, not a text-match or line number.
    sd.work_handler(1.0)
    executed = [v for v in log[before:] if isinstance(v, str)]
    assert executed == ["G1 E.8 F2400", "G1 X210 E1"]
    stats.note_error.assert_not_called()


def test_separate_home_is_reused_without_rehoming(bundle, native, monkeypatch):
    obj, log, sd, head, _ = controller(bundle, native, monkeypatch)
    obj.cmd_home(Gcmd(CONFIRM=1))
    homes = [v for v in log if isinstance(v, tuple) and v[0] == "home"]
    obj.cmd_resume(Gcmd(CONFIRM=1))
    assert homes == [v for v in log if isinstance(v, tuple) and v[0] == "home"]
    assert sd.is_active()
    sd.current_file.close()


def test_cold_resume_does_not_home_or_claim_attempt(bundle, native, monkeypatch):
    obj, log, sd, head, _ = controller(bundle, native, monkeypatch)
    obj.printer.lookup_object("heater_bed").get_status = lambda t: dict(
        target=0.0, temperature=20.0
    )
    with pytest.raises(ValueError, match="stable"):
        obj.cmd_resume(Gcmd(CONFIRM=1))
    assert log == [] and not obj.consumed
    assert not list(bundle[1].parent.glob("*.attempt.json"))


def test_reboot_cannot_repeat_a_consumed_recovery(bundle, native, monkeypatch):
    obj, log, sd, head, _ = controller(bundle, native, monkeypatch)
    ledger = obj.bundle_path + "." + obj.expected_sha[:16] + ".attempt.json"
    recovery.claim_attempt(ledger, obj.expected_sha)
    with pytest.raises(ValueError, match="[Ee]xists"):
        obj.cmd_resume(Gcmd(CONFIRM=1))
    assert log == [] and sd.current_file is None


def test_current_homed_z_overrides_stale_interruption_estimate(
    bundle, native, monkeypatch
):
    obj, log, sd, head, _ = controller(bundle, native, monkeypatch)
    head.homed = "xyz"
    head.pos[2] = 80.0
    obj.cmd_home(Gcmd(DRY_RUN=1))
    assert obj.last_plan["estimated_start_z"] == 80
    assert obj.last_plan["travel_z"] == 90
    assert log == []


def test_home_from_known_z_is_reused_even_when_old_interruption_z_differs(
    bundle, native, monkeypatch
):
    obj, log, sd, head, _ = controller(bundle, native, monkeypatch)
    head.homed = "xyz"
    head.pos[2] = 80.0
    obj.cmd_home(Gcmd(CONFIRM=1))
    assert obj.last_plan["estimated_start_z"] == 80.0
    homes = [v for v in log if isinstance(v, tuple) and v[0] == "home"]
    obj.cmd_resume(Gcmd(CONFIRM=1))
    assert obj.last_plan["estimated_start_z"] == 80.0
    assert homes == [v for v in log if isinstance(v, tuple) and v[0] == "home"]
    sd.current_file.close()


def test_resume_removes_intervening_g92_xyz_without_extrusion(
    bundle, native, monkeypatch
):
    obj, log, sd, head, _ = controller(bundle, native, monkeypatch)
    obj.cmd_home(Gcmd(CONFIRM=1))
    move = obj.printer.lookup_object("gcode_move")
    move.base_position[:3] = [100.0, 200.0, -50.0]
    obj.cmd_resume(Gcmd(CONFIRM=1))
    assert move.base_position[:3] == [0.0, 0.0, 0.0]
    assert not any(isinstance(v, str) and v.startswith("G1 E") for v in log)
    sd.current_file.close()


def test_unaudited_start_event_is_rejected_before_motion(bundle, native, monkeypatch):
    obj, log, sd, head, _ = controller(bundle, native, monkeypatch)
    obj.printer.event_handlers["klippy:start_print"] = [lambda: None]
    with pytest.raises(ValueError, match="Unaudited"):
        obj.cmd_resume(Gcmd(CONFIRM=1))
    assert log == []


def test_uppercase_macro_override_is_rejected(bundle, native, monkeypatch):
    obj, log, sd, head, _ = controller(bundle, native, monkeypatch)
    cfg = obj.printer.lookup_object("configfile")
    settings = cfg.get_status(0)["settings"]
    settings["gcode_macro G1"] = {"gcode": "G28"}
    cfg.get_status = lambda t: {"settings": settings}
    with pytest.raises(ValueError, match="overridden"):
        obj.cmd_resume(Gcmd(CONFIRM=1))
    assert log == []
