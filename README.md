# IMS-SISL — Investor Management Dashboard

Small Flask + SQLAlchemy app that reads investor data from a Manager.io instance and shows:

- Investor list / dashboard
- Investment summary views
- Simple Gantt-style term visualisation

Authentication is a single admin login used to protect the dashboards.

## Configuration

All configuration is done via environment variables:

- `IMS_SECRET_KEY` — Flask session secret (required in production).
- `IMS_ADMIN_USERNAME` — login username (default: `admin`).
- `IMS_ADMIN_PASSWORD` — login password (required; no default).
- `IMS_ENV_LABEL` — short label shown on the login/header (e.g. `DEV`, `UAT`, `PROD`).
- `MANAGER_API_BASE_URL` — Manager.io API base URL (defaults to `https://esourcingbd.ap-southeast-1.manager.io/api2`).
- `MANAGER_API_KEY` — API key for Manager.io (no default, must be set).

Legacy environment variables still supported:

- `AIOSOL_API_BASE_URL` — alternative name for `MANAGER_API_BASE_URL`.
- `AIOSOL_API_KEY` — alternative name for `MANAGER_API_KEY`.

## Local development

From this folder:

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt

$env:IMS_ADMIN_USERNAME = "admin"
$env:IMS_ADMIN_PASSWORD = "changeme"
$env:FLASK_DEBUG = "1"
python app.py
```

Open `http://127.0.0.1:5000/login` and sign in.

## Deploying to Hostinger (overview)

The exact steps depend on whether you are using Hostinger’s Python app feature or a VPS. The high‑level flow is:

1. Push this project to a GitHub repository.
2. On Hostinger, create a Python application (or provision a VPS) and clone the GitHub repo into the app directory.
3. Create a virtual environment on the server and install dependencies:

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

4. Configure environment variables in the Hostinger panel (or via your shell), at minimum:

   - `IMS_SECRET_KEY`
   - `IMS_ADMIN_USERNAME`
   - `IMS_ADMIN_PASSWORD`
   - `MANAGER_API_BASE_URL`
   - `MANAGER_API_KEY`

5. Point your WSGI server to the `wsgi.py` entrypoint:

   ```python
   from app import app as application
   ```

   Most Hostinger Python setups let you specify `wsgi.py` as the entry script so that `application` is used by the server.

6. Run migrations or create the database schema once (if needed) using either:

   ```bash
   python create_db.py
   ```

   or your preferred Alembic/Flask-Migrate workflow.

7. Restart the application from the Hostinger control panel and browse to your configured domain, then log in at `/login`.

