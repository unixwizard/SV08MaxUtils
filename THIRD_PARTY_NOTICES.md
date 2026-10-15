# Attribution and license boundaries

Original SV08MaxUtils code and documentation: Copyright (c) 2026 unixwizard,
under the [MIT license](LICENSE). Retain that copyright and license notice in
copies/substantial portions. This is attribution through license notices, not
a requirement to display branding in a UI.

`tests/fixtures/*.py.txt` are unmodified source snapshots from Sovol's Klipper
installation/fork, retained to make source compatibility and native offline
tests reproducible. Each retains its original copyright/license header.
`tools/patch_sources.py` contains derived source anchors/patches and is GPLv3
or later. Generated patched Klipper files remain GPL-covered, **not MIT-only**.
Distributions incorporating Klipper must satisfy its GPL obligations; the MIT
license does not supersede them. Full license: [GPL-3.0.txt](licenses/GPL-3.0.txt).

Upstream credits include Kevin O'Connor and other Klipper contributors; Eric
Callahan for bed-mesh implementation; and Sovol for its SV08 MAX modifications.
See the preserved individual headers and [Klipper](https://github.com/Klipper3d/klipper)
and [Sovol's fork](https://github.com/Sovol3d/SV08MAX/tree/68c28ea4ef5a81279d69c0e7d3b6433f0945192b/home/sovol/klipper).

Problem research was informed by imagio's diagnosis and cardoc's discussion in
the [Sovol PLR/queue-overflow thread](https://forum.sovol3d.com/t/plr-and-mcu-shutdown-move-queue-overflow/9701).
Their downloadable repair package is not copied here, and their hardware test
results are not claimed as validation of this implementation.

Geometry measurements were derived from Sovol's official SV08 MAX STEP
assembly, with source identifiers in [GEOMETRY.md](docs/GEOMETRY.md). CAD/media
are not redistributed in this repository; numeric measurements and the
independently written measurement script are included. No affiliation or
endorsement by Sovol is implied.
