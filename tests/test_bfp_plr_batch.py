from collections import deque
import json
from pathlib import Path
import sys
import types
from unittest.mock import Mock

import pytest


ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "klippy/extras"
package = types.ModuleType("bfp_batch_test")
package.__path__ = [str(DIRECTORY)]
sys.modules[package.__name__] = package
batch = __import__("bfp_batch_test.plr_batch", fromlist=["*"])
sys.path.insert(0, str(ROOT / "tools"))
import patch_sources as prepare_batch

sys.path.pop(0)


def records(times, boundary=None):
    return deque(dict(end=t, boundary=(i == boundary)) for i, t in enumerate(times))


def test_future_parser_progress_is_not_checkpointed():
    assert batch.choose_batch(records([10.0, 10.001, 10.002]), 6.0) == 0


def test_batch_is_capped_by_motion_span_not_number_of_gcode_lines():
    pending = records([i * 0.0001 for i in range(3000)])
    count = batch.choose_batch(pending, 0.5)
    assert count > 512
    assert pending[count - 1]["end"] - pending[0]["end"] <= 0.100
    assert pending[count]["end"] - pending[0]["end"] > 0.100


def test_travel_transition_closes_earlier_batch():
    assert (
        batch.choose_batch(records([i * 0.001 for i in range(100)], boundary=20), 0.020)
        == 21
    )


def test_partial_tail_is_flushed_without_another_command():
    assert batch.choose_batch(records([0.010, 0.020, 0.030]), 0.109) == 0
    assert batch.choose_batch(records([0.010, 0.020, 0.030]), 0.111) == 3


def test_never_includes_unelapsed_endpoint():
    assert batch.choose_batch(records([0.010, 0.020, 0.5]), 0.111) == 2
    assert batch.choose_batch(records([None, 0.2]), 5.0) == 0


def test_long_indivisible_move_not_falsely_marked_complete():
    assert batch.choose_batch(records([2.0]), 0.2) == 0
    assert batch.choose_batch(records([2.0]), 2.101) == 1


class Printer:
    def __init__(self):
        self.events = {}
        self.clock = 0.0
        self.reactor = types.SimpleNamespace(
            register_timer=Mock(return_value=object()),
            update_timer=Mock(),
            monotonic=lambda: self.clock,
            NOW=0.0,
            NEVER=1e30,
        )
        self.toolhead = types.SimpleNamespace(
            callbacks=[],
            all_mcus=[types.SimpleNamespace(estimated_print_time=lambda event: event)],
        )
        self.toolhead.register_lookahead_callback = self.toolhead.callbacks.append
        self.shutdown = False
        self.invoke_shutdown = Mock(
            side_effect=lambda msg: setattr(self, "shutdown", True)
        )

    def get_reactor(self):
        return self.reactor

    def register_event_handler(self, name, callback):
        self.events.setdefault(name, []).append(callback)

    def lookup_object(self, name):
        assert name == "toolhead"
        return self.toolhead

    def is_shutdown(self):
        return self.shutdown


def make_batch(tmp_path):
    printer = Printer()
    instance = batch.BatchedCheckpoint(printer, str(tmp_path / "checkpoint.json"))
    instance.start()
    instance.writer.commit = Mock()
    return printer, instance


def move_for(tmp_path):
    move = types.SimpleNamespace(
        v_sd=types.SimpleNamespace(
            cmd_from_sd=True,
            current_file=types.SimpleNamespace(name=str(tmp_path / "job.gcode")),
            file_position=0,
            next_file_position=20,
        ),
        absolute_coord=True,
        absolute_extrude=False,
        base_position=[0.0, 0.0, 0.0, 0.0],
        last_position=[1.0, 2.0, 3.0, 0.5],
        homing_position=[0.0, 0.0, 0.0, 0.0],
        speed=150.0,
        speed_factor=1.0 / 60.0,
        extrude_factor=1.0,
    )
    move._get_gcode_position = lambda: list(move.last_position)
    return move


def command(text="G1 X1 Y2 Z3 E.5"):
    return types.SimpleNamespace(
        get_commandline=lambda: text,
        get_command_parameters=lambda: {v[0]: v[1:] for v in text.split()},
    )


