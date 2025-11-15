from flask import Flask, render_template, jsonify
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func
from datetime import datetime
import requests
import os

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///investors.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

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
def fetch_special_accounts():
    url = "https://acc.aiosol.io/api2/special-accounts"
    headers = {
        'Accept': 'application/json',
        'X-API-KEY': 'CgRERU1PEhIJrKVd/OlFcE4RpRUvfTIbgu4aEgmPdm3eDvEGTRGEQtVhWu3Olw=='
    }
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        return response.json().get("specialAccounts", [])
    return []

def fetch_investor_details(key):
    url = f"https://acc.aiosol.io/api2/special-account-form/{key}"
    headers = {
        'X-API-KEY': 'CgRERU1PEhIJrKVd/OlFcE4RpRUvfTIbgu4aEgmPdm3eDvEGTRGEQtVhWu3Olw=='
    }
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        data = response.json()
        dates = data.get("CustomFields2", {}).get("Dates", {})
        decimals = data.get("CustomFields2", {}).get("Decimals", {})
        start_date = dates.get("f30ea2f8-02af-4e5e-b9ec-b8c7ef2d12e2", "").split("T")[0].strip()
        end_date = dates.get("c4b22208-6d56-4c34-870c-c5f40954526f", "").split("T")[0].strip()
        profit_percentage = decimals.get("1e1a26a2-b4a5-4c89-b259-368ec797177e", 0)
        return {
            "start_date": start_date,
            "end_date": end_date,
            "profit_percentage": profit_percentage
        }
    return {"start_date": "", "end_date": "", "profit_percentage": 0}

def fetch_payment_lines():
    """
    Fetch dividend paid amounts from the payment-lines API.
    Returns a list of payment line records.
    """
    url = "https://acc.aiosol.io/api2/payment-lines"
    headers = {
        'Accept': 'application/json',
        'X-API-KEY': 'CgRERU1PEhIJrKVd/OlFcE4RpRUvfTIbgu4aEgmPdm3eDvEGTRGEQtVhWu3Olw=='
    }
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        data = response.json()
        if isinstance(data, list):
            return data
        elif isinstance(data, dict):
            return data.get("paymentLines", [])
    return []

# ---------------------------
# Helper Functions
# ---------------------------
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

def format_currency(value):
    try:
        return "{:,.2f}".format(float(value)) if value else "0.00"
    except (ValueError, TypeError):
        return "0.00"

# ---------------------------
# Main Update Logic
# ---------------------------
def update_database():
    accounts_data = fetch_special_accounts()
    payment_lines = fetch_payment_lines()

    db.session.query(Investor).delete()  # Clear old records

    # Gather "Profit Payable" amounts by investor name from special accounts
    profit_payable_data = {}
    for entry in accounts_data:
        if entry.get("controlAccount") == "Profit Payable":
            name = entry.get("name", "")
            payable_value = abs(entry.get("balance", {}).get("value", 0))
            profit_payable_data[name] = payable_value

    # Aggregate dividend paid amounts from payment-lines API
    dividend_paid_data = {}
    for line in payment_lines:
        if not isinstance(line, dict):
            continue
        account_str = line.get("account", "")
        if account_str.lower().startswith("dividend payable"):
            parts = account_str.split("-")
            if parts and len(parts) >= 2:
                investor_name = parts[-1].strip()
                amount = abs(line.get("amount", {}).get("value", 0))
                dividend_paid_data[investor_name] = dividend_paid_data.get(investor_name, 0) + amount

    # Process investor "Loans payable" accounts
    for entry in accounts_data:
        if entry.get("controlAccount") != "Loans payable":
            continue

        name = entry.get("name", "")
        balance = abs(entry.get("balance", {}).get("value", 0))
        key = entry.get("key", "")

        details = fetch_investor_details(key)
        start_date = details.get("start_date", "")
        end_date = details.get("end_date", "")
        profit_percentage = details.get("profit_percentage", 0)

        start_date, end_date = ensure_correct_dates(start_date, end_date)
        start_dt = parse_date(start_date)
        end_dt = parse_date(end_date)

        duration_months = calculate_months_difference(start_dt, end_dt)
        remaining_months = calculate_remaining_months(end_dt)
        monthly_profit = calculate_monthly_profit(balance, profit_percentage)

        elapsed_months = calculate_elapsed_months(start_dt)
        total_profit_payable = elapsed_months * monthly_profit

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

@app.before_request
def before_request():
    update_database()

# ---------------------------
# Home Route (Table View)
# ---------------------------
@app.route('/')
def home():
    investors = Investor.query.all()

    total_monthly_profit = db.session.query(func.sum(Investor.monthly_profit)).scalar() or 0
    total_balance = db.session.query(func.sum(Investor.balance)).scalar() or 0
    total_profit_payable_up_to_now = db.session.query(func.sum(Investor.profit_payable_up_to_now)).scalar() or 0
    total_dividend_paid = db.session.query(func.sum(Investor.dividend_paid)).scalar() or 0
    total_current_payable = db.session.query(func.sum(Investor.profit_due)).scalar() or 0

    avg_profit_percentage = 0
    if investors:
        sum_percentage = sum(inv.profit_percentage or 0 for inv in investors)
        avg_profit_percentage = sum_percentage / len(investors)

    # Compute the custom Profit %:
    # (Total Monthly Profit * 12 * 100) / Total Balance Amount
    computed_profit_percentage = 0
    if total_balance > 0:
        computed_profit_percentage = (total_monthly_profit * 12 * 100) / total_balance

    return render_template(
        'task.html',
        investors=investors,
        format_currency=format_currency,
        total_monthly_profit=total_monthly_profit,
        total_balance=total_balance,
        total_profit_payable_up_to_now=total_profit_payable_up_to_now,
        total_dividend_paid=total_dividend_paid,
        total_current_payable=total_current_payable,
        avg_profit_percentage=avg_profit_percentage,
        computed_profit_percentage=computed_profit_percentage
    )

# ---------------------------
# Chart Data API Route
# ---------------------------
@app.route('/chart_data')
def chart_data():
    # Example: returning investor names and their balances.
    investors = Investor.query.all()
    labels = [inv.name for inv in investors]
    balances = [inv.balance for inv in investors]
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
    if os.path.exists("investors.db"):
        os.remove("investors.db")
    with app.app_context():
        db.create_all()
    app.run(debug=True)
