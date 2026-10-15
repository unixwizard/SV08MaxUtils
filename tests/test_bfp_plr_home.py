import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

MODULE = Path(__file__).resolve().parents[1] / "klippy/extras/plr_home.py"
spec = importlib.util.spec_from_file_location("bfp_plr_home", MODULE)
home = importlib.util.module_from_spec(spec)
spec.loader.exec_module(home)


def make_file(tmp_path, body):
    path = tmp_path / "job.gcode"
    path.write_bytes(body.encode("ascii"))
    return str(path)


def test_distance_transform_matches_brute_force():
    values = [1.0e12, 0, 1.0e12, 1.0e12, 1.0e12, 0, 1.0e12]
    assert home._distance_transform(values) == [
        min((i - j) ** 2 + v for j, v in enumerate(values)) for i in range(len(values))
    ]


def test_real_footprint_and_probe_clearance(tmp_path):
    path = make_file(
        tmp_path,
        "G90\nM83\nG1 X150 Y150 Z.2\nG1 X350 E1\nG1 Y350 E1\nG1 X150 E1\nG1 Y150 E1\n",
    )
    plan = home.make_plan(path, 10.0)
    assert plan["travel_z"] == 20.0
    assert plan["toolpath_clearance_mm"] >= 70
    assert plan["probe_xy"][0] == pytest.approx(plan["nozzle_xy"][0] - 19.8)
    assert plan["probe_xy"][1] == pytest.approx(plan["nozzle_xy"][1] - 0.75)
    assert len(plan["sha256"]) == 64


def test_relative_xyz_absolute_and_relative_e_and_g92(tmp_path):
    path = make_file(
        tmp_path,
        "G90\nM82\nG1 X100 Y100 Z1\nG1 X110 E5\nG1 X120 E5\nG92 E0\n"
        "G1 X130 E1\nG91\nG1 Y10 E1\nG90\nM83\nG1 Y120 E.5\nG1 Y130 E-.5\n",
    )
    fp, data = home.scan_file(path, home.DEFAULTS)
    assert data["extrusion_segments"] == 4
    assert fp.heights[55 * fp.nx + 65] >= 0
    assert fp.heights[65 * fp.nx + 65] == -1


@pytest.mark.parametrize(
    "cmd",
    [
        "G2 X120 Y100 I10 J0 E1",
        "G3 X120 Y100 I10 J0 E1",
        "G2 X120 Y100 R10 E1",
        "G3 X120 Y100 R-15 E1",
        "G2 X100 Y100 I10 J0 E1",
        "G3 X100 Y100 I10 J0 E1",
    ],
)
def test_arcs_mark_curved_footprint(tmp_path, cmd):
    path = make_file(tmp_path, "G90\nM83\nG1 X100 Y100 Z1\n" + cmd + "\n")
    fp, data = home.scan_file(path, home.DEFAULTS)
    assert data["extrusion_segments"] > 20
    assert sum(z >= 0 for z in fp.heights) > 10


def test_positive_support_and_brim_are_not_excluded(tmp_path):
    path = make_file(
        tmp_path,
        "G90\nM83\nG1 X80 Y80 Z.2\n;TYPE:Support\nG1 X120 E1\n"
        ";TYPE:Skirt\nG1 Y120 E1\n;WIDTH:1.2\nG1 X80 E1\n",
    )
    fp, data = home.scan_file(path, home.DEFAULTS)
    assert data["extrusion_segments"] == 3
    assert fp.max_width == 1.2


def test_prefix_max_handles_sequential_parts_and_checkpoint(tmp_path):
    prefix = "G90\nM83\nG1 X200 Y200 Z50\nG1 X250 E1\n"
    saved = "G1 X270 Y220 Z5 E1"
    path = make_file(tmp_path, prefix + "G1 Z5\n" + saved + "\nG1 Z100\nG1 X200 E1\n")
    _, data = home.scan_file(path, home.DEFAULTS, byte_limit=len(prefix))
    assert data["max_printed_z"] == 50
    _, data = home.scan_file(path, home.DEFAULTS, checkpoint=saved)
    assert data["max_printed_z"] == 50
    assert data["max_file_z"] == 100
    assert data["checkpoint_matches"] == 1


