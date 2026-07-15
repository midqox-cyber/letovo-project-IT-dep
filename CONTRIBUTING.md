# Contributing

## Local setup

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

The development server is available at `http://localhost:5000`.

## Before a pull request

```bash
python -m compileall -q app.py
python tests/test_smoke.py
python tests/test_sensor_parser.py
```

Keep pull requests focused and describe the user impact. Do not include runtime databases, CSV exports, passwords, tokens, `.env` files, private keys, logs, caches, or virtual environments.

## Code style

- Prefer small server-side validation functions and explicit API errors.
- Keep authorization checks on the server; frontend visibility is not an access-control boundary.
- Preserve the no-private-messages invariant for chat groups.
- NIMPH parsing changes must include a synthetic HTML parser test and must not depend on a live external service in CI.
- Keep interface text accessible and verify both desktop and narrow layouts.
