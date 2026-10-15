# Preparing an assisted recovery

The batch checkpoint is evidence, **not a complete recovery bundle**. In
particular, it does not contain a complete active Eddy calibration, full-bed
reference, adaptive mesh/fade, all fan/macro state or proof of physical motion.
Do not fill missing fields with convenient defaults.

## Preserve evidence before it disappears

After the job establishes its calibration/mesh, a read-only context snapshot can
be saved with:

```sh
mkdir -p local
python tools/capture_state.py --url http://127.0.0.1 --output local/context.json
```

Use the actual Moonraker base URL/port. An API key, if required, can be supplied
through `SV08_MOONRAKER_API_KEY`; it is not written to the snapshot. The tool makes
one GET request and sends no G-code. Repeat into a new file after material
calibration/mesh/runtime changes. This is not a scheduled background recorder.
After a failure, preserve the same state **before any restart**, along with the
unchanged original G-code, both checkpoint files and Klipper shutdown logs.

Keep these files private; full configuration/status can identify a machine.
Do not commit them to this public repository. `local/`, `artifacts/`, G-code and
research downloads are ignored. A cold reboot cannot recover missing RAM-only
calibration merely by reading the older on-disk config.

## Establish the restart boundary

Prefer evidence about the last completed scheduled motion, not `virtual_sdcard`
parser progress alone. Inspect the journal's source-line boundary and endpoint,
and reconcile shutdown timing/direction/extruder state with the original G-code.
Scheduled completion still is not encoder feedback. An interrupted extrusion
needs an explicit decision about replay/omission; this package does not silently
discard deposited commands or claim a perfect match from XY alone.

For the original assisted recovery, the stop was a positive-Y wipe/travel after
retraction, not the nearby opposite-direction extrusion. Selecting the existing
next unretraction and repositioning at its preceding XYZ skipped no deposition.
That reasoning is job-specific; do not reuse its byte or coordinates on another
print. Byte offsets are zero-based positions in the **original file bytes**, not
line numbers or character offsets in a newline-converted copy.

## Recipe fields

Prepare a JSON object with the fields below. The earlier local
`bfp-recovery-state-v1` bundle can also be imported because its reviewed state
uses these same fields. There is intentionally no ready-to-run example with
fictional calibration/coordinates.

| Field | Required evidence and meaning |
| --- | --- |
| `source_sha256` | SHA256 of the exact original G-code; optional in a new recipe, mandatory if importing a prior bound recipe. A supplied value must match. |
| `resume` | Object with integer `byte` and exact `command` at that boundary. First resumed command must be a reviewed G0/G1. |
| `estimated_physical_z` | Reviewed current nozzle Z estimate for the initial unhomed lift, not blindly the slicer's nominal Z. A currently homed Z supersedes it. |
| `runtime_z_offset` | Original effective runtime Z offset. No G92 XYZ coordinate rebasing is supported in the source. |
| `calibration` | Original **active** Eddy `[z, frequency]` pairs. Heights ascending, frequencies descending. Prefer preserved pending calibration over stale saved config when it was active. |
| `reg_drive_current` | Original Eddy current setting; must match installed configuration. |
| `home_reference` | `mesh_params` plus `points` from a preserved full-bed mesh that covers the chosen probe and normal homing-reference points. |
| `mesh` | `params`, `points` (probed matrix), exact interpolated `matrix`, `profile_name`, `fade_start`, `fade_end`. v1 uses automatic-average fade target, zero mesh/tool offset, enabled positive fade interval. |
| `max_velocity`, `square_corner_velocity`, `smooth_time` | Original toolhead maximum velocity/SCV and pressure-advance smoothing time. |
| `modal` | State immediately **before** the chosen first resumed command; see below. A later parser snapshot may not have this state. |
| `excluded_objects` | Must be empty in v1. Nonempty exclusions require further reconstruction/support. |
| `unsupported_state` | List any unhandled state: tool changes, firmware retraction, custom offsets/macros, etc. A nonempty list blocks preparation. |

`modal` contains:

- `xyz`: original G-code position `[x,y,z]` before the next command; `e`: current
  G-code extrusion coordinate; `feed`: modal F in mm/min.
- `absolute`: must be true for XYZ; `absolute_e`: true for M82, false for M83.
- `speed_factor`, `extrude_factor`: ratios, e.g. 1.0 for 100%, not percentages.
- `limits`: `ACCEL` and `ACCEL_TO_DECEL`, mm/s². The latter is converted to
  `MINIMUM_CRUISE_RATIO = 1 - ACCEL_TO_DECEL / ACCEL` for this supported fork.
- `pa`: object with `ADVANCE`; `bed` and `nozzle`: original target temperatures.
- `fans`: speed ratios keyed by stock fan number as strings (`"0"`, `"1"`, etc.).
  Include every relevant fan state; absent state is not automatically inferred.
- `layer`: current layer; `objects`: exact validated `EXCLUDE_OBJECT_DEFINE`
  strings; `active_object`: corresponding `EXCLUDE_OBJECT_START NAME=...`, or
  null. These strings are syntax-checked to reject injected commands.

Mesh `params`/`mesh_params` require `min_x`, `max_x`, `min_y`, `max_y`, `x_count`,
`y_count`, `mesh_x_pps`, `mesh_y_pps`, `algo` and `tension`. Do not substitute a
different interpolation algorithm or trim/resample a matrix to make it fit.

## Bind, verify, configure

Work on local copies. Use the same G-code filename that will exist in the
printer's virtual SD directory; the preparation tool binds that basename.

```sh
python tools/prepare_recovery.py --recipe local/reviewed-recipe.json \
  --gcode local/original-job.gcode --output local/plr-recovery.json --reviewed
python tools/offline_replay.py --bundle local/plr-recovery.json \
  --sd-root local --output local/offline-report.json
```

`--reviewed` is an explicit acknowledgment of the evidence review, not a way to
turn a guessed cursor into an executed one. Preparation checks the source hash,
line boundary, exact next command and structural state validity. Offline replay
also runs the occupancy scan and reconstructs the saved matrix using the pinned
native Klipper mesh implementation. Reports do not authorize physical motion.

Place the reviewed bundle at the configured `bundle_path`; set `bundle_sha256`
to the printed hash. This version reads the config hash at Klipper startup, so
plan the required configuration reload before homing, preserving state and
accounting for heater interruption. Do not reload/restart a successful ongoing
recovery just to change packaging or metadata.

At the printer, after state/source checks and thermal preparation:

```gcode
PLR_HOME DRY_RUN=1
PLR_HOME CONFIRM=1
PLR_RESUME CONFIRM=1
```

Or use `PLR_RESUME DRY_RUN=1` followed by `PLR_RESUME CONFIRM=1`; it invokes home
when necessary. `CONFIRM=1` acknowledges that the attached print/plate/gantry and
stock geometry/routing assumptions were checked. Stay with the printer.

An attempt record named `<bundle>.<hash-prefix>.attempt.json` persists across a
restart. Do not delete it or edit the bundle just to make a retry pass. First
inspect actual position, heat, extrusion and file state; an uncertain start is
not a failed start. Keep failed records for diagnosis and explicitly reconstruct
a new recovery if appropriate. Do not press ordinary SD start/resume to bypass
a failed PLR gate.
