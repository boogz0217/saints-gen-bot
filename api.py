"""
Web API for license verification.
Runs alongside the Discord bot to provide HTTP endpoints for the macro.
"""
from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime
from typing import Optional
import asyncpg
import os
import traceback
import hmac
import hashlib
import base64
import json
import re

# API has its own database pool (separate from bot to avoid thread conflicts)
DATABASE_URL = os.getenv("DATABASE_URL", "")
_api_pool: Optional[asyncpg.Pool] = None


async def get_api_pool() -> asyncpg.Pool:
    """Get or create the API's own connection pool."""
    global _api_pool
    if _api_pool is None:
        _api_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    return _api_pool


app = FastAPI(title="Saint's Gen License API", docs_url=None, redoc_url=None)

# Allow CORS for the macro to call
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/")
async def root():
    """Health check endpoint."""
    return {"status": "ok", "service": "saints-gen-license"}


@app.get("/verify")
async def verify_license(key: str, hwid: Optional[str] = None, product: Optional[str] = None):
    """
    Verify if a license key is valid and not revoked.
    Also checks hardware ID binding.

    Query params:
        key: The license key to verify
        hwid: The hardware ID of the machine (optional but recommended)
        product: The product to verify for (optional - saints-gen or saints-shot)

    Returns:
        {"valid": true/false, "reason": "..."}
    """
    if not key or not key.startswith("SAINT-"):
        return {"valid": False, "reason": "invalid_format"}

    try:
        pool = await get_api_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT revoked, expires_at, hwid, product FROM licenses WHERE license_key = $1",
                key
            )

            if not row:
                return {"valid": False, "reason": "not_found"}

            # Check if license is for the correct product
            if product and row["product"] != product:
                return {"valid": False, "reason": "wrong_product"}

            # Check if revoked
            if row["revoked"]:
                return {"valid": False, "reason": "revoked"}

            # Check if expired
            expires_at = row["expires_at"]
            if isinstance(expires_at, str):
                expires_at = datetime.fromisoformat(expires_at)
            if expires_at < datetime.utcnow():
                return {"valid": False, "reason": "expired"}

            # Check hardware ID binding
            stored_hwid = row["hwid"]
            if hwid:
                if stored_hwid is None:
                    # First activation - bind to this hardware
                    await conn.execute(
                        "UPDATE licenses SET hwid = $1 WHERE license_key = $2",
                        hwid, key
                    )
                    return {"valid": True, "reason": "activated", "bound": True}
                elif stored_hwid != hwid:
                    # Hardware mismatch - license used on different machine
                    return {"valid": False, "reason": "hwid_mismatch"}

            return {"valid": True, "reason": "active"}

    except Exception as e:
        # On database error, fail open (allow access)
        # This prevents lockout if database is temporarily unavailable
        print(f"Database error in verify: {e}")
        traceback.print_exc()
        return {"valid": True, "reason": "db_error", "error": str(e)}


# ==================== SHOPIFY WEBHOOK ====================

# Import config for Shopify settings
from config import (
    SHOPIFY_WEBHOOK_SECRET, SHOPIFY_PRODUCT_MAP, DEFAULT_LICENSE_DAYS,
    SECRET_KEY, GUILD_ID, SUBSCRIBER_ROLE_ID, SAINTS_SHOT_ROLE_ID
)
from license_crypto import generate_license_key

# Store for pending Discord notifications (processed by bot)
pending_notifications = []


def verify_shopify_webhook(data: bytes, hmac_header: str) -> bool:
    """Verify that the webhook request came from Shopify."""
    if not SHOPIFY_WEBHOOK_SECRET:
        print("WARNING: SHOPIFY_WEBHOOK_SECRET not set, skipping verification")
        return True

    calculated = base64.b64encode(
        hmac.new(
            SHOPIFY_WEBHOOK_SECRET.encode('utf-8'),
            data,
            hashlib.sha256
        ).digest()
    ).decode('utf-8')

    return hmac.compare_digest(calculated, hmac_header)


def extract_discord_id(order: dict) -> Optional[str]:
    """
    Extract Discord ID/username from order.
    Checks multiple locations where it might be stored.
    """
    # Check order note
    note = order.get("note", "") or ""

    # Look for Discord ID patterns (numeric ID or username#discriminator or just username)
    # Pattern: Discord: 123456789 or Discord ID: 123456789
    discord_patterns = [
        r"discord\s*(?:id)?[:\s]+(\d{17,19})",  # Discord ID: 123456789012345678
        r"discord\s*[:\s]+([a-zA-Z0-9_.]+(?:#\d{4})?)",  # Discord: username#1234 or username
    ]

    for pattern in discord_patterns:
        match = re.search(pattern, note, re.IGNORECASE)
        if match:
            return match.group(1)

    # Check note_attributes (custom checkout fields)
    note_attributes = order.get("note_attributes", [])
    for attr in note_attributes:
        name = attr.get("name", "").lower()
        if "discord" in name:
            return attr.get("value", "").strip()

    # Check custom attributes on line items
    for item in order.get("line_items", []):
        for prop in item.get("properties", []):
            name = prop.get("name", "").lower()
            if "discord" in name:
                return prop.get("value", "").strip()

    return None


