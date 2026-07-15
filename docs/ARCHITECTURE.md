# Architecture

GreenNet Crisis is a compact crisis-coordination web application designed to run on one laptop, inside a local network, or in a container.

```text
Browser (HTML/CSS/JavaScript)
            │ JSON / session cookie
            ▼
Flask application
  ├─ authentication and capabilities
  ├─ feed, verification, tasks, schedules and audit log
  ├─ department channels and group chats (3+ participants)
  └─ NIMPH snapshot collector and cache
            │
            ├──────── SQLite + generated profile CSV
            └──────── NIMPH point API + /monitor HTML snapshot
```

## Main components

- `app.py` — Flask API, database schema, security rules, NIMPH parsing and static-file delivery.
- `static/index.html` — single-page interface with the Google/Gemini-inspired visual system.
- `static/brazil-states.svg` — Brazil state boundaries used by the sensor map.
- `greennet.db` — runtime SQLite database, generated locally and ignored by Git.
- `users.csv` — generated UTF-8 profile dataset without passwords or password hashes.

## Core invariants

- Passwords are stored only as Werkzeug hashes.
- Registration always creates the lowest-privilege observer role.
- Capabilities are checked by the server for every protected mutation.
- A group chat requires at least three members including its creator; private one-to-one rooms are rejected.
- Important mutations are written to the event log.
- NIMPH snapshots are bounded, cached and retained locally so the last useful state survives a temporary upstream failure.

## Data flow

1. The browser authenticates through a server session.
2. `/api/state` returns only the data and capabilities available to the current user.
3. Mutating endpoints validate role, content and request rate before writing to SQLite.
4. The sensor collector reads the station registry and the documented `data-*` attributes from the monitoring page.
5. The frontend renders sensor coordinates in WGS 84 on the Brazil map and opens a descriptive card for each station.
