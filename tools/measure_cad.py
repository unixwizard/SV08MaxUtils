"""Read component AABBs from Sovol's STEP assembly; no printer connection.

Optional workstation dependency: cadquery-ocp. Not used by Klipper.
"""

import argparse
import hashlib
import json
from pathlib import Path
import re

from OCP.Bnd import Bnd_Box
from OCP.BRepBndLib import BRepBndLib
from OCP.IFSelect import IFSelect_RetDone
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.TCollection import TCollection_ExtendedString
from OCP.TDataStd import TDataStd_Name
from OCP.TDF import TDF_Label

try:
    from OCP.TDF import TDF_LabelSequence
except ImportError:  # OpenCascade 8
    from OCP.collections import Sequence_TDF_Label as TDF_LabelSequence
from OCP.TDocStd import TDocStd_Document
from OCP.TopLoc import TopLoc_Location
from OCP.XCAFDoc import XCAFDoc_DocumentTool


def measure(path, pattern=None):
    doc = TDocStd_Document(TCollection_ExtendedString("MDTV-XCAF"))
    reader = STEPCAFControl_Reader()
    reader.SetNameMode(True)
    if reader.ReadFile(str(path)) != IFSelect_RetDone or not reader.Transfer(doc):
        raise ValueError("Could not import STEP")
    shapes = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    roots = TDF_LabelSequence()
    shapes.GetFreeShapes(roots)
    rows = []

    def name(label):
        attr = TDataStd_Name()
        return (
            attr.Get().ToExtString()
            if label.FindAttribute(TDataStd_Name.GetID_s(), attr)
            else ""
        )

    def walk(label, location, parents):
        loc = location.Multiplied(shapes.GetLocation_s(label))
        ref = TDF_Label()
        target = label
        instance_name = name(label)
        if shapes.GetReferredShape_s(label, ref):
            target = ref
        title = name(target) or instance_name
        components = TDF_LabelSequence()
        if shapes.GetComponents_s(target, components):
            for i in range(1, components.Length() + 1):
                walk(components.Value(i), loc, parents + [title])
        else:
            if pattern and not re.search(pattern, title):
                return
            original_shape = shapes.GetShape_s(target)
            shape = original_shape.Moved(loc)
            bounds = Bnd_Box()
            BRepBndLib.AddOptimal_s(shape, bounds, False, False)
            if not bounds.IsVoid():
                rows.append(
                    dict(
                        name=title,
                        instance=instance_name,
                        parents=parents,
                        definition_location_identity=original_shape.Location().IsIdentity(),
                        bounds=[
                            round(v, 5)
                            for v in (
                                *bounds.CornerMin().Coord(),
                                *bounds.CornerMax().Coord(),
                            )
                        ],
                    )
                )

    for i in range(1, roots.Length() + 1):
        walk(roots.Value(i), TopLoc_Location(), [])
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return dict(
        source_sha256=digest, units="mm; STEP assembly coordinates", components=rows
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("step", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--match", help="Optional component-name regular expression")
    args = parser.parse_args()
    result = measure(args.step, args.match)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print("Measured %d leaf instances" % len(result["components"]))