def test_missing_checkpoint_uses_whole_file_height(tmp_path):
    path = make_file(tmp_path, "G90\nM83\nG1 X200 Y200 Z50\nG1 X250 E1\n")
    plan = home.make_plan(path, 5, checkpoint="not present")
    assert plan["travel_z"] == 55
    assert plan["height_source"].startswith("whole-file")


def test_compact_gcode_is_parsed(tmp_path):
    path = make_file(tmp_path, "M83\nG1X200Y200Z1\nG1X250E1\n")
    _, data = home.scan_file(path, home.DEFAULTS)
    assert data["extrusion_segments"] == 1


def test_midprint_homing_is_rejected(tmp_path):
    path = make_file(tmp_path, "M83\nG1 X200 Y200 Z1\nG1 X250 E1\nG28\n")
    with pytest.raises(home.PlanError, match="Mid-print"):
        home.make_plan(path, 1)


@pytest.mark.parametrize("z", [float("nan"), float("inf"), -1, 499])
def test_bad_z_or_headroom_rejected(tmp_path, z):
    path = make_file(tmp_path, "M83\nG1 X200 Y200 Z1\nG1 X250 E1\n")
    with pytest.raises(home.PlanError):
        home.make_plan(path, z)


@pytest.mark.parametrize(
    "cmd", ["G20", "G18", "G19", "G90.1", "G53", "G92 X0", "SET_GCODE_OFFSET Y=2"]
)
def test_unhandled_coordinates_rejected(tmp_path, cmd):
    path = make_file(tmp_path, cmd + "\nM83\nG1 X200 Y200 Z1\nG1 X250 E1\n")
    with pytest.raises(home.PlanError):
        home.make_plan(path, 1)


def test_no_clear_location_rejected():
    fp = home.Footprint(home.DEFAULTS)
    fp.heights = [1.0] * len(fp.heights)
    with pytest.raises(home.PlanError, match="No clear"):
        home.choose_location(fp, home.DEFAULTS)


def test_rear_edge_replaces_full_width_gantry_corridor():
    fp = home.Footprint(home.DEFAULTS)
    fp.add_line([0, 350, 100], [500, 350, 100])
    place = home.choose_location(fp, home.DEFAULTS)
    assert place["nozzle_xy"][1] == 490.0
    assert place["probe_xy"][1] == 489.25
    assert place["homing_zone"] == "rear"
    assert place["rear_edge_inset_mm"] == 10.0
    assert place["nozzle_xy"][1] + place["head_radius_mm"] > 500


def test_low_print_below_gantry_can_home_but_tall_crossing_is_rejected():
    fp = home.Footprint(home.DEFAULTS)
    fp.add_line([250, 0, 100], [250, 500, 100])
    place = home.choose_location(fp, home.DEFAULTS, printed_z=9)
    with pytest.raises(home.PlanError, match="flexible_tube"):
        home.choose_location(fp, home.DEFAULTS, printed_z=100)
    assert place["nozzle_xy"][1] == 490.0


def test_blocked_rear_falls_back_to_clear_front():
    fp = home.Footprint(home.DEFAULTS)
    fp.add_line([0, 495, 1], [500, 495, 1])
    place = home.choose_location(fp, home.DEFAULTS, printed_z=1)
    assert place["homing_zone"] == "front_fallback"
    assert place["nozzle_xy"][1] == 15
    with pytest.raises(home.PlanError, match="No clear"):
        home.choose_location(fp, dict(home.DEFAULTS, front_fallback=0), printed_z=1)


