# SV08MaxUtils — Power Loss Recovery Fix

Utilities for the **Sovol SV08 MAX**: durable motion-boundary checkpointing,
bounded MCU queue allocation, and controlled recovery homing/resume.

**v1.0 is an offline-validated, assisted-recovery baseline—not unattended,
guaranteed power-loss recovery.** The checkpoint fix operates during printing.
`PLR_HOME` and `PLR_RESUME` require a reviewed recovery bundle containing the
exact restart boundary and preserved machine state. They do not invent missing
calibration or assume the old saved line was the last physically executed move.

## What went wrong

The investigated SV08 MAX stopped with `MCU 'mcu' shutdown: Move queue overflow`
during dense ironing, after earlier ironing regions had printed normally.
The part remained attached. That symptom does not by itself identify the cause
of every overflow on every machine.

Inspection of the preserved installed sources and shutdown evidence found:

| Issue | Specific symptom/consequence | What this package changes |
| --- | --- | --- |
| Sovol performs JSON write/flush/`fsync` inside the G-code movement handler, before submitting selected XYZ moves. | Storage stalls can block the host reactor while motion is being scheduled. Dense short-segment printing exposes timing pressure. | Dedicated I/O worker; Klipper waits cooperatively for durable batch completion. No filesystem I/O or per-move `M400` in normal G1 capture. |
| A parsed command/file cursor is not proof of executed motion. | The legacy checkpoint can be ahead of the actual stop; a text match can select the wrong repeated command. | Checkpoints wait for scheduled endpoints to elapse on all estimated MCU clocks and for the source line to finish parsing. Resume uses SHA256 plus an exact byte boundary and next-command check. |
| Allocation uses the advertised queue capacity with little additional reserve. | Host-issued step traffic can put pressure on the available slots; this is a mitigation, not proof of the shutdown's sole cause. | Host budget is `min(512, advertised_move_count // 2)`, then existing reserved slots are subtracted. |
| Stock recovery/startup macros perform additional operations. | Unwanted homing, feeding/extrusion, coordinate assumptions, or startup calibration can damage an attached print. | Dedicated recovery commands; no `START_PRINT`, `RESUME`, `RESUME_INTERRUPTED`, purge, QGL, or fresh mesh calibration. |
| Restart can lose active, unsaved calibration and adaptive mesh state. | Wrong Z reference/gap; using an adaptive mesh outside its measured area silently clamps the correction. | Restore the preserved Eddy curve and full-bed homing reference, then the exact original adaptive mesh/fade. Reject out-of-map homing rather than clamp. |
| A clear nozzle position is not necessarily a clear gantry position. | The full-width X beam or gantry-mounted PTFE/wiring holder can descend into a tall part elsewhere on the plate. | Rear-preferred homing plus CAD-informed front fallback that checks those larger envelopes. |

These changes address verified mechanisms and recovery hazards. They are **not
proof that every possible queue-overflow cause is eliminated**. The community
discussion that prompted the audit is credited in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Checkpoint behavior

Motion endpoints are grouped into approximately **100 ms** batches, closing
earlier at an extrusion-to-travel boundary. Short final tails are flushed even
without another move. An indivisible long move waits for its endpoint. Arcs or
macros producing several internal G1 moves cannot checkpoint the source line's
after-byte until that whole line returns successfully and its final endpoint
has elapsed.

Each selected batch gets an immediate worker write, file `fsync`, atomic rename
and directory `fsync`. Success is acknowledged only afterward; storage failures
shut down rather than silently disabling PLR. There is no seconds-long deferred
write timer. Python's GIL does not prevent the blocking filesystem calls from
releasing it; `completion.wait()` yields Klipper's reactor, not the OS thread.
JSON encoding still uses CPU/GIL time.

The canonical record is `/home/sovol/sovol_plr_height.batch.json`. A compatible
legacy XYZ/Z record is also retained, but **do not use the stock recovery reader**
as the new resume path. Those two files are not a multi-file atomic transaction.

The 512 limit is an **MCU step-queue budget**, not a count of G-code lines in a
JSON batch. Compressed step entries and G-code lines are not one-to-one. The
separate 16,384-record host-memory bound applies cooperative backpressure.

## Recovery commands

```gcode
PLR_HOME DRY_RUN=1
PLR_HOME CONFIRM=1
PLR_RESUME DRY_RUN=1
PLR_RESUME CONFIRM=1
```

`DRY_RUN=1` checks the bundle, source, native mesh reconstruction, geometry and
homing plan. It does not restore state, heat, home, move, extrude or start.

`PLR_HOME` restores the preserved home-reference mesh/calibration, provisionally
sets **only Z** from the reviewed height estimate, lifts vertically, homes X/Y
using native handlers, moves to the clear location, probes Z, transfers the
datum back to the normal homing reference, and parks above the print. It reports
scan bytes/percentage and phase updates about every five seconds. There is no
upper-Z endstop: a configured maximum is not a homing sensor.

