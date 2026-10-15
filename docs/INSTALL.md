# Installation, upgrade and rollback

## Supported baseline

Linux/Sovol Klipper with Python 3.9 or later; stock unenclosed SV08 MAX geometry
and Eddy probe offsets X=-19.8, Y=-0.75. This is **not** a blanket compatibility
claim for every firmware carrying the SV08 MAX name.

The installer compares the native files to the exact GPL source snapshots in
`tests/fixtures/`. Important original SHA256 pins:

| File | SHA256 |
| --- | --- |
| `gcode_move.py` | `45ba5045cfbfcd765fbdef2e3905145a6192fc4cb75b614b3ff612a55f325511` |
| `mcu.py` | `9733f1e2e4a7b8921fc7f532a23514167e8d619558a7d3dffbbb063ef665b1e6` |
| `virtual_sdcard.py` | `cfad85629ee278bfc8af282ebb716e3031183a716fddf87b61ba2d9900e6c4b7` |
| `bed_mesh.py` | `babab824dcf173e25ec2b4a13cc789e1fa0b23f438822f8e43c4f819034d9357` |
| `probe_eddy_current.py` | `83af6762b25b3e6b4b6b8f20e7666aaae8b02c7b7ff332de780a22e3cec12c5e` |
| `homing.py` | `816f8102cda11641ef594d67edf3d24da212f555d1c8f3857670fce1a54639a7` |
| `idle_timeout.py` | `8e1b4ccfb4b6f20bb8d76b98bace2567e77d47bc4e899eddbb37a241257cd6a4` |

Only `gcode_move.py` and `mcu.py` are patched. The other native files are verified,
not replaced. Five extras are installed. Firmware updates may overwrite patches
or change these interfaces: re-audit afterward; do not force an unsupported hash.

## Fresh installation

Do this during maintenance with a safely idle printer. Do not run these commands
against a printer actively printing. A stop/restart interrupts heating; after a
failure, capture the active state and checkpoint evidence **before** any restart.
The installer neither sends printer commands nor controls heaters/services.

On the printer, as its normal `sovol` account:

```sh
git clone https://github.com/unixwizard/SV08MaxUtils.git
cd SV08MaxUtils
git checkout v1.0
/home/sovol/klippy-env/bin/python tools/install.py --klipper /home/sovol/klipper
```

The last command is an audit only. Read the entire result. A hash mismatch is
not fixed by copying this repository's fixture over the printer's native file.
Port/review/test the patch for that revision first.

When the printer is safe to stop, and after a separate backup of `printer.cfg`,
its included configs, `saved_variables.cfg`, and any recovery evidence:

```sh
sudo systemctl stop klipper
/home/sovol/klippy-env/bin/python tools/install.py \
  --klipper /home/sovol/klipper --apply \
  --backup /home/sovol/plr-backup-v1.0
```

The backup directory must not already exist. Every old file is copied and hash
checked before the first replacement. Installed bytes are read back and checked.
A manifest records both sides, including files that did not previously exist.
If interrupted, keep Klipper stopped and inspect/roll back that manifest.

Copy `config/plr.cfg` into `/home/sovol/printer_data/config/` and include it once
from `printer.cfg`:

```ini
[include plr.cfg]
```

Use a different include filename if an existing vendor file is already named
`plr.cfg`; **do not overwrite the vendor configuration blindly**. For example,
copy this package's config as `sv08max_plr.cfg` and include that filename instead.
Remove the previous custom `[plr_home]` section/include, and any older definitions
of `PLR_HOME`/`PLR_RESUME`, to prevent duplicate command registration. Keep stock
normal-start/homing/mesh/QGL/calibration definitions intact. Do not route the new
recovery through stock `RESUME` or `RESUME_INTERRUPTED`.

Review [GEOMETRY.md](GEOMETRY.md). Set `stock_geometry_confirmed: True` only when
the stock hardware, routing, coordinate direction and upper clearance match.
The supplied False default intentionally prevents physical recovery until then.
A blank `bundle_sha256` lets Klipper load normally but blocks recovery commands.
Install a reviewed bundle and hash using [RECOVERY_BUNDLE.md](RECOVERY_BUNDLE.md)
before attempting recovery.

Finally, when safe:

```sh
sudo systemctl start klipper
```

Verify ready state, console errors and the new MCU budget log. Verify actual
heater state rather than assuming a service restart preserved it. The first
physical trial should be supervised with a disposable low print and an accessible
emergency stop. A firmware restart is never part of either recovery macro.

## Upgrade from the earlier local BFP helpers

Those helpers have different hashes and lack this packaged front fallback and
resume interface. Inspect each differing extra; the installer reports its name.
To replace a **reviewed** old extra, add its exact current hash:

```sh
/home/sovol/klippy-env/bin/python tools/install.py \
  --klipper /home/sovol/klipper \
  --replace-extra plr_home.py=EXACT_REVIEWED_OLD_SHA256
```

Repeat `--replace-extra NAME=SHA256` for other reviewed differences, then use the
same pins with `--apply` and a new backup directory. This option cannot override
unsupported native Klipper source pins. Do not copy a placeholder as a hash.
The old one-job recovery loader is not used by these macros; preserve its files
as evidence until that recovery is completed, and never update it mid-print.

## Rollback

When safely idle, stop Klipper yourself and use the retained backup:

```sh
/home/sovol/klippy-env/bin/python tools/install.py \
  --klipper /home/sovol/klipper --rollback /home/sovol/plr-backup-v1.0
```

Rollback refuses to overwrite post-install changes. It restores old files and
removes only exact newly installed extras recorded in that manifest. The backup
remains recoverable. Restore the previous config include/definitions from your
config backup before starting Klipper. No service restart is performed for you.

`--offline` is solely for disposable workstation copies in tests, not an escape
from the live-service guard. On Linux even offline mode rejects a target checkout
that has a running Klipper process.
