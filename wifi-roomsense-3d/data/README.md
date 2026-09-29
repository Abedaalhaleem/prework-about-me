# data/

Local runtime data lives here and is git-ignored:

* `roomsense.sqlite3`: metadata (sessions, consents, calibrations, events, models)
* `recordings/`: bounded raw CSI captures (`*.jsonl.gz`), made only when you opt in with recorded consent
* `exports/`: zip exports you asked for
* `room.json`: the room geometry you entered (USER PROVIDED)
* `models/`: zone models, each tied to a hardware signature and room hash

Nothing in this folder is uploaded anywhere. You can delete recordings from the UI
(Recordings → Delete) or with `roomsense recordings delete <id>`.
