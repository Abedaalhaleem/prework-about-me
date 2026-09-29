# data/

Local runtime data lives here and is git-ignored:

* `roomsense.sqlite3`: metadata (sessions, consents, calibrations, events, models)
* `recordings/`: bounded raw CSI captures (`*.jsonl.gz`), made only when you opt in with recorded consent
* `exports/`: zip exports you asked for
* `room.json`: the room geometry you entered (USER PROVIDED)
* `models/`: zone models, each tied to a hardware signature and room hash

Nothing in this folder is uploaded anywhere. Delete recordings in the UI (Recordings & Replay → Delete),
or with the server stopped: `cd backend && uv run roomsense recordings delete <id>`. Deleting a recording
also removes its exports and any zone model trained on it.
