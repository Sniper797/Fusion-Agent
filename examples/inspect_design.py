"""Read-only overview of the open design: components, bodies, joints, parameters.

    python run_job.py examples/inspect_design.py
"""
d = design()
root = d.rootComponent
parametric = d.designType == adsk.fusion.DesignTypes.ParametricDesignType


def body_info(b):
    return {"name": b.name, "solid": b.isSolid, "visible": b.isVisible,
            "volume_cm3": try_value(lambda: round(b.volume, 3)),
            "bbox_mm": try_value(lambda: bbox_mm(b)),
            "material": try_value(lambda: b.material.name)}


out["document"] = doc.name                  # `doc` follows --doc (add-in 1.1.0+)
out["units"] = d.unitsManager.defaultLengthUnits
out["design_type"] = "parametric" if parametric else "direct"
if parametric:
    out["timeline"] = {"items": d.timeline.count, "marker": d.timeline.markerPosition}
out["root_bodies"] = [body_info(b) for b in root.bRepBodies]
out["occurrences"] = [{"name": o.fullPathName, "component": o.component.name,
                       "visible": o.isLightBulbOn,
                       "bodies": [body_info(b) for b in o.bRepBodies]}
                      for o in root.allOccurrences]
# Joints per component: root.allJoints returned 0 of 111 joints on a direct design.
out["joints"] = [{"name": j.name, "component": comp.name,
                  "motion": try_value(lambda: j.jointMotion.objectType.split("::")[-1]),
                  "healthy": try_value(lambda: j.healthState ==
                                       adsk.fusion.FeatureHealthStates.HealthyFeatureHealthState)}
                 for comp in d.allComponents for j in comp.joints]
if parametric:                               # direct designs have no parameters
    out["user_parameters"] = [{"name": p.name, "expression": p.expression} for p in d.userParameters]

all_bodies = out["root_bodies"] + [b for o in out["occurrences"] for b in o["bodies"]]
unmeasurable = [b["name"] for b in all_bodies if b["volume_cm3"] is None]
print("%s: %d occurrences, %d bodies, %d joints"
      % (out["document"], len(out["occurrences"]), len(all_bodies), len(out["joints"])))
if unmeasurable:
    print("volume not computable for %d bodies (open surfaces or damaged geometry): %s"
          % (len(unmeasurable), ", ".join(unmeasurable[:10])))
