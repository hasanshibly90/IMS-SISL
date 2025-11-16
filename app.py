from flask import Flask, render_template, jsonify, redirect, url_for, request, session
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func
from datetime import datetime
from threading import Lock
import requests
import os
import json

import plotly.graph_objs as go
from plotly.utils import PlotlyJSONEncoder

app = Flask(__name__)
app.secret_key = os.environ.get("IMS_SECRET_KEY", "change-me-in-production")
# Database configuration (SQLite for now)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///investors.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Synchronization control (to avoid concurrent DB writes / locks)
db_update_lock = Lock()
last_update_time = None  # UTC datetime of last successful sync
UPDATE_INTERVAL_SECONDS = 300  # only refresh from Manager.io at most every 5 minutes
DETAIL_DEBUG_COUNT = 0  # limit verbose logging for detail calls

# In-memory cache for investment summary so we don't hit
# Manager.io APIs on every /investment_summary request.
summary_cache = None
summary_last_update = None  # UTC datetime of last summary build

# External API configuration (Manager.io adapter)
# Defaults point to SISL's Manager.io endpoint; can be overridden by env vars.
API_BASE_URL = os.environ.get("AIOSOL_API_BASE_URL", "https://esourcingbd.ap-southeast-1.manager.io/api2")
API_KEY = os.environ.get(
    "AIOSOL_API_KEY",
    "Ch5TTUFSVCBJTkRVU1RSSUFMIFNPTFVUSU9OIExURC4SEgnyKhJxeaxVRhGtOA2alblJKBoSCQKFGqhLRrVBEZAgv0uBOk6W",
)
API_TIMEOUT_SECONDS = 10

