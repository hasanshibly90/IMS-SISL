# IMS-SISL – Investor Management Dashboard

Small Flask + SQLAlchemy app that reads investor data from a Manager.io instance and shows:

- Investor list / dashboard
- Investment summary views
- Simple Gantt-style term visualisation

Authentication is a single admin login used to protect the dashboards.

## Configuration

All configuration is done via environment variables:

- `IMS_SECRET_KEY` – Flask session secret (required in production).
- `IMS_ADMIN_USERNAME` – login username (default: `admin`).
- `IMS_ADMIN_PASSWORD` – login password (required; no default).
- `IMS_ENV_LABEL` – short label shown on the login/header (e.g. `DEV`, `UAT`, `PROD`).
- `MANAGER_API_BASE_URL` – Manager.io API base URL (defaults to `https://esourcingbd.ap-southeast-1.manager.io/api2`).
- `MANAGER_API_KEY` – API key for Manager.io (no default, must be set).

Legacy environment variables still supported:

- `AIOSOL_API_BASE_URL` – alternative name for `MANAGER_API_BASE_URL`.
- `AIOSOL_API_KEY` – alternative name for `MANAGER_API_KEY`.

## Local development

From this folder:

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt  # if you keep a requirements file

$env:IMS_ADMIN_USERNAME = "admin"
$env:IMS_ADMIN_PASSWORD = "changeme"
$env:FLASK_DEBUG = "1"
python app.py
```

Open `http://127.0.0.1:5000/login` and sign in.

