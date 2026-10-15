# Rear-first homing and front fallback

## Primary-source measurements

Source: [Sovol's official SV08MAX repository](https://github.com/Sovol3d/SV08MAX/tree/68c28ea4ef5a81279d69c0e7d3b6433f0945192b/STEP),
commit `68c28ea4ef5a81279d69c0e7d3b6433f0945192b`.
Archive `MP4802-0000 500尺寸3D打印机 V8 3d printer.zip`:
SHA256 `3858dc2342b5e0af51f12459a7cde42a21bc43a4d66cec1764eaa6217743d069`.
Extracted STEP SHA256:
`0e8da3f8980dbe0e61d960b7c77f1c105a07359f655c379e0321389d7d585410`.

`tools/measure_cad.py` traversed assembly instance transforms and measured 1,038
leaf instances using OpenCascade 8.0.1.1.0. The rigid nozzle tip datum in assembly
coordinates is approximately `(335.47522, 445.07842, 425.00270)` mm. Increasing
printer X/Y is opposite increasing assembly X/Y; printer +Y is the rear. Bed-X
placement uses a nominal centered 500 mm printable span on the 505 mm substrate;
edge/datum uncertainty is included in rounded bounds and clearance padding.

These are **our measurements/inferences from vendor CAD**, not vendor-certified
collision limits or measurements of the actual printer. Part identifiers and
English descriptions distinguish components despite garbled STEP name encoding.

| Rigid component | CAD identifier | Measured Y behind nozzle | Measured height above tip |
| --- | --- | --- | --- |
| Moving X extrusion | `JXHMP4802-03002-a`, front profile | 40.720–60.720 mm | 46.700–66.700 mm |
| X linear rail | `MGNR12R566C` | 32.720–40.720 mm | 50.700–62.700 mm |
| Gantry bracket mounting foot | `JXHMP4802-03007-d`, X line/cable bracket fixing seat | 60.720–85.720 mm | 46.700–78.595 mm |
| Upright PTFE/wiring-guide envelope | `JXHMP4802-03008-d`, wiring bracket | 68.720–83.720 mm | 66.700–153.757 mm |

The last two are the gantry-mounted assembly relevant to the raised tube/cable
support—not the small PTFE coupling on top of the extruder. Confirm the physical
holder actually matches those parts. The CAD also depicts a long flexible tube
and harness; their one drawn pose cannot establish all real routing positions.
Sovol separately warns that cable interference at Teflon-tube fixation points
can tug connections during motion. See its [communication/cable guidance](https://wiki.sovol3d.com/en/SV08Max/Lost-communication-with-MCU-%26-Unable-to-connect).

## Implemented envelopes

Nozzle/toolhead: a conservative **75 mm XY radius** for rigid toolhead parts,
plus 5 mm clearance and raster/bead allowance. This encompasses the drawn rigid
head bounding rectangle, including the rear fan/motor/knob. The old 65 mm radius
was expanded after CAD review.

The gantry rectangles below use **bed-fixed X**, but **nozzle-relative Y/Z**.
They are tested over the entire downward homing sweep, including a -5 mm nozzle
floor allowance and a further 5 mm vertical margin.

| Envelope | Bed X | Y relative to nozzle | Conservative lower Z |
| --- | --- | --- | --- |
| Full-width X beam/rail | 0…500 | +30…+65 | +43 |
| Bracket mounting foot | 355…420 | +58…+89 | +43 |
| Upright bracket | 320…500 | +65…+88 | +63 |
| Flexible tube/harness uncertainty | 0…500 | -10…+300 | +90 |

All values are mm. The flexible envelope is an **assumption**, deliberately much
wider than its single CAD pose. A drooping tube, relocated bracket, accessory,
different hotend or enclosure invalidates stock confirmation. Do not shrink the
envelope simply to obtain a plan; measure a changed arrangement and test it.

## Selection and swept clearance

1. Scan positive-extrusion G-code, including supports and brims, into 2 mm cells.
   Track the deposited prefix height and hash every source byte. The whole-file
   XY footprint is reserved conservatively; future heights are capped at the
   maximum possibly printed prefix. Saved mesh/offset allowance is added.
2. Try rear nozzle Y=490 first, searching X. The probe must remain inside the
   preserved full-bed reference. A clear rear candidate always wins.
3. If rear is unavailable, try the front row: nominal Y=15, moved inward to the
   next 5 mm grid row when the saved reference bounds require it (Y=20 for a
   reference starting at probe Y=15). Check nozzle, probe **and all four gantry
   envelopes**. If any descending envelope meets a printed column, reject it.
4. All X/Y homing and travel occur after a vertical lift above the global
   occupied-height bound. Because modeled hardware has no part below the nozzle,
   that global plane clears its swept XY envelope under the stated Z/geometry
   assumptions. Descend only at the accepted location; then lift back to park.

The rail begins around +32.7 mm behind the nozzle, so the combined beam/rail
envelope starts at +30, not at the extrusion's +40.7. This distinction is covered
by a regression test. A tall object can block the beam at a completely different
X from the nozzle. Conversely, a low object below the padded underside need not
block front fallback. The holder uses bed-fixed X because it does not follow
the toolhead along the rail.

This is a practical conservative clearance model, not a physics guarantee.
Unknown overhang deformation, cable motion, bed/gantry shifts and objects absent
from the source are outside it. Initial Z remains an estimate after lost homing;
if Z is still genuinely homed, the current toolhead Z is used instead. A normal
matching PLR home is reused without a second lift/home.

## Before enabling physical recovery

Verify +Y is rear, the stock holder/foot and routing are present in the modeled
locations, the nozzle/probe offsets match, the plate and print have not moved,
and no accessories occupy the sweep. Confirm upper clearance: the standard
500 mm machine and an enclosed/modified machine are not interchangeable.
Sovol lists reduced build height with its enclosure on the [product page](https://www.sovol3d.com/products/sovol-sv08-max-3d-printer).
Then set `stock_geometry_confirmed: True`; otherwise leave it False.

Reproduce CAD measurements on a workstation (optional, not on the printer):

```sh
python -m pip install cadquery-ocp
python tools/measure_cad.py path/to/official.STEP --output local/cad-bounds.json
```

The CAD is not bundled. The new front-fallback path has offline collision and
sequence tests, **not an occupied-bed physical validation**.
