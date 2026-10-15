import pytest
import json
import math
from pathlib import Path
from klippy.extras import plr_home as home


def footprint():
    fp = home.Footprint(home.DEFAULTS)
    fp.add_line([0, 495, 1], [500, 495, 1])  # rear unavailable
    return fp


def test_empty_front_fallback_and_whole_bed_xy_travel_height():
    fp = footprint()
    place = home.choose_location(fp, home.DEFAULTS, 1)
    assert place["homing_zone"] == "front_fallback"
    assert set(place["checked_envelopes"]) == {e[0] for e in home.GANTRY_ENVELOPES}


def test_beam_hits_tall_object_far_from_selected_nozzle_x():
    fp = footprint()
    fp.add_line([5, 65, 80], [10, 65, 80])
    with pytest.raises(home.PlanError, match="x_beam_and_rail"):
        home.choose_location(fp, home.DEFAULTS, 80)


def test_low_object_under_beam_can_clear():
    fp = footprint()
    fp.add_line([5, 65, 10], [10, 65, 10])
    assert (
        home.choose_location(fp, home.DEFAULTS, 10)["homing_zone"] == "front_fallback"
    )


def test_bracket_foot_can_collide_behind_beam_band():
    fp = footprint()
    fp.add_line([400, 112, 45], [405, 112, 45])
    conflicts = home.gantry_conflicts(fp, home.DEFAULTS, 20, 45)
    assert "ptfe_bracket_mount" in conflicts
    assert "x_beam_and_rail" not in conflicts


def test_higher_bracket_and_flexible_tube_have_distinct_envelopes():
    fp = footprint()
    fp.add_line([340, 107, 70], [342, 107, 70])
    assert "ptfe_wiring_bracket" in home.gantry_conflicts(fp, home.DEFAULTS, 20, 70)
    fp.add_line([200, 250, 110], [202, 250, 110])
    assert "flexible_tube_and_wiring" in home.gantry_conflicts(
        fp, home.DEFAULTS, 20, 110
    )


def test_saved_mesh_bounds_move_front_probe_inward_without_clamping():
    fp = footprint()
    cfg = dict(home.DEFAULTS, probe_bounds=[13, 15, 475.56, 489.95])
    place = home.choose_location(fp, cfg, 1)
    assert place["nozzle_xy"][1] == 20
    assert place["probe_xy"][1] == 19.25


def test_absolute_height_allowance_is_included_in_collision_check():
    fp = footprint()
    fp.add_line([5, 65, 30], [10, 65, 30])
    assert not home.gantry_conflicts(fp, home.DEFAULTS, 15, 30)
    assert "x_beam_and_rail" in home.gantry_conflicts(
        fp, dict(home.DEFAULTS, height_allowance=4), 15, 30
    )


def test_front_of_rail_is_not_mistaken_for_extrusion_front():
    fp = footprint()
    fp.add_line([5, 43, 60], [10, 43, 60])
    assert "x_beam_and_rail" in home.gantry_conflicts(fp, home.DEFAULTS, 20, 60)


def test_implemented_envelopes_enclose_the_pinned_cad_measurements():
    measured = json.loads(
        (
            Path(__file__).resolve().parents[1] / "docs/geometry-source-bounds.json"
        ).read_text()
    )
    nx, ny, nz = measured["nozzle_tip"]
    envelopes = {v[0]: v[1:] for v in home.GANTRY_ENVELOPES}
    for part in measured["components"]:
        x0, x1, y0, y1, bottom = envelopes[part["envelope"]]
        a, b, c, d, e, _ = part["bounds"]
        assert y0 <= ny - e <= ny - b <= y1
        assert bottom <= c - nz
        if part["envelope"] != "x_beam_and_rail":
            assert x0 <= measured["nominal_bed_x_origin"] - d
            assert measured["nominal_bed_x_origin"] - a <= x1
    a, b, c, d = measured["rigid_toolhead_xy_union"]
    assert (
        max(math.hypot(x - nx, y - ny) for x in (a, c) for y in (b, d))
        <= home.DEFAULTS["head_radius"]
    )