def test_rearmost_row_also_keeps_positive_probe_offset_on_bed():
    cfg = dict(home.DEFAULTS, probe_y=10.0)
    fp = home.Footprint(cfg)
    fp.add_line([250, 250, 1], [251, 250, 1])
    place = home.choose_location(fp, cfg)
    assert place["nozzle_xy"][1] == 485.0
    assert place["probe_xy"][1] == 495.0


class FakeToolhead:
    def __init__(self, log):
        self.log, self.pos = log, [350.0, 324.0, 9.0, 42.0]
        self.kin = SimpleNamespace(note_z_not_homed=lambda: log.append(("z_unhomed",)))

    def get_position(self):
        return list(self.pos)

    def set_position(self, pos, homing_axes=()):
        self.pos = list(pos)
        self.log.append(("set", list(pos), homing_axes))

    def manual_move(self, pos, speed):
        for i, v in enumerate(pos):
            if v is not None:
                self.pos[i] = v
        self.log.append(("move", list(pos), speed))

    def wait_moves(self):
        self.log.append(("wait",))

    def get_kinematics(self):
        return self.kin


def fake_printer(fail=False):
    log = []
    th = FakeToolhead(log)
    mesh = SimpleNamespace(
        calc_z=lambda x, y: x / 1000,
        get_mesh_params=lambda: dict(min_x=0.0, max_x=500.0, min_y=0.0, max_y=500.0),
    )
    bm = SimpleNamespace(z_mesh=mesh, set_mesh=lambda m: log.append(("mesh", m)))

    def raw_home(cmd):
        log.append(("home", cmd))
        if cmd == {"Z": "0"}:
            if fail:
                raise RuntimeError("probe error")
            th.pos[2] = 3.5

    obj = dict(
        toolhead=th,
        homing=SimpleNamespace(cmd_G28=raw_home),
        bed_mesh=bm,
        gcode_move=SimpleNamespace(reset_last_position=lambda: log.append(("reset",))),
    )
    p = SimpleNamespace(lookup_object=lambda name, default=None: obj.get(name, default))
    g = SimpleNamespace(create_gcode_command=lambda name, line, params: params)
    return p, g, log, mesh


def test_motion_order_native_home_only_and_mesh_restoration():
    p, g, log, mesh = fake_printer()
    plan = dict(
        settings=home.DEFAULTS,
        nozzle_xy=[400.0, 425.0],
        probe_xy=[380.2, 424.25],
        estimated_start_z=8.933,
        travel_z=18.933,
    )
    delta = home.execute_home(p, g, plan)
    assert delta == pytest.approx(0.129)
    homes = [(i, item[1]) for i, item in enumerate(log) if item[0] == "home"]
    assert [v for i, v in homes] == [{"X": "0"}, {"Y": "0"}, {"Z": "0"}]
    first_set = next(v for v in log if v[0] == "set")
    assert first_set[2] == (2,)
    first_move = next(v for v in log if v[0] == "move")
    assert first_move == ("move", [None, None, 18.933], 5.0)
    assert log.index(first_move) < homes[0][0]
    assert log[homes[2][0] - 2] == ("move", [400.0, 425.0, None], 50.0)
    assert log[-2:] == [("mesh", mesh), ("reset",)]
    assert p.lookup_object("toolhead").pos == [400.0, 425.0, 18.933, 42.0]


def test_probe_failure_does_not_leave_assumed_z_homed():
    p, g, log, mesh = fake_printer(fail=True)
    plan = dict(
        settings=home.DEFAULTS,
        nozzle_xy=[400.0, 425.0],
        probe_xy=[380.2, 424.25],
        estimated_start_z=8.933,
        travel_z=18.933,
    )
    with pytest.raises(RuntimeError, match="probe error"):
        home.execute_home(p, g, plan)
    assert log[-3:] == [("mesh", mesh), ("reset",), ("z_unhomed",)]


