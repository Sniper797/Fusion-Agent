"""Bring a drawing up to the latest version of its design, then export it to PDF (add-in 1.6.0).

    python run_job.py examples/drawing_pdf.py --doc "Box Drawing" --args "{\"pdf\": \"C:/out/box.pdf\"}"
    ... --args "{\"pdf\": \"C:/out/box.pdf\", \"sheets\": [1, 3]}"      one file with those sheets (1-based)
    ... --args "{\"pdf\": \"C:/out/box.pdf\", \"per_sheet\": true}"     box_01.pdf, box_02.pdf, ... one per sheet
    ... --args "{\"pdf\": \"C:/out/box.pdf\", \"update\": false}"       export as it is

The drawing must be open in Fusion (it need not be the active tab); pin it with --doc. Save the design
first (examples/save_document.py waits for the upload), or the update finds nothing newer.
`update` changes the drawing (its views follow the new version) and does not save it.
Measured on a one-sheet box drawing: update 7.9 s, export 0.4 s. Straight from a job,
exportManager.execute() never finished ("export timed out" after 600 s); export_drawing_pdf() runs
it inside a Fusion command instead.
"""
import os

if args.get("update", True):
    out["update"] = update_drawing(timeout=args.get("timeout", 600))
pdf = args["pdf"]
if args.get("per_sheet"):
    import adsk.drawing
    n = adsk.drawing.DrawingDocument.cast(doc).drawing.sheets.count
    want = args.get("sheets") or range(1, n + 1)
    stem, ext = os.path.splitext(pdf)
    out["files"] = []
    for k in want:
        r = export_drawing_pdf("%s_%02d%s" % (stem, k, ext), sheets=k)
        out["files"].append({"sheet": k, "ok": r["ok"], "file": r["file"], "bytes": r["bytes"],
                             "s": r["export_seconds"]})
        flush()
    out["ok"] = all(f["ok"] for f in out["files"])
else:
    out["export"] = export_drawing_pdf(pdf, sheets=args.get("sheets"))
    out["ok"] = out["export"]["ok"]