# Simple admin login (for protecting the dashboard)
ADMIN_USERNAME = os.environ.get("IMS_ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("IMS_ADMIN_PASSWORD")

# Custom field IDs for SISL investor terms
NEW_START_ID = "826be8ff-63ab-4773-a616-c322ff84063e"
NEW_END_ID = "6e7981f8-d83f-44b8-beac-55c0acd7592c"
NEW_PROFIT_ID = "5862bbaa-82ea-4094-a2a4-7fc6a77ebac4"

# Legacy IDs kept for backward compatibility
OLD_START_ID = "f30ea2f8-02af-4e5e-b9ec-b8c7ef2d12e2"
OLD_END_ID = "c4b22208-6d56-4c34-870c-c5f40954526f"
OLD_PROFIT_ID = "1e1a26a2-b4a5-4c89-b259-368ec797177e"

# ---------------------------
# Investor Model (with dividend_paid field)
# ---------------------------
class Investor(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    start_date = db.Column(db.String(10), nullable=True)
    end_date = db.Column(db.String(10), nullable=True)
    duration_months = db.Column(db.Integer, nullable=True)
    remaining_months = db.Column(db.Integer, nullable=True)
    profit_percentage = db.Column(db.Float, nullable=True)
    balance = db.Column(db.Float, nullable=False)
    monthly_profit = db.Column(db.Float, nullable=True)
    profit_payable_up_to_now = db.Column(db.Float, default=0)
    profit_paid = db.Column(db.Float, default=0)   # existing computed field (if needed)
    profit_due = db.Column(db.Float, default=0)      # computed as: profit_payable_up_to_now - dividend_paid
    dividend_paid = db.Column(db.Float, default=0)   # new field from payment-lines API

# ---------------------------
# Fetching Functions
# ---------------------------
def _api_headers(include_accept_json: bool = True) -> dict:
    headers = {}
    if include_accept_json:
        headers["Accept"] = "application/json"
    if API_KEY:
        headers["X-API-KEY"] = API_KEY
    return headers


def fetch_special_accounts():
    url = f"{API_BASE_URL}/special-accounts"
    try:
        # Use a large pageSize to ensure we fetch all special accounts,
        # not just the first page.
        response = requests.get(
            url,
            headers=_api_headers(),
            params={"pageSize": 9999},
            timeout=API_TIMEOUT_SECONDS,
        )
        if response.status_code == 200:
            return response.json().get("specialAccounts", [])
        else:
            print(f"[AIOSOL] special-accounts HTTP {response.status_code}: {response.text[:500]}")
    except requests.RequestException as exc:
        print(f"[AIOSOL] Error fetching special accounts: {exc}")
    return []

def fetch_investor_details(key):
    url = f"{API_BASE_URL}/special-account-form/{key}"
    try:
        response = requests.get(url, headers=_api_headers(include_accept_json=False), timeout=API_TIMEOUT_SECONDS)
        if response.status_code == 200:
            data = response.json()
            cf = data.get("CustomFields2") or data.get("CustomFields") or {}
            dates = cf.get("Dates", {})
            decimals = cf.get("Decimals", {})

            # Debug: log a few samples so we can verify field IDs
            global DETAIL_DEBUG_COUNT
            if DETAIL_DEBUG_COUNT < 5:
                DETAIL_DEBUG_COUNT += 1
                try:
                    print(
                        f"[DETAIL] key={key}, dates_keys={list(dates.keys())}, "
                        f"decimals_keys={list(decimals.keys())}"
                    )
                except Exception:
                    pass

            raw_start = dates.get(NEW_START_ID) or dates.get(OLD_START_ID, "")
            raw_end = dates.get(NEW_END_ID) or dates.get(OLD_END_ID, "")

            start_date = str(raw_start).split("T")[0].strip() if raw_start else ""
            end_date = str(raw_end).split("T")[0].strip() if raw_end else ""

            profit_percentage = decimals.get(NEW_PROFIT_ID)
            if profit_percentage is None:
                profit_percentage = decimals.get(OLD_PROFIT_ID, 0)

            return {
                "start_date": start_date,
                "end_date": end_date,
                "profit_percentage": profit_percentage
            }
        else:
            print(f"[AIOSOL] special-account-form HTTP {response.status_code} for key={key}: {response.text[:500]}")
    except requests.RequestException as exc:
        print(f"[AIOSOL] Error fetching investor details for key={key}: {exc}")
    return {"start_date": "", "end_date": "", "profit_percentage": 0}

def fetch_payment_lines():
    """
    Fetch dividend paid amounts from the payment-lines API.
    Returns a list of payment line records.
    """
    url = f"{API_BASE_URL}/payment-lines"
    try:
        response = requests.get(
            url,
            headers=_api_headers(),
            params={"pageSize": 9999},
            timeout=API_TIMEOUT_SECONDS,
        )
        if response.status_code == 200:
            data = response.json()
            if isinstance(data, list):
                return data
            elif isinstance(data, dict):
                return data.get("paymentLines", [])
        else:
            print(f"[AIOSOL] payment-lines HTTP {response.status_code}: {response.text[:500]}")
    except requests.RequestException as exc:
        print(f"[AIOSOL] Error fetching payment lines: {exc}")
    return []


def fetch_receipt_lines():
    """
    Fetch investment receipts from the receipt-lines API.
    Returns a list of receipt line records.
    """
    url = f"{API_BASE_URL}/receipt-lines"
    try:
        response = requests.get(
            url,
            headers=_api_headers(),
            params={"pageSize": 9999},
            timeout=API_TIMEOUT_SECONDS,
        )
        if response.status_code == 200:
            data = response.json()
            if isinstance(data, list):
                return data
            elif isinstance(data, dict):
                return data.get("receiptLines", [])
        else:
            print(f"[AIOSOL] receipt-lines HTTP {response.status_code}: {response.text[:500]}")
    except requests.RequestException as exc:
        print(f"[AIOSOL] Error fetching receipt lines: {exc}")
    return []


def fetch_journal_entry_lines():
    """
    Fetch journal entry lines (used for adjustments that hit Loans payable
    or Profit payable directly, outside of receipts/payments).
    """
    url = f"{API_BASE_URL}/journal-entry-lines"
    try:
        response = requests.get(
            url,
            headers=_api_headers(),
            params={"pageSize": 9999},
            timeout=API_TIMEOUT_SECONDS,
        )
        if response.status_code == 200:
            data = response.json()
            if isinstance(data, list):
                return data
            elif isinstance(data, dict):
                return data.get("journalEntryLines", [])
        else:
            print(f"[AIOSOL] journal-entry-lines HTTP {response.status_code}: {response.text[:500]}")
    except requests.RequestException as exc:
        print(f"[AIOSOL] Error fetching journal entry lines: {exc}")
    return []

# ---------------------------
# Helper Functions
# ---------------------------
def extract_balance_amount(entry):
    """
    Extracts the numeric balance for a special account entry.
    For liability-type accounts like 'Loans payable', Manager.io
    typically exposes 'credit'/'debit' and a signed 'value'.
    We prefer 'credit' when present, otherwise fall back to abs(value).
    """
    bal = entry.get("balance", {})
    if isinstance(bal, dict):
        credit = bal.get("credit")
        value = bal.get("value", 0)
        try:
            if isinstance(credit, (int, float)) and credit is not None:
                return float(credit)
            return abs(float(value))
        except (TypeError, ValueError):
            return 0.0
    try:
        return abs(float(bal))
    except (TypeError, ValueError):
        return 0.0


def parse_date(date_str):
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return None

def calculate_months_difference(start_dt, end_dt):
    if not start_dt or not end_dt:
        return None
    return (end_dt.year - start_dt.year) * 12 + (end_dt.month - start_dt.month)

def calculate_remaining_months(end_dt):
    if not end_dt:
        return 0
    today = datetime.today()
    return max(0, (end_dt.year - today.year) * 12 + (end_dt.month - today.month))

def calculate_elapsed_months(start_dt):
    if not start_dt:
        return 0
    today = datetime.today()
    return max(0, (today.year - start_dt.year) * 12 + (today.month - start_dt.month))

def ensure_correct_dates(start_date, end_date):
    if not start_date or not end_date:
        return start_date, end_date
    start_dt = parse_date(start_date)
    end_dt = parse_date(end_date)
    if start_dt and end_dt and start_dt > end_dt:
        return end_date, start_date
    return start_date, end_date

def calculate_monthly_profit(balance, profit_percentage):
    if not profit_percentage:
        return 0
    return round((balance * profit_percentage) / 100 / 12, 2)


def extract_investor_terms_from_entry(entry):
    """
    Try to extract start_date, end_date and profit_percentage
    directly from a special-accounts entry's custom fields.
    """
    cf = entry.get("CustomFields2") or entry.get("customFields2") or entry.get("CustomFields") or {}
    dates = cf.get("Dates", {})
    decimals = cf.get("Decimals", {})

    raw_start = dates.get(NEW_START_ID) or dates.get(OLD_START_ID)
    raw_end = dates.get(NEW_END_ID) or dates.get(OLD_END_ID)

    start_date = str(raw_start).split("T")[0].strip() if raw_start else ""
    end_date = str(raw_end).split("T")[0].strip() if raw_end else ""

    profit_percentage = decimals.get(NEW_PROFIT_ID)
    if profit_percentage is None:
        profit_percentage = decimals.get(OLD_PROFIT_ID)

    return {
        "start_date": start_date or "",
        "end_date": end_date or "",
        "profit_percentage": profit_percentage if profit_percentage is not None else 0,
    }


def normalize_investor_name(name: str) -> str:
    """
    Normalize investor name for grouping.
    - Trim whitespace
    - Strip phase / variant suffixes in parentheses, e.g. 'Md X (P2)' -> 'Md X'
    """
    if not name:
        return ""
    base = name.strip()
    idx = base.find(" (")
    if idx != -1:
        base = base[:idx].strip()
    return base


def split_investor_variant(raw_name: str):
    """
    Split a raw investor name (which may contain a numeric code and phase
    in parentheses) into:
      - base_name: normalized investor name (no code, no phase)
      - phase_label: text inside the last parentheses, or "Base"
      - display_name: cleaned display name without leading code

    Examples:
      "9995 - Ashique Hossain Turzo"          -> ("Ashique Hossain Turzo", "Base", "Ashique Hossain Turzo")
      "9993 - Md. Ashraful Islam Rajib (P2)"  -> ("Md. Ashraful Islam Rajib", "P2", "Md. Ashraful Islam Rajib (P2)")
    """
    if not raw_name:
        return "", "", ""

    display = raw_name.strip()
    # Drop leading code like "9995 - "
    if " - " in display:
        display = display.split(" - ", 1)[1].strip()

    # Phase label from trailing parentheses, if present
    phase_label = "Base"
    idx = display.rfind("(")
    if idx != -1 and display.endswith(")"):
        phase_label = display[idx + 1 : -1].strip() or "Base"
        base = display[:idx].strip()
    else:
        base = display

    base_name = normalize_investor_name(base)
    return base_name, phase_label, display


def group_investors_for_dashboard(investors):
    """
    Group Investor rows by base investor name (ignoring numeric codes and
    phase suffixes in parentheses) so the dashboard shows one row per
    investor, with aggregated balances and dates.
    """
    groups = {}

    for inv in investors:
        base_name, phase_label, display_name = split_investor_variant(inv.name)
        key = base_name or inv.name or "Unknown"

        g = groups.get(key)
        if not g:
            g = groups[key] = {
                "name": key,
                "start_date": None,
                "end_date": None,
                "duration_months": None,
                "remaining_months": 0,
                "profit_percentage": 0.0,
                "balance": 0.0,
                "monthly_profit": 0.0,
            }

        # Aggregate numeric fields
        g["balance"] += inv.balance or 0.0
        g["monthly_profit"] += inv.monthly_profit or 0.0

        # Track earliest start date and latest end date
        if inv.start_date:
            if not g["start_date"] or inv.start_date < g["start_date"]:
                g["start_date"] = inv.start_date
        if inv.end_date:
            if not g["end_date"] or inv.end_date > g["end_date"]:
                g["end_date"] = inv.end_date

        # For profit percentage, prefer non-zero values; if multiple
        # different values exist we leave the last non-zero value.
        if inv.profit_percentage:
            g["profit_percentage"] = inv.profit_percentage

    # Derive duration and remaining months per group using aggregated dates
    for g in groups.values():
        start_date = g["start_date"]
        end_date = g["end_date"]

        if start_date and end_date:
            start_dt = parse_date(start_date)
            end_dt = parse_date(end_date)
            g["duration_months"] = calculate_months_difference(start_dt, end_dt)
            g["remaining_months"] = calculate_remaining_months(end_dt)
        else:
            g["duration_months"] = None
            g["remaining_months"] = 0

    # Sort groups by investor name for stable display
    return sorted(groups.values(), key=lambda x: x["name"])


def _parse_investor_name_from_account(account_str: str, expected_prefix: str):
    """
    Given an account string like 'Loans payable — Name' or 'Profit payable - Name',
    return the investor name part when the prefix matches.
    """
    if not account_str:
        return None
    lower = account_str.lower().strip()
    if not lower.startswith(expected_prefix.lower()):
        return None

    # Support different separator characters used by Manager:
    # hyphen-minus '-', en dash '–', em dash '—'.
    for sep in ["—", "–", "-"]:
        idx = account_str.rfind(sep)
        if idx != -1:
            name = account_str[idx + 1 :].strip()
            if name:
                return name
    return None

def format_currency(value):
    try:
        return "{:,.2f}".format(float(value)) if value else "0.00"
    except (ValueError, TypeError):
        return "0.00"

# ---------------------------
# Main Update Logic
# ---------------------------
def update_database(force: bool = False):
    """
    Pull fresh data from Manager.io APIs and refresh the Investor table.
    Runs at most once every UPDATE_INTERVAL_SECONDS unless force=True.
    Wrapped in a process-wide lock to avoid concurrent SQLite writes.
    """
    global last_update_time

    with db_update_lock:
        if not force and last_update_time is not None:
            elapsed = (datetime.utcnow() - last_update_time).total_seconds()
            if elapsed < UPDATE_INTERVAL_SECONDS:
                return

        accounts_data = fetch_special_accounts()
        payment_lines = fetch_payment_lines()
        journal_lines = fetch_journal_entry_lines()

        # If the API call failed or returned nothing, don't wipe existing data
        if not accounts_data:
            print("[SYNC] No special-accounts data received; skipping DB refresh.")
            return

        total_accounts = len(accounts_data)
        print(f"[SYNC] Received {total_accounts} special-accounts records.")

        db.session.query(Investor).delete()  # Clear old records

        # Gather "Profit Payable" amounts by investor name from special accounts
        profit_payable_data = {}
        profit_payable_count = 0
        for entry in accounts_data:
            if entry.get("controlAccount") == "Profit Payable":
                name = entry.get("name", "")
                payable_value = extract_balance_amount(entry)
                profit_payable_data[name] = payable_value
                profit_payable_count += 1

        # Aggregate profit paid amounts from payment-lines API.
        # We treat lines posted to either "Dividend payable — Name"
        # or "Profit payable — Name" as profit distributions.
        dividend_paid_data = {}
        for line in payment_lines:
            if not isinstance(line, dict):
                continue
            account_str = (line.get("account") or "").strip()
            investor_name = (
                _parse_investor_name_from_account(account_str, "Dividend payable")
                or _parse_investor_name_from_account(account_str, "Profit payable")
            )
            if investor_name:
                amount = abs(line.get("amount", {}).get("value", 0))
                dividend_paid_data[investor_name] = dividend_paid_data.get(investor_name, 0) + amount

        # Journal-entry-lines: debits to Profit/Dividend payable reduce the liability
        # and should be treated as profit distributions.
        for line in journal_lines:
            if not isinstance(line, dict):
                continue
            account_str = (line.get("account") or "").strip()
            investor_name = (
                _parse_investor_name_from_account(account_str, "Profit payable")
                or _parse_investor_name_from_account(account_str, "Dividend payable")
            )
            if not investor_name:
                continue
            debit = line.get("debit") or {}
            amount = abs(debit.get("value", 0)) if isinstance(debit, dict) else 0
            if amount:
                dividend_paid_data[investor_name] = dividend_paid_data.get(investor_name, 0) + amount

        # Process investor "Loans payable" accounts
        loans_payable_count = 0
        for entry in accounts_data:
            if entry.get("controlAccount") != "Loans payable":
                continue

            name = entry.get("name", "")
            balance = extract_balance_amount(entry)
            # Skip investors whose current balance is zero
            if not balance:
                continue
            loans_payable_count += 1

            # First, try to get Start/End/Profit from the special-accounts entry itself.
            terms_from_entry = extract_investor_terms_from_entry(entry)

            start_date = terms_from_entry["start_date"]
            end_date = terms_from_entry["end_date"]
            profit_percentage = terms_from_entry["profit_percentage"]

            # If any of the key fields are missing, fall back to the
            # more detailed special-account-form/{key} endpoint.
            if not start_date or not end_date or not profit_percentage:
                key = entry.get("key", "") or entry.get("Key", "")
                if key:
                    details = fetch_investor_details(key)
                    start_date = start_date or details.get("start_date", "")
                    end_date = end_date or details.get("end_date", "")
                    profit_percentage = profit_percentage or details.get("profit_percentage", 0)

            start_date, end_date = ensure_correct_dates(start_date, end_date)
            start_dt = parse_date(start_date)
            end_dt = parse_date(end_date)

            duration_months = calculate_months_difference(start_dt, end_dt)
            remaining_months = calculate_remaining_months(end_dt)
            monthly_profit = calculate_monthly_profit(balance, profit_percentage or 0)

            elapsed_months = calculate_elapsed_months(start_dt)
            total_profit_payable = elapsed_months * (monthly_profit or 0)

            # Get dividend paid from payment-lines aggregation
            dividend_paid_value = dividend_paid_data.get(name, 0)
            # For backward compatibility, also assign profit_paid from special accounts if needed
            profit_paid_value = profit_payable_data.get(name, 0)
            # Current Payable = total profit payable minus dividend paid
            current_payable = max(0, total_profit_payable - dividend_paid_value)

            investor = Investor(
                name=name,
                start_date=start_date,
                end_date=end_date,
                duration_months=duration_months,
                remaining_months=remaining_months,
                profit_percentage=profit_percentage,
                balance=balance,
                monthly_profit=monthly_profit,
                profit_payable_up_to_now=total_profit_payable,
                profit_paid=profit_paid_value,
                profit_due=current_payable,
                dividend_paid=dividend_paid_value
            )
            db.session.add(investor)

        db.session.commit()
        print(f"[SYNC] Profit Payable entries: {profit_payable_count}, Loans payable entries: {loans_payable_count}")
        last_update_time = datetime.utcnow()

@app.before_request
def before_request():
    # Skip automatic sync for the explicit /sync endpoint
    if request.endpoint == 'sync':
        return

    # Only auto-sync once when there is no data yet.
    global last_update_time
    if last_update_time is None and Investor.query.count() == 0:
        update_database(force=True)

    # --- Authentication guard ---
    # Allow unauthenticated access to the login page, health check and static assets.
    open_endpoints = {'login', 'healthcheck'}
    endpoint = request.endpoint or ''
    if endpoint in open_endpoints or endpoint.startswith('static'):
        return

    # If no admin password is configured, do not allow login at all.
    # (You will configure IMS_ADMIN_PASSWORD in the systemd service.)
    if not ADMIN_PASSWORD:
        return redirect(url_for('login'))

    if not session.get('logged_in'):
        return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if session.get('logged_in'):
        return redirect(url_for('home'))

    error = None
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''

        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session['logged_in'] = True
            return redirect(url_for('home'))
        else:
            error = "Invalid username or password"

    return render_template('login.html', error=error)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/healthz')
def healthcheck():
    return "ok", 200

# ---------------------------
# Home Route (Table View)
# ---------------------------
@app.route('/')
def home():
    search_query = (request.args.get("q") or "").strip()
    search_lower = search_query.lower()

    raw_investors = Investor.query.order_by(Investor.name).all()

    # Group investors by base name but keep per-phase rows.
    groups = {}
    for inv in raw_investors:
        base_name, phase_label, display_name = split_investor_variant(inv.name)
        key = base_name or inv.name or "Unknown"

        g = groups.get(key)
        if not g:
            g = groups[key] = {
                "name": key,
                "start_date": None,
                "end_date": None,
                "duration_months": None,
                "remaining_months": 0,
                "profit_percentage": 0.0,
                "balance": 0.0,
                "monthly_profit": 0.0,
                "members": [],
            }
        g["members"].append(inv)

        g["balance"] += inv.balance or 0.0
        g["monthly_profit"] += inv.monthly_profit or 0.0

        if inv.start_date:
            if not g["start_date"] or inv.start_date < g["start_date"]:
                g["start_date"] = inv.start_date
        if inv.end_date:
            if not g["end_date"] or inv.end_date > g["end_date"]:
                g["end_date"] = inv.end_date

        if inv.profit_percentage:
            g["profit_percentage"] = inv.profit_percentage

    # Derive duration and remaining months per group using aggregated dates
    for g in groups.values():
        start_date = g["start_date"]
        end_date = g["end_date"]
        if start_date and end_date:
            start_dt = parse_date(start_date)
            end_dt = parse_date(end_date)
            g["duration_months"] = calculate_months_difference(start_dt, end_dt)
        g["remaining_months"] = calculate_remaining_months(end_dt)
    else:
        g["duration_months"] = None
        g["remaining_months"] = 0

    # Optional filtering by investor name (group or member)
    if search_query:
        filtered_groups = {}
        for key, g in groups.items():
            key_match = search_lower in (key or "").lower()
            member_match = any(
                search_lower in (m.name or "").lower()
                for m in g["members"]
            )
            if key_match or member_match:
                filtered_groups[key] = g
    else:
        filtered_groups = groups

    # Build table rows: phases followed by a group total row.
    # Order groups by total balance (largest to smallest),
    # and within each group order phases by balance as well.
    table_rows = []
    for key, g in sorted(filtered_groups.items(), key=lambda item: (item[1]["balance"] or 0), reverse=True):
        for inv in sorted(g["members"], key=lambda inv: (inv.balance or 0), reverse=True):
            table_rows.append({
                "kind": "phase",
                "name": inv.name,
                "start_date": inv.start_date,
                "end_date": inv.end_date,
                "duration_months": inv.duration_months,
                "remaining_months": inv.remaining_months,
                "profit_percentage": inv.profit_percentage,
                "monthly_profit": inv.monthly_profit,
                "balance": inv.balance,
            })
        table_rows.append({
            "kind": "total",
            "name": f"{key} (Total)",
            "start_date": g["start_date"],
            "end_date": g["end_date"],
            "duration_months": g["duration_months"],
            "remaining_months": g["remaining_months"],
            "profit_percentage": g["profit_percentage"],
            "monthly_profit": g["monthly_profit"],
            "balance": g["balance"],
        })

    # Build Plotly bar chart (horizontal) using Python,
    # aggregated by investor group total balance (respecting any filter).
    bar_pairs = sorted(
        ((g["name"], g["balance"]) for g in filtered_groups.values()),
        key=lambda x: x[1] or 0,
        reverse=True,
    )
    sorted_labels = [name for name, _ in bar_pairs]
    sorted_balances = [bal for _, bal in bar_pairs]

    bar_fig = go.Figure(
        data=[
            go.Bar(
                x=sorted_balances,
                y=sorted_labels,
                orientation="h",
                marker=dict(
                    color="rgba(59, 130, 246, 0.85)",
                    line=dict(color="rgba(37, 99, 235, 1)", width=1.2),
                ),
                hovertemplate="%{y}<br>Tk %{x:,.0f}<extra></extra>",
            )
        ]
    )
    bar_fig.update_layout(
        height=420,
        margin=dict(l=220, r=40, t=40, b=40),
        xaxis=dict(
            title="Balance Amount (Tk)",
            tickprefix="Tk ",
            separatethousands=True,
            gridcolor="rgba(148, 163, 184, 0.3)",
            zerolinecolor="rgba(148, 163, 184, 0.5)",
        ),
        yaxis=dict(automargin=True),
        showlegend=False,
        plot_bgcolor="#ffffff",
        paper_bgcolor="#f9fafb",
        title=dict(text="Investor's Investment Distribution", x=0.5),
    )
    bar_chart_json = json.dumps(bar_fig, cls=PlotlyJSONEncoder)

    total_monthly_profit = db.session.query(func.sum(Investor.monthly_profit)).scalar() or 0
    total_balance = db.session.query(func.sum(Investor.balance)).scalar() or 0
    total_profit_payable_up_to_now = db.session.query(func.sum(Investor.profit_payable_up_to_now)).scalar() or 0
    total_dividend_paid = db.session.query(func.sum(Investor.dividend_paid)).scalar() or 0
    total_current_payable = db.session.query(func.sum(Investor.profit_due)).scalar() or 0

    avg_profit_percentage = 0
    if groups:
        sum_percentage = sum(g["profit_percentage"] or 0 for g in groups.values())
        avg_profit_percentage = sum_percentage / len(groups)

    # Compute the custom Profit %:
    # (Total Monthly Profit * 12 * 100) / Total Balance Amount
    computed_profit_percentage = 0
    if total_balance > 0:
        computed_profit_percentage = (total_monthly_profit * 12 * 100) / total_balance

    return render_template(
        'task.html',
        investors=table_rows,
        format_currency=format_currency,
        total_monthly_profit=total_monthly_profit,
        total_balance=total_balance,
        total_profit_payable_up_to_now=total_profit_payable_up_to_now,
        total_dividend_paid=total_dividend_paid,
        total_current_payable=total_current_payable,
        avg_profit_percentage=avg_profit_percentage,
        computed_profit_percentage=computed_profit_percentage,
        last_update_time=last_update_time,
        bar_chart_json=bar_chart_json,
        search_query=search_query,
    )


@app.route('/sync')
def sync():
    update_database(force=True)
    return redirect(url_for('home'))


# Legacy summary (no longer exposed via a route)
def investment_summary_legacy():
    """
    Summarize, per investor, how much principal we have received
    (receipt-lines posted to 'Loans payable — Name'), how much
    principal has been repaid (payment-lines to 'Loans payable — Name'),
    how much profit has been paid (payment-lines to 'Profit payable' /
    'Dividend payable'), and compare the computed principal balance
    with the current Loans payable balance.
    """
    global summary_cache, summary_last_update

    # Serve cached summary if it's still fresh and no explicit refresh is requested.
    if summary_cache is not None and summary_last_update is not None:
        age = (datetime.utcnow() - summary_last_update).total_seconds()
        if age < UPDATE_INTERVAL_SECONDS and not request.args.get("refresh"):
            return render_template(
                "investment_summary.html",
                investors=summary_cache["investors"],
                totals=summary_cache["totals"],
                format_currency=format_currency,
            )

    accounts_data = fetch_special_accounts()
    receipt_lines = fetch_receipt_lines()
    payment_lines = fetch_payment_lines()
    journal_lines = fetch_journal_entry_lines()

    # Seed investors from Loans payable special accounts
    summary = {}
    for entry in accounts_data:
        if entry.get("controlAccount") != "Loans payable":
            continue
        raw_name = entry.get("name", "")
        if not raw_name:
            continue
        base_name = normalize_investor_name(raw_name)
        current_balance = extract_balance_amount(entry)
        info = summary.get(base_name)
        if not info:
            info = summary[base_name] = {
                "name": base_name,
                "current_balance": 0.0,
                "total_received": 0.0,
                "first_receipt_date": None,
                "last_receipt_date": None,
                "principal_repaid": 0.0,
                "profit_paid": 0.0,
            }
        info["current_balance"] += current_balance

    # Helper to ensure an investor entry exists even if there is
    # a receipt/payment but no Loans payable account found.
    def ensure_investor(raw_name: str):
        base = normalize_investor_name(raw_name)
        if base not in summary:
            summary[base] = {
                "name": base,
                "current_balance": 0.0,
                "total_received": 0.0,
                "first_receipt_date": None,
                "last_receipt_date": None,
                "principal_repaid": 0.0,
                "profit_paid": 0.0,
            }
        return summary[base]

    # Aggregate receipts into Loans payable accounts
    for line in receipt_lines:
        if not isinstance(line, dict):
            continue
        account_str = (line.get("account") or "").strip()
        investor_name = _parse_investor_name_from_account(account_str, "Loans payable")
        if not investor_name:
            continue
        amount = abs(line.get("amount", {}).get("value", 0))
        info = ensure_investor(investor_name)
        info["total_received"] += amount

        date_str = line.get("date")
        date_dt = parse_date(date_str) if date_str else None
        if date_dt:
            if not info["first_receipt_date"] or date_dt < info["first_receipt_date"]:
                info["first_receipt_date"] = date_dt
            if not info["last_receipt_date"] or date_dt > info["last_receipt_date"]:
                info["last_receipt_date"] = date_dt

    # Aggregate payments: principal repayments and profit payouts
    for line in payment_lines:
        if not isinstance(line, dict):
            continue
        account_str = (line.get("account") or "").strip()
        amount = abs(line.get("amount", {}).get("value", 0))

        # Principal repayments to Loans payable — Name
        investor_name_lp = _parse_investor_name_from_account(account_str, "Loans payable")
        if investor_name_lp:
            info = ensure_investor(investor_name_lp)
            info["principal_repaid"] += amount
            continue

        # Profit paid to Profit payable / Dividend payable — Name
        investor_name_profit = (
            _parse_investor_name_from_account(account_str, "Profit payable")
            or _parse_investor_name_from_account(account_str, "Dividend payable")
        )
        if investor_name_profit:
            info = ensure_investor(investor_name_profit)
            info["profit_paid"] += amount

    # Finalize computed balance and match flag
    for info in summary.values():
        computed_balance = info["total_received"] - info["principal_repaid"]
        info["computed_balance"] = computed_balance
        current_balance = info["current_balance"] or 0.0
        info["balance_match"] = abs(computed_balance - current_balance) < 0.01

    # Sort investors alphabetically for display
    investors_summary = sorted(summary.values(), key=lambda x: x["name"])

    totals = {
        "total_received": sum(i["total_received"] for i in investors_summary),
        "principal_repaid": sum(i["principal_repaid"] for i in investors_summary),
        "computed_balance": sum(i["computed_balance"] for i in investors_summary),
        "current_balance": sum(i["current_balance"] for i in investors_summary),
        "profit_paid": sum(i["profit_paid"] for i in investors_summary),
    }

    summary_cache = {"investors": investors_summary, "totals": totals}
    summary_last_update = datetime.utcnow()

    return render_template(
        "investment_summary.html",
        investors=investors_summary,
        totals=totals,
        format_currency=format_currency,
    )


@app.route('/investment_summary')
def investment_summary():
    """
    New grouped summary: one row per investor (base name) plus
    per-phase/per-ledger detail rows.
    """
    search_query = (request.args.get("q") or "").strip()
    search_lower = search_query.lower()

    accounts_data = fetch_special_accounts()
    receipt_lines = fetch_receipt_lines()
    payment_lines = fetch_payment_lines()
    journal_lines = fetch_journal_entry_lines()

    groups = {}

    def ensure_group_and_phase(raw_name: str, current_balance_delta: float = 0.0):
        base_name, phase_label, display_name = split_investor_variant(raw_name)
        if not base_name:
            return None, None

        group = groups.get(base_name)
        if not group:
            group = groups[base_name] = {
                "name": base_name,
                "current_balance": 0.0,
                "total_received": 0.0,
                "principal_repaid": 0.0,
                "profit_paid": 0.0,
                "first_receipt_date": None,
                "last_receipt_date": None,
                "phases": {},
            }
        group["current_balance"] += current_balance_delta

        phases = group["phases"]
        phase = phases.get(display_name)
        if not phase:
            phase = phases[display_name] = {
                "name": display_name,
                "phase": phase_label,
                "current_balance": 0.0,
                "total_received": 0.0,
                "principal_repaid": 0.0,
                "profit_paid": 0.0,
                "first_receipt_date": None,
                "last_receipt_date": None,
            }
        phase["current_balance"] += current_balance_delta
        return group, phase

    # Seed groups from Loans payable special accounts
    for entry in accounts_data:
        if entry.get("controlAccount") != "Loans payable":
            continue
        raw_name = entry.get("name", "")
        balance = extract_balance_amount(entry)
        if not raw_name or balance == 0:
            continue
        ensure_group_and_phase(raw_name, current_balance_delta=balance)

    # Aggregate receipts into Loans payable accounts
    for line in receipt_lines:
        if not isinstance(line, dict):
            continue
        account_str = (line.get("account") or "").strip()
        investor_raw = _parse_investor_name_from_account(account_str, "Loans payable")
        if not investor_raw:
            continue

        amount = abs(line.get("amount", {}).get("value", 0))
        group, phase = ensure_group_and_phase(investor_raw)
        if not group or not phase:
            continue

        group["total_received"] += amount
        phase["total_received"] += amount

        date_str = line.get("date")
        date_dt = parse_date(date_str) if date_str else None
        if date_dt:
            for target in (group, phase):
                if not target["first_receipt_date"] or date_dt < target["first_receipt_date"]:
                    target["first_receipt_date"] = date_dt
                if not target["last_receipt_date"] or date_dt > target["last_receipt_date"]:
                    target["last_receipt_date"] = date_dt

    # Aggregate payments: principal repayments and profit payouts
    for line in payment_lines:
        if not isinstance(line, dict):
            continue
        account_str = (line.get("account") or "").strip()
        amount = abs(line.get("amount", {}).get("value", 0))

        inv_lp = _parse_investor_name_from_account(account_str, "Loans payable")
        inv_pp = (
            _parse_investor_name_from_account(account_str, "Profit payable")
            or _parse_investor_name_from_account(account_str, "Dividend payable")
        )

        if inv_lp:
            group, phase = ensure_group_and_phase(inv_lp)
            if not group or not phase:
                continue
            group["principal_repaid"] += amount
            phase["principal_repaid"] += amount
            continue

        if inv_pp:
            group, phase = ensure_group_and_phase(inv_pp)
            if not group or not phase:
                continue
            group["profit_paid"] += amount
            phase["profit_paid"] += amount

    # Journal-entry-lines: Loans payable principal and Profit payable profit
    for line in journal_lines:
        if not isinstance(line, dict):
            continue
        account_str = (line.get("account") or "").strip()
        debit = line.get("debit") or {}
        credit = line.get("credit") or {}
        debit_val = abs(debit.get("value", 0)) if isinstance(debit, dict) else 0
        credit_val = abs(credit.get("value", 0)) if isinstance(credit, dict) else 0

        inv_lp = _parse_investor_name_from_account(account_str, "Loans payable")
        inv_pp = (
            _parse_investor_name_from_account(account_str, "Profit payable")
            or _parse_investor_name_from_account(account_str, "Dividend payable")
        )

        # Loans payable: credit increases principal, debit reduces principal
        if inv_lp:
            group, phase = ensure_group_and_phase(inv_lp)
            if not group or not phase:
                continue
            if credit_val:
                group["total_received"] += credit_val
                phase["total_received"] += credit_val
            if debit_val:
                group["principal_repaid"] += debit_val
                phase["principal_repaid"] += debit_val
            continue

        # Profit payable: debit reduces liability, treat as profit paid
        if inv_pp and debit_val:
            group, phase = ensure_group_and_phase(inv_pp)
            if not group or not phase:
                continue
            group["profit_paid"] += debit_val
            phase["profit_paid"] += debit_val

    # Finalize computed balances and match flags
    group_list = []
    for group in groups.values():
        for phase in group["phases"].values():
            phase["computed_balance"] = phase["total_received"] - phase["principal_repaid"]

        group["computed_balance"] = group["total_received"] - group["principal_repaid"]
        current_balance = group["current_balance"] or 0.0
        group["balance_match"] = abs(group["computed_balance"] - current_balance) < 0.01

        group["phases_list"] = sorted(group["phases"].values(), key=lambda p: p["name"])
        group_list.append(group)

    # Order summary groups by current balance (largest to smallest)
    group_list.sort(key=lambda g: (g["current_balance"] or 0), reverse=True)

    # Optional filter by investor base name
    if search_query:
        group_list = [
            g for g in group_list
            if search_lower in (g["name"] or "").lower()
        ]

    totals = {
        "total_received": sum(g["total_received"] for g in group_list),
        "principal_repaid": sum(g["principal_repaid"] for g in group_list),
        "computed_balance": sum(g["computed_balance"] for g in group_list),
        "current_balance": sum(g["current_balance"] for g in group_list),
        "profit_paid": sum(g["profit_paid"] for g in group_list),
    }

    return render_template(
        "investment_summary.html",
        groups=group_list,
        totals=totals,
        format_currency=format_currency,
        search_query=search_query,
    )


@app.route('/journal')
def journal():
    # Placeholder journal view – currently no API integration.
    # Renders the Journal page with an empty list.
    return render_template("journal.html", journal_entries=[])

# ---------------------------
# Chart Data API Route
# ---------------------------
@app.route('/chart_data')
def chart_data():
    # Return investor GROUP names and their total balances
    # (same base-name grouping used on the dashboard),
    # with optional ?q=<name> filter.
    search_query = (request.args.get("q") or "").strip().lower()
    raw_investors = Investor.query.order_by(Investor.name).all()
    grouped = group_investors_for_dashboard(raw_investors)
    if search_query:
        grouped = [g for g in grouped if search_query in (g["name"] or "").lower()]
    labels = [g["name"] for g in grouped]
    balances = [g["balance"] for g in grouped]
    return jsonify({'labels': labels, 'balances': balances})

# ---------------------------
# Gantt Data API Route (for Investor Timeline)
# ---------------------------
@app.route('/gantt_data')
def gantt_data():
    investors = Investor.query.all()
    rows = []
    for inv in investors:
        if inv.start_date and inv.end_date:
            rows.append({
                'investor': inv.name,
                'start_date': inv.start_date,  # Must be in YYYY-MM-DD format
                'end_date': inv.end_date,        # Must be in YYYY-MM-DD format
                'invested_amount': inv.balance   # Using balance as invested amount
            })
    return jsonify({'rows': rows})

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(debug=debug_mode)
