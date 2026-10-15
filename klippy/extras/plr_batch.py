"""Bounded checkpoint batches tied to scheduled motion, not parser progress.

This uses Klipper's estimated MCU clocks, NOT a hardware completion interrupt or
an encoder acknowledgement. Normal checkpoint cadence is 100 ms plus storage
latency. A single indivisible G-code move can exceed that interval. No M400 or
per-move fsync is introduced. Full batch state is written to .batch.json. The
legacy recovery reader still receives its original XYZ-command/Z checkpoint;
it is not silently given an XY-only command it cannot recover from.
"""

from collections import deque
import logging
import os
import json
import tempfile

from .plr_checkpoint import CheckpointWriter


BATCH_SECONDS = 0.100
POLL_SECONDS = 0.010
MAX_PENDING_RECORDS = 16384


class AtomicBatchWriter(CheckpointWriter):
    def _write_record(self, content):
        self._atomic_record(content, self.path + ".batch.json")
        legacy = content.get("legacy_checkpoint")
        if legacy is not None:
            self._atomic_record(legacy, self.path)

    def _atomic_record(self, content, path):
        directory = os.path.dirname(path)
        fd, temporary = tempfile.mkstemp(prefix=".plr-batch-", dir=directory)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(content, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            temporary = None
            if hasattr(os, "O_DIRECTORY"):
                parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(parent)
                finally:
                    os.close(parent)
        finally:
            if temporary is not None:
                os.unlink(temporary)  # only our exact, uncommitted scratch file


def choose_batch(pending, elapsed_print_time, interval=BATCH_SECONDS):
    """Return an elapsed prefix, ending at 100 ms or an earlier travel boundary.

    Never select a future endpoint. A long indivisible command is committed at
    its endpoint; it is not represented as a fictitious partial completion.
    """
    if not pending or pending[0]["end"] is None or not pending[0].get("complete", True):
        return 0
    first = pending[0]
    if first["end"] > elapsed_print_time:
        return 0
    deadline = first["end"] + interval
    selected = 0
    for record in pending:
        end = record["end"]
        if end is None or end > elapsed_print_time or not record.get("complete", True):
            break
        if end > deadline:
            break
        selected += 1
        if record["boundary"]:
            return selected
    # A short tail is still written even if there are no more G-code commands.
    return selected if elapsed_print_time >= deadline else 0


class BatchedCheckpoint:
    def __init__(self, printer, path="/home/sovol/sovol_plr_height"):
        self.printer = printer
        self.reactor = printer.get_reactor()
        self.writer = AtomicBatchWriter(printer, path)
        self.pending = deque()
        self.toolhead = None
        self.timer = None
        self.job = None
        self.handle = None  # retain identity; Python id reuse cannot merge jobs
        self.generation = 0
        self.last_file_position = -1
        self.previous = None
        self.previous_extruding = False
        self.legacy = None
        self.closed = False
        self.failure = None
        self.committed_batches = 0
        self.committed_moves = 0
        self.last_commit = None
        printer.register_event_handler("klippy:ready", self.start)
        printer.register_event_handler("klippy:shutdown", self.stop)
        printer.register_event_handler("klippy:disconnect", self.stop)

    def start(self):
        self.toolhead = self.printer.lookup_object("toolhead")
        self.timer = self.reactor.register_timer(self._poll, self.reactor.NOW)

    def stop(self):
        self.closed = True
        if self.timer is not None:
            self.reactor.update_timer(self.timer, self.reactor.NEVER)

    def after_move(self, move, gcmd, before):
        sd = move.v_sd
        if sd is None or not sd.cmd_from_sd:
            return
        if self.closed or self.failure:
            raise RuntimeError("PLR batch writer is unavailable")
        if tuple(move.last_position) == before:
            return
        handle = sd.current_file
        if handle is None:
            raise RuntimeError("SD move without an open source file")
        filename = os.path.abspath(handle.name)
        cursor = sd.next_file_position
        same_command = bool(
            self.pending
            and self.handle is handle
            and self.pending[-1]["payload"]["file_position_before"] == sd.file_position
            and self.pending[-1]["payload"]["file_position_after"] == cursor
        )
        # This bound is host memory protection, NOT the MCU's queue accounting.
        # Normal G1 handling only captures state; backpressure occurs on overload.
        while not same_command and len(self.pending) >= MAX_PENDING_RECORDS:
            if self.closed or self.failure or self.printer.is_shutdown():
                raise RuntimeError("PLR batching stopped while applying backpressure")
            self.reactor.pause(self.reactor.monotonic() + POLL_SECONDS)
        job = (filename, id(handle))
        if self.job != job or cursor < self.last_file_position:
            self.pending.clear()
            self.previous = None
            self.previous_extruding = False
            self.legacy = None
            self.job = job
            self.handle = handle
            self.generation += 1
        self.last_file_position = cursor
        position = move._get_gcode_position()
        extruding = move.last_position[3] > before[3]
        xyz_changed = any(move.last_position[i] != before[i] for i in range(3))
        travel = xyz_changed and not extruding
        if travel and self.previous_extruding and self.previous is not None:
            self.previous["boundary"] = True
        parameters = gcmd.get_command_parameters()
        if all(axis in parameters for axis in ("G", "X", "Y", "Z")):
            # Preserve exact original text and Z spelling for the legacy sed
            # recovery reader. That reader requires XYZ in this one command.
            self.legacy = dict(commandline=gcmd.get_commandline(), Z=parameters["Z"])
        payload = dict(
            commandline=gcmd.get_commandline(),
            Z=str(position[2]),
            legacy_checkpoint=self.legacy,
            checkpoint_schema="sv08max.motion-time-batch.v2",
            source_file=filename,
            file_position_before=sd.file_position,
            file_position_after=cursor,
            gcode_state=dict(
                absolute_coordinates=move.absolute_coord,
                absolute_extrude=move.absolute_extrude,
                base_position=list(move.base_position),
                last_position=list(move.last_position),
                homing_position=list(move.homing_position),
                speed=move.speed,
                speed_factor=move.speed_factor,
                extrude_factor=move.extrude_factor,
            ),
        )
        record = dict(
            payload=payload,
            end=None,
            boundary=False,
            sd=sd,
            complete=False,
            generation=self.generation,
        )
        if same_command and self.pending:
            # Arcs and motion macros may emit many internal G1s for ONE source
            # line. Never associate its after-byte with a partial internal move.
            self.pending.pop()
        self.pending.append(record)
        self.previous = record
        self.previous_extruding = extruding

        def planned(end_time):
            record["end"] = end_time

        self.toolhead.register_lookahead_callback(planned)

    def _poll(self, eventtime):
        if self.closed or self.printer.is_shutdown():
            return self.reactor.NEVER
        try:
            # All MCUs share the print-time coordinate system. Use the slowest
            # estimate, not toolhead.print_time (which is seconds in the future).
            elapsed = min(
                mcu.estimated_print_time(eventtime) for mcu in self.toolhead.all_mcus
            )
            for record in self.pending:
                sd = record["sd"]
                # virtual_sdcard advances file_position only AFTER the entire
                # source command returned successfully. A yielded macro/arc is
                # not complete even when an early internal endpoint elapsed.
                record["complete"] = sd.file_position >= record["payload"][
                    "file_position_after"
                ] and (sd.current_file is self.handle or sd.current_file is None)
            count = choose_batch(self.pending, elapsed)
            if not count:
                return eventtime + POLL_SECONDS
            first, last = self.pending[0], self.pending[count - 1]
            generation = self.generation
            payload = dict(last["payload"])
            payload["motion_end_print_time"] = last["end"]
            payload["completion_basis"] = (
                "scheduled endpoint elapsed on all estimated MCU clocks; no hardware completion acknowledgement"
            )
            payload["batch"] = dict(
                moves=count,
                first_end_print_time=first["end"],
                last_end_print_time=last["end"],
                endpoint_span_s=last["end"] - first["end"],
                target_interval_s=BATCH_SECONDS,
                ended_at_travel_boundary=last["boundary"],
            )
            # Exactly one durable commit per selected batch. This yields the
            # reactor; G1 and already-buffered motion continue during disk I/O.
            self.writer.commit(payload)
            if generation == self.generation:
                for _ in range(count):
                    self.pending.popleft()
            self.committed_batches += 1
            self.committed_moves += count
            self.last_commit = payload
            return self.reactor.monotonic() + POLL_SECONDS
        except Exception as exc:
            if not self.closed and not self.printer.is_shutdown():
                self.failure = "%s: %s" % (type(exc).__name__, exc)
                logging.exception("PLR batch checkpoint failed")
                self.printer.invoke_shutdown(
                    "PLR checkpoint persistence failed: " + str(exc)
                )
            return self.reactor.NEVER