def test_g1_capture_never_waits_for_a_disk_write(tmp_path):
    printer, instance = make_batch(tmp_path)
    move = move_for(tmp_path)
    instance.after_move(move, command(), (0.0, 0.0, 0.0, 0.0))
    instance.writer.commit.assert_not_called()
    assert len(instance.pending) == 1 and instance.pending[0]["end"] is None
    assert instance.pending[0]["payload"]["file_position_after"] == 20
    assert instance.pending[0]["payload"]["gcode_state"]["absolute_extrude"] is False
    printer.toolhead.callbacks.pop()(4.0)
    move.v_sd.file_position = 20
    instance._poll(3.999)
    instance.writer.commit.assert_not_called()
    instance._poll(4.101)
    instance.writer.commit.assert_called_once()
    assert instance.last_commit["motion_end_print_time"] == 4.0


def test_snapshot_is_not_mutated_by_subsequent_moves(tmp_path):
    printer, instance = make_batch(tmp_path)
    move = move_for(tmp_path)
    instance.after_move(move, command(), (0.0, 0.0, 0.0, 0.0))
    move.last_position[2] = 99.0
    assert instance.pending[0]["payload"]["gcode_state"]["last_position"][2] == 3.0


def test_use_slowest_mcu_not_parser_time(tmp_path):
    printer, instance = make_batch(tmp_path)
    move = move_for(tmp_path)
    instance.after_move(move, command(), (0.0, 0.0, 0.0, 0.0))
    printer.toolhead.callbacks.pop()(1.0)
    printer.toolhead.all_mcus.append(
        types.SimpleNamespace(estimated_print_time=lambda event: 0.9)
    )
    instance._poll(10.0)
    instance.writer.commit.assert_not_called()


def test_transition_from_extrusion_to_travel_is_boundary(tmp_path):
    printer, instance = make_batch(tmp_path)
    move = move_for(tmp_path)
    instance.after_move(move, command(), (0.0, 0.0, 0.0, 0.0))
    before = tuple(move.last_position)
    move.last_position[0] = 10.0
    move.v_sd.next_file_position = 40
    instance.after_move(move, command("G1 X10"), before)
    assert instance.pending[0]["boundary"]
    assert not instance.pending[1]["boundary"]


def test_old_job_and_rewound_cursor_do_not_mix(tmp_path):
    printer, instance = make_batch(tmp_path)
    move = move_for(tmp_path)
    instance.after_move(move, command(), (0.0, 0.0, 0.0, 0.0))
    previous_generation = instance.generation
    move.v_sd.next_file_position = 10
    instance.after_move(move, command(), (0.0, 0.0, 0.0, 0.0))
    assert len(instance.pending) == 1 and instance.generation == previous_generation + 1


def test_write_failure_shutdown_and_no_success_accounting(tmp_path):
    printer, instance = make_batch(tmp_path)
    move = move_for(tmp_path)
    instance.after_move(move, command(), (0.0, 0.0, 0.0, 0.0))
    printer.toolhead.callbacks.pop()(1.0)
    instance.writer.commit.side_effect = OSError("storage failed")
    move.v_sd.file_position = 20
    instance._poll(1.2)
    printer.invoke_shutdown.assert_called_once()
    assert instance.committed_batches == 0 and len(instance.pending) == 1


def test_atomic_writer_preserves_prior_record_on_failed_write(tmp_path, monkeypatch):
    path = tmp_path / "checkpoint.json"
    path.write_text('{"old": true}')
    writer = batch.AtomicBatchWriter(Printer(), str(path))
    monkeypatch.setattr(batch.os, "fsync", Mock(side_effect=OSError("failed sync")))
    with pytest.raises(OSError):
        writer._write_record(dict(new=True))
    assert json.loads(path.read_text()) == {"old": True}
    assert not list(tmp_path.glob(".plr-batch-*"))


def test_atomic_writer_new_record_readback(tmp_path):
    path = tmp_path / "checkpoint.json"
    writer = batch.AtomicBatchWriter(Printer(), str(path))
    writer._write_record(dict(commandline="G1 X1", Z="3", file_position_after=20))
    assert (
        json.loads(Path(str(path) + ".batch.json").read_text())["file_position_after"]
        == 20
    )