def get_license_config(order: dict) -> dict:
    """
    Determine license product and duration based on Shopify order.
    Returns {"product": "saints-gen", "days": 30}
    """
    for item in order.get("line_items", []):
        title = item.get("title", "").lower()
        variant = item.get("variant_title", "").lower() if item.get("variant_title") else ""
        sku = item.get("sku", "").lower() if item.get("sku") else ""

        # Combine all product info for matching
        full_text = f"{title} {variant} {sku}"

        print(f"Matching product: title='{title}', variant='{variant}', sku='{sku}'")

        # Check for Saint's Gen first (simplest - only one option)
        if "gen" in full_text and "shot" not in full_text:
            print("Matched: Saint's Gen - 30 days")
            return {"product": "saints-gen", "days": 30}

        # Check for Saint's Shot
        if "shot" in full_text:
            # Check for weekly (7 days) - check variant for "week"
            if "week" in variant or "week" in full_text:
                print("Matched: Saint's Shot Weekly - 7 days")
                return {"product": "saints-shot", "days": 7}
            # Monthly (30 days)
            else:
                print("Matched: Saint's Shot Monthly - 30 days")
                return {"product": "saints-shot", "days": 30}

    # Default: saints-gen with default days
    print("No match found, defaulting to Saint's Gen - 30 days")
    return {"product": "saints-gen", "days": DEFAULT_LICENSE_DAYS}


@app.post("/shopify/webhook")
async def shopify_order_webhook(
    request: Request,
    x_shopify_hmac_sha256: str = Header(None, alias="X-Shopify-Hmac-SHA256"),
    x_shopify_topic: str = Header(None, alias="X-Shopify-Topic")
):
    """
    Handle Shopify order webhooks.
    Automatically generates license keys when orders are paid.
    """
    # Get raw body for HMAC verification
    body = await request.body()

    # Verify webhook signature
    if x_shopify_hmac_sha256 and not verify_shopify_webhook(body, x_shopify_hmac_sha256):
        print("Shopify webhook signature verification failed")
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        order = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # Log the webhook
    order_id = order.get("id", "unknown")
    order_number = order.get("order_number", order.get("name", "unknown"))
    email = order.get("email", "unknown")
    print(f"Received Shopify webhook: Order #{order_number} (ID: {order_id}) for {email}")

    # Extract Discord ID from order
    discord_id = extract_discord_id(order)

    if not discord_id:
        print(f"No Discord ID found in order #{order_number}")
        # Still return 200 to acknowledge receipt (don't want Shopify to retry)
        # You might want to handle this case differently (email notification, etc.)
        return {
            "success": False,
            "reason": "no_discord_id",
            "message": "Order received but no Discord ID found. Customer needs to provide Discord ID."
        }

    print(f"Found Discord ID: {discord_id}")

    # Get license configuration based on product
    license_config = get_license_config(order)
    product = license_config["product"]
    days = license_config["days"]

    print(f"Generating {product} license for {days} days")

    # Get customer name for the license
    customer = order.get("customer", {})
    customer_name = f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip()
    if not customer_name:
        customer_name = email.split("@")[0] if email else "Customer"

    # Generate the license key
    license_key, expires_at = generate_license_key(
        SECRET_KEY,
        discord_id,
        days,
        customer_name
    )

    # Store in database
    try:
        pool = await get_api_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO licenses (license_key, discord_id, discord_name, expires_at, product)
                   VALUES ($1, $2, $3, $4, $5)""",
                license_key, discord_id, customer_name, expires_at, product
            )
        print(f"License saved to database for {discord_id}")
    except Exception as e:
        print(f"Database error saving license: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Failed to save license")

    # Queue notification for Discord bot to send
    # The bot will pick this up and DM the user + assign roles
    pending_notifications.append({
        "discord_id": discord_id,
        "license_key": license_key,
        "expires_at": expires_at.isoformat(),
        "product": product,
        "customer_name": customer_name,
        "email": email,
        "order_number": order_number
    })

    print(f"License generated successfully for order #{order_number}")

    return {
        "success": True,
        "order_number": order_number,
        "discord_id": discord_id,
        "product": product,
        "days": days,
        "expires_at": expires_at.isoformat()
    }


@app.get("/shopify/pending")
async def get_pending_notifications():
    """
    Get pending Discord notifications.
    Called by the Discord bot to retrieve licenses that need DMs sent.
    """
    global pending_notifications
    notifications = pending_notifications.copy()
    pending_notifications = []  # Clear after retrieval
    return {"notifications": notifications}


@app.get("/health")
async def health():
    """Health check for Railway."""
    return {"status": "healthy"}