def test_reference_mesh_cannot_silently_clamp_rear_homing_datum():
    p, g, log, mesh = fake_printer()
    mesh.get_mesh_params = lambda: dict(
        min_x=13.0, max_x=475.56, min_y=15.0, max_y=489.95
    )
    plan = dict(
        settings=home.DEFAULTS,
        nozzle_xy=[315.0, 495.0],
        probe_xy=[295.2, 494.25],
        estimated_start_z=8.933,
        travel_z=18.933,
    )
    with pytest.raises(home.PlanError, match="outside the loaded reference mesh"):
        home.execute_home(p, g, plan)
    assert log == []


def test_dry_run_does_not_call_motion(monkeypatch):
    class Completion:
        result = None

        def wait(self):
            return self.result

    c = Completion()
    timers, removed = [], []

    def register_timer(callback, when):
        timers.append((callback, when))
        return timers[-1]

    reactor = SimpleNamespace(
        completion=lambda: c,
        async_complete=lambda _, result: setattr(c, "result", result),
        monotonic=lambda: 10.0,
        register_timer=register_timer,
        unregister_timer=removed.append,
    )
    monkeypatch.setattr(
        home.threading, "Thread", lambda target, daemon: SimpleNamespace(start=target)
    )
    plan = dict(
        file="job.gcode",
        nozzle_xy=[400.0, 425.0],
        estimated_start_z=9.0,
        travel_z=19.0,
        toolpath_clearance_mm=90.0,
        sha256="abc",
    )
    monkeypatch.setattr(home, "make_plan", lambda *a, **k: plan)
    monkeypatch.setattr(
        home, "execute_home", lambda *a: pytest.fail("dry run called motion")
    )
    module = home.PLRHome.__new__(home.PLRHome)
    module.cfg = home.DEFAULTS
    module.printer = SimpleNamespace(get_reactor=lambda: reactor)
    module._guard = lambda: None
    module._inputs = lambda g: ("file", 9, None, None)
    gcmd = SimpleNamespace(respond_info=lambda s: None, get_int=lambda *a, **k: 1)
    module.cmd_PLR_HOME(gcmd)
    assert module.last_plan == plan
    assert len(timers) == 1 and timers[0][1] == 15.0
    assert removed == timers
    assert module.get_status(10.0)["progress"]["phase"] == "dry_run_complete"
    assert not module.get_status(10.0)["progress"]["active"]


def test_scan_progress_counts_every_byte_including_comments_and_blank_lines(tmp_path):
    body = "; metadata\n\n" * 30000 + "M83\nG1 X200 Y200 Z1\nG1 X250 E1\n"
    path = make_file(tmp_path, body)
    updates = []
    fp, data = home.scan_file(path, home.DEFAULTS, progress=updates.append)
    assert len(updates) >= 3
    assert updates[0] == dict(
        phase="scanning", bytes_processed=0, total_bytes=len(body), lines_processed=0
    )
    assert updates[-1] == dict(
        phase="scanning",
        bytes_processed=len(body),
        total_bytes=len(body),
        lines_processed=body.count("\n"),
    )
    assert [v["bytes_processed"] for v in updates] == sorted(
        v["bytes_processed"] for v in updates
    )
    assert all(v["total_bytes"] == len(body) for v in updates)
    assert data["size_bytes"] == len(body) and fp.segments == 1


def test_reporting_does_not_change_the_selected_plan(tmp_path):
    path = make_file(tmp_path, "G90\nM83\nG1 X150 Y150 Z.2\nG1 X350 E1\nG1 Y350 E1\n")
    updates = []
    baseline = home.make_plan(path, 9.0)
    assert home.make_plan(path, 9.0, progress=updates.append) == baseline
    assert updates[-1] == {"phase": "selecting_location"}
    assert updates[-2]["bytes_processed"] == baseline["size_bytes"]


