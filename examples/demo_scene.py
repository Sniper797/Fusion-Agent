"""Create a NEW design document with a test mount (plate + round pin) and a
gear-like part whose blind bore is too wide, so the pin runs 8 mm into solid
material. A safe playground for snap_fit.py; other documents are not touched.

    python run_job.py examples/demo_scene.py
    python run_job.py examples/check_clearance.py      -> about 905 mm3 of interference
    python run_job.py examples/snap_fit.py --args-file examples/demo_args.json
    python run_job.py examples/check_clearance.py      -> 0 interference, 0.2 mm gap

Geometry (mm): mount face at y = 0, pin dia 12 x 20 along -Y; part dia 50 x 30
from y = -2 to -32 with an old bore dia 18 x 10 from its back face.
Close the document without saving when you are done.
"""
tbm = adsk.fusion.TemporaryBRepManager.get()
V = adsk.core.Vector3D.create

app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
d = design()
d.designType = adsk.fusion.DesignTypes.ParametricDesignType
root = d.rootComponent


def new_component(name, body):
    occ = root.occurrences.addNewComponent(adsk.core.Matrix3D.create())
    comp = occ.component
    comp.name = name
    bf = comp.features.baseFeatures.add()
    bf.startEdit()
    try:
        comp.bRepBodies.add(body, bf)
    finally:
        bf.finishEdit()
    return comp


plate = tbm.createBox(adsk.core.OrientedBoundingBox3D.create(
    point_mm(0, 5, 0), V(1, 0, 0), V(0, 1, 0), cm(60), cm(10), cm(60)))
pin = tbm.createCylinderOrCone(point_mm(0, 5, 0), cm(6), point_mm(0, -20, 0), cm(6))
tbm.booleanOperation(plate, pin, adsk.fusion.BooleanTypes.UnionBooleanType)
new_component("Mount", plate)

part = tbm.createCylinderOrCone(point_mm(0, -2, 0), cm(25), point_mm(0, -32, 0), cm(25))
old_bore = tbm.createCylinderOrCone(point_mm(0, -1, 0), cm(9), point_mm(0, -12, 0), cm(9))
tbm.booleanOperation(part, old_bore, adsk.fusion.BooleanTypes.DifferenceBooleanType)
new_component("Gear", part)

cam = app.activeViewport.camera
cam.isFitView = True
app.activeViewport.camera = cam

out["document"] = app.activeDocument.name
out["components"] = [o.component.name for o in root.allOccurrences]
print("demo ready in " + out["document"])
