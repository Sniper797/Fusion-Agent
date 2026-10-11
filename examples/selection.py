"""What is selected in Fusion right now, measured, with a token for each item that a later
job can turn back into the same face, edge or body (read-only).

    python run_job.py examples/selection.py

Select something in Fusion first (a face, an edge, a body, a component...), then run this.
A later script gets the very same entity back from its token:

    found = design().findEntityByToken(args["token"])
    if len(found) != 1:                       # gone (deleted), or split in two by a later feature
        raise RuntimeError("the selection changed: run selection.py again")
    face = found[0]

This is how you point at "this face" for Claude instead of describing it. The selection
belongs to the ACTIVE document, so this example does not follow --doc.
"""
sel = ui.activeSelections


def p_mm(p):
    return [round(p.x * 10, 3), round(p.y * 10, 3), round(p.z * 10, 3)]


def owner_of(e):
    body = try_value(lambda: e.body)
    occ = try_value(lambda: (body or e).assemblyContext)
    return {"body": try_value(lambda: body.name) if body else None,
            "occurrence": try_value(lambda: occ.fullPathName) if occ else None}


def describe(e):
    kind = e.objectType.split("::")[-1]
    info = {"type": kind, "token": try_value(lambda: e.entityToken)}
    face, edge = adsk.fusion.BRepFace.cast(e), adsk.fusion.BRepEdge.cast(e)
    body, occ = adsk.fusion.BRepBody.cast(e), adsk.fusion.Occurrence.cast(e)
    vertex = adsk.fusion.BRepVertex.cast(e)
    if face:
        ok, n = face.evaluator.getNormalAtPoint(face.pointOnFace)
        info.update(owner_of(face), surface=face.geometry.objectType.split("::")[-1],
                    area_mm2=round(face.area * 100, 3), centroid_mm=p_mm(face.centroid),
                    normal=[round(x, 4) for x in n.asArray()], edges=face.edges.count,
                    box_mm=bbox_mm(face))
    elif edge:
        info.update(owner_of(edge), curve=edge.geometry.objectType.split("::")[-1],
                    length_mm=round(edge.length * 10, 3),
                    ends_mm=[p_mm(edge.startVertex.geometry), p_mm(edge.endVertex.geometry)])
        circle = adsk.core.Circle3D.cast(edge.geometry) or adsk.core.Arc3D.cast(edge.geometry)
        if circle:
            info.update(radius_mm=round(circle.radius * 10, 4), centre_mm=p_mm(circle.center))
    elif body:
        info.update(name=body.name, occurrence=try_value(lambda: body.assemblyContext.fullPathName),
                    solid=body.isSolid, volume_mm3=try_value(lambda: round(body.volume * 1000, 3)),
                    box_mm=bbox_mm(body))
    elif occ:
        info.update(name=occ.fullPathName, component=occ.component.name, bodies=occ.bRepBodies.count,
                    box_mm=try_value(lambda: bbox_mm(occ)))
    elif vertex:
        info.update(owner_of(vertex), point_mm=p_mm(vertex.geometry))
    else:
        info["name"] = try_value(lambda: e.name)
    return info


out["document"] = app.activeDocument.name
out["selected"] = [describe(sel.item(i).entity) for i in range(sel.count)]
if not out["selected"]:
    print("Nothing is selected in %s. Select a face, edge, body or component in Fusion, then run this again."
          % out["document"])
else:
    print("%d selected in %s" % (len(out["selected"]), out["document"]))
