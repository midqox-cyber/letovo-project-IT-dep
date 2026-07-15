# Security policy

## Supported version

Security fixes are applied to the current `main` branch.

## Reporting a vulnerability

Do not publish credentials, database copies, user CSV files, session keys, or exploit details in a public issue. Use a private GitHub Security Advisory for the repository owner and include:

- affected version or commit;
- minimal reproduction steps;
- expected impact;
- suggested mitigation, if known.

## Secret-handling rules

- Runtime data belongs outside the source tree through `GREENNET_DATA`.
- Passwords and integration keys are supplied through environment variables or locally generated ignored files.
- `greennet.db`, `users.csv`, `secret_key.txt`, `admin_password.txt`, `demo_password.txt`, `.env*`, private keys, and logs must never be committed.
- The generated CSV dataset deliberately excludes passwords and password hashes.
- `GREENNET_TRUST_PROXY=1` is safe only behind a trusted reverse proxy that overwrites forwarding headers.
- Demo mode is for local presentations and must not be enabled on a public deployment.

If a secret is committed accidentally, remove it from Git history and rotate it immediately. Deleting only the latest file is not sufficient.
