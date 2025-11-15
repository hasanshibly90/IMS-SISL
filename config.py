import os

"""
Central configuration for the Investor Management module.

All values can be overridden via environment variables so this
module can be reused for other Manager.io clients without
changing code.
"""

# Base URL and API key for Manager.io
MANAGER_API_BASE_URL = (
    os.environ.get("MANAGER_API_BASE_URL")
    or os.environ.get("AIOSOL_API_BASE_URL")
    or "https://esourcingbd.ap-southeast-1.manager.io/api2"
)

MANAGER_API_KEY = (
    os.environ.get("MANAGER_API_KEY")
    or os.environ.get("AIOSOL_API_KEY")
    or "Ch5TTUFSVCBJTkRVU1RSSUFMIFNPTFVUSU9OIExURC4SEgnyKhJxeaxVRhGtOA2alblJKBoSCQKFGqhLRrVBEZAgv0uBOk6W"
)

# HTTP timeout for Manager.io API calls (seconds)
API_TIMEOUT_SECONDS = int(os.environ.get("MANAGER_API_TIMEOUT_SECONDS", "10"))

# Minimum interval between automatic syncs (seconds)
UPDATE_INTERVAL_SECONDS = int(os.environ.get("INVESTOR_UPDATE_INTERVAL_SECONDS", "300"))

# Custom field IDs for investor terms (Start Date, End Date, Profit %)
# These can be overridden via env vars per tenant.
FIELD_IDS = {
    "start_new": os.environ.get(
        "INVESTOR_FIELD_START_NEW", "826be8ff-63ab-4773-a616-c322ff84063e"
    ),
    "end_new": os.environ.get(
        "INVESTOR_FIELD_END_NEW", "6e7981f8-d83f-44b8-beac-55c0acd7592c"
    ),
    "profit_new": os.environ.get(
        "INVESTOR_FIELD_PROFIT_NEW", "5862bbaa-82ea-4094-a2a4-7fc6a77ebac4"
    ),
    "start_old": os.environ.get(
        "INVESTOR_FIELD_START_OLD", "f30ea2f8-02af-4e5e-b9ec-b8c7ef2d12e2"
    ),
    "end_old": os.environ.get(
        "INVESTOR_FIELD_END_OLD", "c4b22208-6d56-4c34-870c-c5f40954526f"
    ),
    "profit_old": os.environ.get(
        "INVESTOR_FIELD_PROFIT_OLD", "1e1a26a2-b4a5-4c89-b259-368ec797177e"
    ),
}

