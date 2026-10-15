# v1.0 review and offline verification

Reviewed 2026-10-15. This is a code/source and offline-test review by the
implementer, **not an independent hardware certification**.

## Review findings addressed before baseline

- Separated parser progress from scheduled motion completion; kept explicit
  no-hardware-ACK limitations and a reviewed-bundle requirement.
- Found the multi-G1 source-line hazard: arcs/macros can have an after-byte
  pointing past the whole line before all internal moves finish. Grouped those
  captures, retained only the final endpoint and gated on native SD line
  completion. Added partial-arc and concurrent-capture regressions.
- Kept the 100 ms persistence cadence separate from the MCU step-slot budget;
  tested the 512/half-capacity cap with existing reservations.
- Expanded rigid toolhead radius to 75 mm after CAD inspection. Included the
  X rail's more forward edge (+32.7 mm), not only the extrusion's +40.7 mm edge.
  Added full-width beam, bracket-foot, upright-holder, flexible-routing and
  height-allowance collision regressions.
- Made front-row selection honor preserved reference-mesh bounds. Outside-map
  datum correction, out-of-bed deposition and unsupported coordinate modes fail
  closed. Missing geometry confirmation blocks live macros.
- Restored the exact active Eddy curve and reconstructed native adaptive mesh;
  preserved the full-bed home reference separately. Rejected nonstandard fade
  target/tool offsets and unreviewed state rather than guessing them.
- Made resume reuse a matching recovery-home park, but use current homed Z
  instead of a stale interruption estimate when a new home is necessary.
- Reset intervening G92 XYZ rebasing before offset restoration. Preserved E
  mode/coordinate without adding retraction, unretraction, prime or purge.
- Loaded/reset the native SD file before restoring object metadata, then bound
  the loaded handle to the verified source. Tested native `work_handler` seeking
  the exact byte and executing only the original tail.
- Rejected uppercase/lowercase macro overrides and unaudited start-event handlers.
  Checked temperature before claiming/performing resume; no automatic heating.
- Created a durable exclusive resume-attempt ledger before action; ambiguous
  responses/errors remain consumed across restarts. No automatic retry path.
- Installer audits by default, pins native sources, stops on unknown changes,
  requires operator-stopped Klipper, preserves durable backups and does exact
  readback. Rollback refuses to overwrite later edits and validates every target.
- Kept private printer state, G-code, CAD downloads, credentials and local paths
  out of the public Git tree. Retained upstream copyright/license headers.

## Offline evidence

The release suite covers persistence fault injection, batching/source-line
boundaries, MCU budget accounting, planner/parser/progress guards, collision
envelopes, native mesh math, native SD seeking, one-shot resume and installer
rollback. Run `python -m pytest -q` from the repository root. The GitHub workflow
runs the same network-free tests on Linux with Python 3.9, 3.11 and 3.14.

On the development workstation the final suite passes **118 tests**. Ruff's
fatal-error/undefined-name checks and Python-3.9 syntax parsing also pass.

An additional private offline replay used the unchanged **117,054,583-byte**
source from the actual interruption and the previously preserved recovery state:

- Exact original restart boundary and next unretraction matched.
- Every saved interpolated adaptive-mesh value reconstructed identically:
  maximum error **0.0 mm** using the pinned native Klipper implementation.
- New planner selected the rear, nozzle **X315/Y490**, with clearance lift
  **Z19.146532 mm** under that job's saved-height assumptions.
- Full-bed reference datum transfer at that point was approximately
  **-0.227897 mm**. This is job/reference-specific, not a universal Z offset.
- Source and old artifacts remained unchanged. **Zero printer connections,
  motion, heating, uploads, installations, restarts or starts** were made for
  this release work.

Private replay artifacts are deliberately not published. The included synthetic
fixtures and native-source tests are reproducible without them.

## Not established by these tests

No occupied-bed physical trial of the new front fallback or packaged macros was
performed. No deliberate power cut, long-duration storage endurance test,
measured gantry-sag bound, thermal requalification, or cross-firmware field
validation was performed. The earlier assisted recovery's reported successful
restart is encouraging but is not a test result for these newly packaged paths.

Offline success is a release baseline, not permission to bypass the physical
assumptions or replace unknown printer source files. First live deployment and
trial require a separate safe maintenance window.