def test_motion_progress_is_in_exact_existing_order():
    p, g, log, mesh = fake_printer()
    plan = dict(
        settings=home.DEFAULTS,
        nozzle_xy=[400.0, 425.0],
        probe_xy=[380.2, 424.25],
        estimated_start_z=8.933,
        travel_z=18.933,
    )
    updates = []
    home.execute_home(p, g, plan, progress=updates.append)
    assert [u["phase"] for u in updates] == [
        "clearance_lift",
        "homing_x",
        "homing_y",
        "moving_to_clear_location",
        "homing_z",
        "parking",
    ]
    assert [v[1] for v in log if v[0] == "home"] == [{"X": "0"}, {"Y": "0"}, {"Z": "0"}]
    assert p.lookup_object("toolhead").pos == [400.0, 425.0, 18.933, 42.0]
    assert log[-2:] == [("mesh", mesh), ("reset",)]


def test_progress_snapshot_percent_elapsed_and_reactor_console(monkeypatch):
    clock = [25.0]
    monkeypatch.setattr(home.time, "monotonic", lambda: clock[0])
    module = home.PLRHome.__new__(home.PLRHome)
    module.last_plan = None
    module._progress_started, module._progress_finished = 10.0, None
    module._progress = dict(
        phase="scanning",
        active=True,
        bytes_processed=0,
        total_bytes=0,
        lines_processed=0,
        scan_percent=0.0,
    )
    messages = []
    gcmd = SimpleNamespace(respond_info=messages.append)
    module._update_progress(
        dict(bytes_processed=500000, total_bytes=1000000, lines_processed=40000)
    )
    assert messages == []  # Worker snapshot updates never write the console.
    first = module.get_status(25.0)["progress"]
    assert first["scan_percent"] == 50.0 and first["elapsed_seconds"] == 15.0
    assert module._report_progress(gcmd, 25.0) == 30.0
    assert "50.0% (500,000/1,000,000 bytes); 15s elapsed" in messages[-1]
    module._update_progress(dict(phase="complete", bytes_processed=1000000))
    clock[0] = 80.0
    final = module.get_status(80.0)["progress"]
    assert final["scan_percent"] == 100.0 and final["elapsed_seconds"] == 15.0
    assert not final["active"]
    assert (
        first["bytes_processed"] == 500000
    )  # Previously returned snapshots do not mutate.


def test_scan_failure_cleans_up_progress_timer_without_motion(monkeypatch):
    class Completion:
        result = None

        def wait(self):
            return self.result

    c = Completion()
    removed = []
    reactor = SimpleNamespace(
        completion=lambda: c,
        async_complete=lambda _, result: setattr(c, "result", result),
        monotonic=lambda: 10.0,
        register_timer=lambda callback, when: "timer",
        unregister_timer=removed.append,
    )
    monkeypatch.setattr(
        home.threading, "Thread", lambda target, daemon: SimpleNamespace(start=target)
    )

    def fail_scan(*args, **kwargs):
        kwargs["progress"](dict(phase="scanning", bytes_processed=20, total_bytes=100))
        raise home.PlanError("Invalid scan")

    monkeypatch.setattr(home, "make_plan", fail_scan)
    monkeypatch.setattr(
        home, "execute_home", lambda *a, **k: pytest.fail("failed scan called motion")
    )
    module = home.PLRHome.__new__(home.PLRHome)
    module.last_plan, module.cfg = None, home.DEFAULTS
    module.printer = SimpleNamespace(get_reactor=lambda: reactor)
    module._guard = lambda: None
    module._inputs = lambda g: ("file", 9, None, None)
    gcmd = SimpleNamespace(
        respond_info=lambda s: None, get_int=lambda *a, **k: 0, error=RuntimeError
    )
    with pytest.raises(RuntimeError, match="PLR_HOME: Invalid scan"):
        module.cmd_PLR_HOME(gcmd)
    assert removed == ["timer"]
    progress = module.get_status(10.0)["progress"]
    assert progress["phase"] == "failed" and not progress["active"]
    assert progress["scan_percent"] == 20.0 and progress["error"] == "Invalid scan"


