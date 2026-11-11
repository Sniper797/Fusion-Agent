"""Save a new version of a document, as Ctrl+S does, and wait until Fusion has finished (add-in 1.6.0).

    python run_job.py examples/save_document.py --doc "Box" --args "{\"description\": \"w = 50 mm\"}"
    python run_job.py examples/save_document.py --doc "Box" --args "{\"description\": \"...\", \"wait_upload\": false}"

Saving is the user's call: send this only when they asked for it, with their description.
save_document() refuses while a command is open in Fusion and for a document that was never saved
(the first save needs doc.saveAs(name, folder, description, "")). It waits for Fusion's documentSaved
event and, unless wait_upload is false, for the new version to reach the cloud, which a drawing needs
before it can update to it. Read out["save"]["version"]: it must be one more than "version_before".
"""
out["save"] = save_document(args["description"], timeout=args.get("timeout", 900),
                            wait_upload=args.get("wait_upload", True))
