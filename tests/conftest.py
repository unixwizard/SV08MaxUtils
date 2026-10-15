import hashlib
import json
from pathlib import Path
import sys
import types

import pytest


@pytest.fixture
def native():
    package = types.ModuleType("sv08_native_test")
    package.__path__ = []
    sys.modules[package.__name__] = package
    sys.modules[package.__name__ + ".probe"] = types.ModuleType(
        package.__name__ + ".probe"
    )
    result = {}
    for name in ("bed_mesh", "virtual_sdcard"):
        module = types.ModuleType(package.__name__ + "." + name)
        module.__package__ = package.__name__
        path = Path(__file__).parent / "fixtures" / (name + ".py.txt")
        exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
        result[name] = module
    return result


@pytest.fixture
def bundle(tmp_path, native):
    params = dict(
        min_x=13.0,
        min_y=15.0,
        max_x=475.56,
        max_y=489.95,
        x_count=3,
        y_count=3,
        mesh_x_pps=0,
        mesh_y_pps=0,
        algo="lagrange",
        tension=0.2,
    )
    points = [[0.1, 0.15, 0.2], [0.15, 0.2, 0.25], [0.2, 0.25, 0.3]]
    mesh = native["bed_mesh"].ZMesh(params, "test")
    mesh.build_mesh(points)
    prefix = (
        b"G90\nM83\nG1 X200 Y200 Z10\nG1 X300 E1\nG1 Y300 E1\nG1 X200 E1\nG1 Y200 E1\n"
    )
    path = tmp_path / "sample.gcode"
    path.write_bytes(prefix + b"G1 E.8 F2400\nG1 X210 E1\n")
    data = dict(
        schema="sv08max.recovery.v1",
        reviewed=True,
        source_file=path.name,
        source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        resume=dict(byte=len(prefix), command="G1 E.8 F2400"),
        estimated_physical_z=10.3,
        runtime_z_offset=0.1,
        reg_drive_current=15,
        calibration=[[0.0, 4000000.0], [1.0, 3900000.0], [2.0, 3800000.0]],
        mesh=dict(
            params=params,
            points=points,
            matrix=mesh.get_mesh_matrix(),
            fade_start=0.0,
            fade_end=10.0,
            profile_name="test",
        ),
        home_reference=dict(mesh_params=params, points=points),
        max_velocity=700.0,
        square_corner_velocity=5.0,
        smooth_time=0.035,
        modal=dict(
            absolute=True,
            absolute_e=False,
            xyz=[200.0, 200.0, 10.0],
            e=4.0,
            feed=2400.0,
            speed_factor=1.0,
            extrude_factor=1.0,
            limits=dict(ACCEL=8000.0, ACCEL_TO_DECEL=4000.0),
            pa=dict(ADVANCE=0.06),
            fans={"0": 0.6, "1": 0.6},
            layer=50,
            bed=50.0,
            nozzle=207.0,
            objects=[],
            active_object=None,
        ),
    )
    raw = json.dumps(data).encode()
    saved = tmp_path / "bundle.json"
    saved.write_bytes(raw)
    return data, path, saved, hashlib.sha256(raw).hexdigest()