class InputCommand:
    def __init__(self, **values):
        self.values = values

    def get(self, name, default=None):
        return self.values.get(name, default)

    get_float = lambda self, name, default=None, **k: self.get(name, default)
    get_int = lambda self, name, default=None, **k: self.get(name, default)


def input_module(tmp_path, saved_path=None):
    path = make_file(tmp_path, "M83\nG1 X200 Y200 Z1\nG1 X250 E1\n")
    checkpoint = tmp_path / "height.json"
    checkpoint.write_text('{"Z":"8.933","commandline":"G1 X250 E1"}')
    status = dict(file_path=path, file_position=20)
    sd = SimpleNamespace(sdcard_dirname=str(tmp_path), get_status=lambda t: status)
    variables = dict(filepath=saved_path or path, power_resume_z=520.0)
    objects = dict(
        virtual_sdcard=sd, save_variables=SimpleNamespace(allVariables=variables)
    )
    module = home.PLRHome.__new__(home.PLRHome)
    module.cfg, module.checkpoint_path = home.DEFAULTS, str(checkpoint)
    module.printer = SimpleNamespace(
        lookup_object=lambda name: objects[name],
        get_reactor=lambda: SimpleNamespace(monotonic=lambda: 0),
    )
    return module, path, checkpoint


def test_inputs_prefer_checkpoint_over_stale_cached_520mm(tmp_path):
    module, path, checkpoint = input_module(tmp_path)
    filename, z, byte, command = module._inputs(InputCommand())
    assert (filename, z, byte, command) == (path, 8.933, 20, "G1 X250 E1")


def test_explicit_height_and_byte_override_saved_data(tmp_path):
    module, path, checkpoint = input_module(tmp_path)
    filename, z, byte, command = module._inputs(InputCommand(Z=9.1, BYTE=24))
    assert (z, byte) == (9.1, 24)


def test_unrelated_file_cannot_borrow_checkpoint_height(tmp_path):
    module, path, checkpoint = input_module(tmp_path)
    other = tmp_path / "other.gcode"
    other.write_text("G90\n")
    with pytest.raises(home.PlanError, match="No usable saved height"):
        module._inputs(InputCommand(FILE=str(other)))


def test_file_outside_sd_root_is_rejected(tmp_path):
    module, path, checkpoint = input_module(tmp_path)
    with pytest.raises(home.PlanError, match="inside the virtual SD"):
        module._inputs(InputCommand(FILE=str(MODULE), Z=9))


@pytest.mark.parametrize(
    "state,active,shutdown,calibrated,expected",
    [
        ("paused", False, True, True, "shut down"),
        ("printing", False, False, True, "active print"),
        ("paused", True, False, True, "active print"),
        ("paused", False, False, False, "calibration"),
        ("paused", False, False, True, None),
    ],
)
def test_runtime_guards(state, active, shutdown, calibrated, expected):
    objects = dict(
        virtual_sdcard=SimpleNamespace(is_active=lambda: active),
        print_stats=SimpleNamespace(get_status=lambda t: {"state": state}),
        probe=SimpleNamespace(
            get_status=lambda t: {"is_calibrated": calibrated},
            get_offsets=lambda: (-19.8, -0.75, 3.5),
        ),
    )
    module = home.PLRHome.__new__(home.PLRHome)
    module.cfg = home.DEFAULTS
    module.printer = SimpleNamespace(
        is_shutdown=lambda: shutdown,
        lookup_object=lambda name: objects[name],
        get_reactor=lambda: SimpleNamespace(monotonic=lambda: 0),
    )
    if expected:
        with pytest.raises(home.PlanError, match=expected):
            module._guard()
    else:
        module._guard()
