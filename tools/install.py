"""Hash-pinned local installer/rollback. Audit by default; NEVER restarts Klipper.

Stop Klipper only after the printer is safely idle and heater implications are
understood. --offline is for disposable workstation copies, never a live host.
"""

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from patch_sources import patch_gcode, patch_mcu

ROOT = Path(__file__).resolve().parents[1]
EXTRAS = (
    "plr_checkpoint.py",
    "plr_batch.py",
    "plr_home.py",
    "plr_recovery.py",
    "sv08_plr.py",
)
PINS = (
    "virtual_sdcard.py",
    "bed_mesh.py",
    "probe_eddy_current.py",
    "homing.py",
    "idle_timeout.py",
)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def safe_path(root, relative):
    root = root.resolve()
    path = (root / relative).resolve()
    if root not in path.parents:
        raise ValueError("Target escapes Klipper root")
    return path


def inactive(root, offline=False):
    if offline:
        if os.name == "posix":
            # Even offline mode may not modify a process's running checkout.
            for command in Path("/proc").glob("[0-9]*/cmdline"):
                try:
                    text = (
                        command.read_bytes()
                        .replace(b"\0", b" ")
                        .decode(errors="replace")
                    )
                except OSError:
                    continue
                if str(root.resolve() / "klippy/klippy.py") in text:
                    raise ValueError("This checkout has a running Klipper process")
        return
    if os.name != "posix":
        raise ValueError(
            "Live install is Linux only; use --offline for a disposable test tree"
        )
    service = subprocess.run(
        ["systemctl", "is-active", "klipper"], capture_output=True, text=True
    )
    if service.stdout.strip() not in ("inactive", "failed"):
        raise ValueError(
            "Klipper must be stopped by the operator first; no automatic stop/restart"
        )
    inactive(root, offline=True)


def build(root, replacements=None):
    replacements = replacements or {}
    outputs = {}
    for name in PINS:
        path = safe_path(root, "klippy/extras/" + name)
        expected = sha((ROOT / "tests/fixtures" / (name + ".txt")).read_bytes())
        if sha(path.read_bytes()) != expected:
            raise ValueError("Unsupported native module: " + name)
    for name, relative, patch in (
        ("gcode_move.py", "klippy/extras/gcode_move.py", patch_gcode),
        ("mcu.py", "klippy/mcu.py", patch_mcu),
    ):
        original = (ROOT / "tests/fixtures" / (name + ".txt")).read_bytes()
        patched, _ = patch(original)
        current = safe_path(root, relative).read_bytes()
        if sha(current) not in (sha(original), sha(patched)):
            raise ValueError("Unsupported or modified baseline: " + name)
        outputs[relative] = patched
    for name in EXTRAS:
        relative = "klippy/extras/" + name
        path = safe_path(root, relative)
        new = (ROOT / relative).read_bytes()
        if path.exists() and sha(path.read_bytes()) != sha(new):
            if replacements.get(name) != sha(path.read_bytes()):
                raise ValueError(
                    "Existing extra differs: "
                    + name
                    + "; inspect then pin with --replace-extra NAME=SHA256"
                )
        outputs[relative] = new
    for data in outputs.values():
        ast.parse(data.decode(), feature_version=(3, 9))
    return outputs


def atomic_write(path, data, mode=None):
    fd, temporary = tempfile.mkstemp(prefix=".sv08plr-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
        temporary = None
        if hasattr(os, "O_DIRECTORY"):
            directory = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if temporary is not None:
            os.unlink(temporary)


def apply(root, backup, outputs, offline=False):
    inactive(root, offline)
    if backup.resolve() == root.resolve() or root.resolve() in backup.resolve().parents:
        raise ValueError("Keep backup outside the target Klipper tree")
    backup.mkdir(parents=True, exist_ok=False)
    manifest = dict(
        schema="sv08max.install.v1",
        root=str(root.resolve()),
        utc=datetime.now(timezone.utc).isoformat(),
        files={},
    )
    for relative, data in outputs.items():
        target = safe_path(root, relative)
        existed = target.exists()
        old = target.read_bytes() if existed else None
        if existed:
            saved = safe_path(backup, relative)
            saved.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(saved, old, target.stat().st_mode)
            shutil.copystat(target, saved)
            if sha(saved.read_bytes()) != sha(old):
                raise ValueError("Backup verification failed")
        manifest["files"][relative] = dict(
            before=sha(old) if existed else None,
            after=sha(data),
            mode=target.stat().st_mode if existed else 0o644,
        )
    atomic_write(
        backup / "manifest.json",
        (json.dumps(manifest, indent=2) + "\n").encode(),
        0o600,
    )
    if hasattr(os, "O_DIRECTORY"):
        for path in [p for p in backup.rglob("*") if p.is_dir()] + [
            backup,
            backup.parent,
        ]:
            fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    # Backups and manifest are complete before modifying the first source file.
    for relative, data in outputs.items():
        target = safe_path(root, relative)
        record = manifest["files"][relative]
        if (sha(target.read_bytes()) if target.exists() else None) != record["before"]:
            raise ValueError("Target changed after backup; stop and inspect manifest")
        atomic_write(target, data, record["mode"])
        if sha(target.read_bytes()) != record["after"]:
            raise ValueError("Installed readback differs")
    return manifest


def rollback(root, backup, offline=False):
    inactive(root, offline)
    manifest = json.loads((backup / "manifest.json").read_text())
    if (
        manifest["schema"] != "sv08max.install.v1"
        or Path(manifest["root"]).resolve() != root.resolve()
    ):
        raise ValueError("Backup belongs to a different target")
    allowed = {"klippy/extras/" + n for n in EXTRAS} | {
        "klippy/extras/gcode_move.py",
        "klippy/mcu.py",
    }
    for relative, record in manifest["files"].items():
        if relative not in allowed:
            raise ValueError("Unexpected rollback target")
        target = safe_path(root, relative)
        current = sha(target.read_bytes()) if target.exists() else None
        if current not in (record["before"], record["after"]):
            raise ValueError("Target changed since install: " + relative)
        if (
            record["before"] is not None
            and sha(safe_path(backup, relative).read_bytes()) != record["before"]
        ):
            raise ValueError("Backup hash mismatch")
    for relative, record in manifest["files"].items():
        target = safe_path(root, relative)
        if record["before"] is None:
            if target.exists():
                target.unlink()  # only exact new files verified above; backup manifest retained
        else:
            atomic_write(
                target, safe_path(backup, relative).read_bytes(), record["mode"]
            )
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--klipper", type=Path, required=True)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true")
    group.add_argument("--rollback", type=Path)
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--replace-extra", action="append", default=[])
    args = parser.parse_args()
    if args.rollback:
        rollback(args.klipper, args.rollback, args.offline)
        print(
            "Exact files restored; newly installed extras removed. Backup retained. No restart performed."
        )
        return
    replacements = dict(value.split("=", 1) for value in args.replace_extra)
    outputs = build(args.klipper, replacements)
    print(json.dumps({name: sha(data) for name, data in outputs.items()}, indent=2))
    if args.apply:
        if args.backup is None:
            parser.error("--apply requires a new --backup directory")
        apply(args.klipper, args.backup, outputs, args.offline)
        print(
            "Installed and readback verified. No config include or restart performed."
        )
    else:
        print("AUDIT ONLY: compatible sources; nothing modified.")


if __name__ == "__main__":
    main()
