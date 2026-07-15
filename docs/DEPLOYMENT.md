# Deployment guide

## Local laptop

On Windows, run `start-local.bat` or execute:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

Open `http://localhost:5000`. The application listens on all interfaces, so other devices on the same trusted network can use `http://<laptop-ip>:5000` while the server is running and the firewall permits TCP port 5000.

## Docker

```bash
docker compose up --build -d
docker compose logs -f greennet
```

Runtime data is stored in the named `greennet-data` volume. Back up that volume before upgrades.

## Configuration

| Variable | Purpose |
|---|---|
| `GREENNET_DATA` | Directory for SQLite, generated CSV and local secret files |
| `GREENNET_ADMIN_USER` | Initial privileged username |
| `GREENNET_ADMIN_PASSWORD` | Initial privileged password; omit to generate locally |
| `GREENNET_DEMO` | Enables the prefilled local presentation scenario |
| `GREENNET_DEMO_PASSWORD` | Local password for demonstration profiles |
| `GREENNET_REGISTRATION` | Enables or disables self-registration |
| `GREENNET_NIMPH_URL` | Upstream NIMPH base URL |
| `GREENNET_HTTPS` | Adds the `Secure` flag to the session cookie |
| `GREENNET_TRUST_PROXY` | Trusts forwarding headers only behind a controlled proxy |
| `GREENNET_AUTH_MODE` | Selects local or portal-backed authentication |
| `GREENNET_PORTAL_URL` | Portal base URL for server-to-server integration |
| `GREENNET_PORTAL_KEY` | Portal integration key; never commit it |

## Internet exposure

The built-in Flask server is for development and trusted local demonstrations. For an internet deployment, use a production WSGI server behind a reverse proxy with HTTPS, request-size limits, security headers, restricted network access, monitoring and backups.

Before going public:

1. keep runtime data outside the source tree;
2. provide strong secrets through the hosting platform;
3. leave demo mode disabled;
4. enable secure cookies behind HTTPS;
5. configure a trusted reverse proxy;
6. test restore from a database backup;
7. review the guidance in `SECURITY.md`.