`PLR_RESUME` calls that same home routine **when a matching completed recovery
home/park is absent**. It then restores the original print mesh, offsets, flow,
speed, pressure advance, limits, fans and object metadata. It lifts to at least
10 mm above the restart Z and above the occupied-height bound, moves X/Y, lowers
to the restart Z, restores E/modes, and starts the original file at the exact
byte. The original next command owns any unretraction—no extra E is added.

A durable one-shot attempt record is created before resume actions. An error,
ambiguous response or reboot must not trigger an automatic retry. Inspect the
printer and record before deliberately preparing a new recovery. An ordinary
paused print is not this workflow.

## Installation

See [INSTALL.md](docs/INSTALL.md) for audited commands, upgrade and rollback.
In brief:

1. Use a safely idle printer; back up configurations and recovery evidence.
   **Do not install/restart during a running print.** A restart interrupts heat
   and can lose unsaved state, so preserve it before restarting after a failure.
2. Clone this repository at `v1.0`. Use the printer's Python 3.9+ environment.
3. Run `tools/install.py --klipper /home/sovol/klipper` to audit exact supported
   source hashes. Unknown revisions fail closed; do not bypass the hash checks.
4. Stop Klipper yourself only when safe. Run the installer with `--apply` and a
   new backup directory. It never stops, starts or restarts services itself.
5. Install/include `config/plr.cfg`, remove the old `[plr_home]` include if any,
   and review [GEOMETRY.md](docs/GEOMETRY.md). Enable geometry confirmation only
   for a matching machine. Restart once under supervision when safe.
6. For a recovery, prepare the reviewed bundle described in
   [RECOVERY_BUNDLE.md](docs/RECOVERY_BUNDLE.md), pin its SHA256 in the config,
   run the dry run, inspect its plan, and use `CONFIRM=1` only when ready.

The macros require bed/nozzle to **already be stable at the saved targets**.
They do not heat automatically or impose a new fixed soak clock after a brief
reset. Actual thermal history and adhesion must be assessed by the operator.

## Limitations and caveats

- Elapsed scheduled MCU time is **not a hardware completion acknowledgment or
  encoder measurement**. Lost steps, asynchronous shutdown, clock estimation,
  storage latency and in-flight motion can leave an uncertain/replayed tail.
  This is not a zero-loss guarantee or a hard 100 ms durability guarantee.
- Background I/O keeps motion scheduling responsive; it does not magically
  persist moves completed after the last durable batch. Power loss during a
  write normally leaves an old or new valid record, not proof of latest motion.
  Storage/controller hardware must honor `fsync`; there is no UPS integration.
- v1 does **not automatically capture every runtime state change or construct
  a ready bundle from the checkpoint alone**. Preserve active calibration,
  meshes and context; review the restart boundary. Missing state is a blocker,
  not permission to run a new calibration on an occupied bed.
- Initial Z is an estimate. Gantry sag, moved axes/plate, detached or curled
  prints, loose cables, nozzle blobs, changed hotends/probes, and thermal drift
  are not detected by G-code. Do not recover an obviously disturbed setup.
- Geometry targets the stock, unenclosed 500 mm machine. Enclosures can reduce
  usable Z; aftermarket holders/toolheads and different cable routing require
  a new measured envelope. The flexible tube model is conservative, not a
  physical simulation. Front fallback can legitimately find no safe location.
- Supported recovery is single-extruder, reviewed absolute-XYZ G-code at the
  restart point, with no excluded objects, custom mesh offsets/fade target,
  firmware-retraction state, tool changes or unmodeled coordinate systems.
  Arbitrary macros may change hidden state. Overridden restoration commands and
  unaudited start-event handlers are rejected; other custom plugins need review.
- The scan includes supports/brims and conservatively reserves the whole file's
  footprint. This can reject a physically usable spot. Out-of-bed deposition is
  rejected. Raster, bead-width and clearance margins are included.
- The earlier assisted recovery began printing successfully and the owner
  reported it working. That is not a completed-print result or physical
  validation of these new packaged macros/front fallback. **This release was
  tested offline; it was not deployed to the running printer.**

## Tests and provenance

```sh
python -m pip install pytest
python -m pytest -q
```

Tests cover persistence failures, asynchronous checkpoint state, grouped
arc/macro boundaries, MCU budget/reservations, G-code parsing, rear/front
geometry, native mesh reconstruction, exact-byte native SD continuation,
no-extra-extrusion setup, duplicate-start prevention and install/rollback.
See [REVIEW.md](docs/REVIEW.md) for the release review and offline results.

Original utilities/docs are MIT licensed; preserved Klipper sources and derived
patches retain GPL licensing and attribution. See [LICENSE](LICENSE) and
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
