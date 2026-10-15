import shutil

import pytest

from tools import install


def target(tmp_path):
    root = tmp_path / "klipper"
    (root / "klippy/extras").mkdir(parents=True)
    for name in (*install.PINS, "gcode_move.py", "mcu.py"):
        dest = root / ("klippy/mcu.py" if name == "mcu.py" else "klippy/extras/" + name)
        shutil.copyfile(install.ROOT / "tests/fixtures" / (name + ".txt"), dest)
    return root


def test_audit_then_install_readback_and_exact_rollback(tmp_path):
    root = target(tmp_path)
    before = {
        str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }
    outputs = install.build(root)
    assert before == {
        str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }
    backup = tmp_path / "backup"
    install.apply(root, backup, outputs, offline=True)
    for relative, data in outputs.items():
        assert (root / relative).read_bytes() == data
    assert install.build(root) == outputs
    install.rollback(root, backup, offline=True)
    after = {
        str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }
    assert before == after
    assert (backup / "manifest.json").exists()


def test_unsupported_baseline_never_patched(tmp_path):
    root = target(tmp_path)
    path = root / "klippy/extras/gcode_move.py"
    path.write_bytes(path.read_bytes() + b"\n# local changes\n")
    with pytest.raises(ValueError, match="baseline"):
        install.build(root)


def test_existing_extra_requires_exact_reviewed_hash(tmp_path):
    root = target(tmp_path)
    path = root / "klippy/extras/plr_home.py"
    path.write_bytes(b"# local home customization\n")
    with pytest.raises(ValueError, match="Existing extra"):
        install.build(root)
    assert install.build(root, {"plr_home.py": install.sha(path.read_bytes())})


def test_rollback_refuses_post_install_changes(tmp_path):
    root = target(tmp_path)
    backup = tmp_path / "backup"
    install.apply(root, backup, install.build(root), offline=True)
    path = root / "klippy/extras/plr_batch.py"
    path.write_bytes(path.read_bytes() + b"\n# owner change\n")
    with pytest.raises(ValueError, match="changed since install"):
        install.rollback(root, backup, offline=True)


def test_backup_and_paths_fail_closed(tmp_path):
    root = target(tmp_path)
    with pytest.raises(ValueError, match="escapes"):
        install.safe_path(root, "../outside")
    with pytest.raises(ValueError, match="outside the target"):
        install.apply(root, root / "backup", install.build(root), offline=True)
    backup = tmp_path / "exists"
    backup.mkdir()
    with pytest.raises(FileExistsError):
        install.apply(root, backup, install.build(root), offline=True)
