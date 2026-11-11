"""Fill title-block fields of a drawing's ACTIVE sheet (add-in 1.6.0).

    python run_job.py examples/title_block.py --doc "Box Drawing" --args "{\"createdBy\": \"Mohammad Ghassan\"}"
    python run_job.py examples/title_block.py --doc "Box Drawing" --args "{\"approvedBy\": \"...\", \"dwgNo\": \"A-01\"}"

Fields: department, technicalRef, createdBy, createdDate, approvedBy, approvedDate, documentType,
documentStatus, title1, title2, title3, dwgNo, revision, issueDate, sheet.

The API has no setter for these values, so set_title_block() drives Fusion's own Edit Title Block
command with text commands, reads every field back, and commits only when all of them match.
Each sheet keeps its own values, and a job cannot make another sheet active: click the next
sheet's tab in Fusion, then run this again. The drawing is changed, not saved.
Never call drawing.sheets.applyTitleBlockToAllSheets() afterwards: it puts the defaults back.
"""
out["title_block"] = set_title_block(dict(args))