def test_legacy_xyz_record_remains_compatible_during_xy_only_moves(tmp_path):
    printer, instance = make_batch(tmp_path)
    move = move_for(tmp_path)
    instance.after_move(move, command("G1 X1 Y2 Z3.000 E.5"), (0.0, 0.0, 0.0, 0.0))
    before = tuple(move.last_position)
    move.last_position[0] = 10.0
    move.v_sd.next_file_position = 40
    instance.after_move(move, command("G1 X10"), before)
    payload = instance.pending[-1]["payload"]
    assert payload["commandline"] == "G1 X10" and payload["file_position_after"] == 40
    assert payload["legacy_checkpoint"] == dict(
        commandline="G1 X1 Y2 Z3.000 E.5", Z="3.000"
    )
    writer = batch.AtomicBatchWriter(printer, str(tmp_path / "legacy.json"))
    writer._write_record(payload)
    assert (
        json.loads((tmp_path / "legacy.json").read_text())
        == payload["legacy_checkpoint"]
    )
    assert (
        json.loads((tmp_path / "legacy.json.batch.json").read_text())[
            "file_position_after"
        ]
        == 40
    )


@pytest.mark.parametrize(
    "advertised,reserved,expected",
    [
        (1067, 12, 500),
        (4086, 12, 500),
        (1116, 0, 512),
        (800, 10, 390),
        (2, 0, 1),
    ],
)
def test_mcu_budget_from_exact_patched_source(advertised, reserved, expected):
    source = (ROOT / "tests/fixtures/mcu.py.txt").read_bytes()
    data, _ = prepare_batch.patch_mcu(source)
    text = data.decode().replace("\r\n", "\n")
    start = text.index("        # Owner: at most 512")
    end = text.index("        ffi_main, ffi_lib = chelper.get_ffi()", start)
    fragment = text[start:end]
    function = (
        "def budget(self, config_params):\n"
        + fragment
        + "        return move_count-self._reserved_move_slots\n"
    )
    scope = {"error": RuntimeError}
    exec(function, scope)
    assert (
        scope["budget"](
            types.SimpleNamespace(_reserved_move_slots=reserved, _name="test"),
            dict(move_count=advertised),
        )
        == expected
    )


def test_mcu_budget_fails_if_reservations_exhaust_cap():
    assert "if move_count <= self._reserved_move_slots:" in prepare_batch.MCU_NEW


def test_gcode_patch_has_no_per_move_wait_or_fsync():
    source = (ROOT / "tests/fixtures/gcode_move.py.txt").read_bytes()
    data, diff = prepare_batch.patch_gcode(source)
    text = data.decode()
    function = text[
        text.index("    def cmd_G1(") : text.index(
            "    # G-Code coordinate manipulation", text.index("    def cmd_G1(")
        )
    ]
    assert (
        "fsync" not in function
        and ".wait(" not in function
        and ".commit(" not in function
    )
    assert "after_move" in function
    with pytest.raises(ValueError):
        prepare_batch.patch_gcode(source + b"\n")


def test_partial_arc_or_macro_cannot_commit_entire_source_line(tmp_path):
    printer, instance = make_batch(tmp_path)
    move = move_for(tmp_path)
    instance.after_move(move, command(), (0.0, 0.0, 0.0, 0.0))
    printer.toolhead.callbacks.pop()(1.0)
    instance._poll(1.2)
    instance.writer.commit.assert_not_called()  # parent SD line has not returned
    before = tuple(move.last_position)
    move.last_position[0] += 1
    instance.after_move(move, command("G1 X2"), before)
    assert len(instance.pending) == 1  # same SD line, keep final internal endpoint
    printer.toolhead.callbacks.pop()(2.0)
    move.v_sd.file_position = 20  # native SD advances ONLY after successful line
    instance._poll(1.5)
    instance.writer.commit.assert_not_called()
    instance._poll(2.101)
    assert instance.last_commit["motion_end_print_time"] == 2
    assert instance.last_commit["gcode_state"]["last_position"][0] == 2
    assert instance.last_commit["file_position_after"] == 20


def test_persistence_can_yield_while_next_command_is_captured(tmp_path):
    printer, instance = make_batch(tmp_path)
    move = move_for(tmp_path)
    instance.after_move(move, command(), (0.0, 0.0, 0.0, 0.0))
    printer.toolhead.callbacks.pop()(1.0)
    move.v_sd.file_position = 20

    def io_in_flight(payload):
        old = tuple(move.last_position)
        move.last_position[0] = 5
        move.v_sd.next_file_position = 40
        instance.after_move(move, command("G1 X5"), old)
        printer.toolhead.callbacks.pop()(2.0)

    instance.writer.commit.side_effect = io_in_flight
    instance._poll(1.101)
    assert len(instance.pending) == 1
    assert instance.pending[0]["payload"]["file_position_after"] == 40
