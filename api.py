"""
Web API for license verification.
Runs alongside the Discord bot to provide HTTP endpoints for the macro.
"""
from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
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

# Ed25519 for new token signing
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# Ed25519 private key for signing tokens (keep secret!)
PRIVATE_KEY_B64 = os.getenv("PRIVATE_KEY_B64", "ivndKTkbKsa74AoKIAyx4cEbRtw3gH+k2hDtuOkF4/E=")


def get_private_key() -> Ed25519PrivateKey:
    """Load the Ed25519 private key."""
    private_bytes = base64.b64decode(PRIVATE_KEY_B64)
    return Ed25519PrivateKey.from_private_bytes(private_bytes)


def generate_signed_token(discord_id: str, username: str, expires_timestamp: int, product: str = "") -> str:
    """Generate an Ed25519 signed token for the user."""
    payload = {
        "did": discord_id,
        "name": username,
        "exp": expires_timestamp
    }
    if product:
        payload["product"] = product

    payload_json = json.dumps(payload, separators=(',', ':'))
    payload_b64 = base64.urlsafe_b64encode(payload_json.encode()).decode().rstrip('=')

    private_key = get_private_key()
    signature = private_key.sign(payload_b64.encode())
    signature_b64 = base64.urlsafe_b64encode(signature).decode().rstrip('=')

    return f"{payload_b64}.{signature_b64}"


class DiscordAuthRequest(BaseModel):
    discord_id: str
    hwid: Optional[str] = None
    product: Optional[str] = None  # "saints-gen" or "saints-shot"

# API has its own database pool (separate from bot to avoid thread conflicts)
DATABASE_URL = os.getenv("DATABASE_URL", "")
_api_pool: Optional[asyncpg.Pool] = None


async def get_api_pool() -> asyncpg.Pool:
    """Get or create the API's own connection pool."""
    global _api_pool
    if _api_pool is None:
        _api_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
        # Initialize the notifications table
        async with _api_pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS shopify_notifications (
                    id SERIAL PRIMARY KEY,
                    discord_id TEXT NOT NULL,
                    license_key TEXT NOT NULL,
                    expires_at TIMESTAMP NOT NULL,
                    product TEXT NOT NULL,
                    customer_name TEXT,
                    email TEXT,
                    order_number TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    delivered INTEGER DEFAULT 0,
                    delivery_attempts INTEGER DEFAULT 0,
                    last_attempt_at TIMESTAMP,
                    error_message TEXT
                )
            """)
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
async def verify_license(
    key: str,
    hwid: Optional[str] = None,
    product: Optional[str] = None,
    integrity: Optional[str] = None,
    version: Optional[str] = None
):
    """
    Verify if a license key is valid and not revoked.
    Also checks hardware ID binding and returns feature flags.

    Query params:
        key: The license key to verify
        hwid: The hardware ID of the machine (optional but recommended)
        product: The product to verify for (optional - saints-gen or saints-shot)
        integrity: Hash of client files for tamper detection (optional)
        version: Client version string (optional)

    Returns:
        {"valid": true/false, "reason": "...", "config": {"features": {...}}}
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
            bound = False
            if hwid:
                if stored_hwid is None:
                    # First activation - bind to this hardware
                    await conn.execute(
                        "UPDATE licenses SET hwid = $1 WHERE license_key = $2",
                        hwid, key
                    )
                    bound = True
                elif stored_hwid != hwid:
                    # Hardware mismatch - license used on different machine
                    return {"valid": False, "reason": "hwid_mismatch"}

            # Build feature flags based on product type
            license_product = row["product"] or "saints-shot"

            # Default features for saints-shot
            features = {
                "shooting": True,
                "defense": True,
                "sync": True,
                "auto_jump": True,
                "auto_follow": True,
            }

            # Build response with config
            response = {
                "valid": True,
                "reason": "activated" if bound else "active",
                "config": {
                    "features": features,
                    "product": license_product,
                }
            }

            if bound:
                response["bound"] = True

            return response

    except Exception as e:
        # On database error, fail open (allow access)
        # This prevents lockout if database is temporarily unavailable
        print(f"Database error in verify: {e}")
        traceback.print_exc()
        return {
            "valid": True,
            "reason": "db_error",
            "error": str(e),
            "config": {
                "features": {
                    "shooting": True,
                    "defense": True,
                    "sync": True,
                    "auto_jump": True,
                    "auto_follow": True,
                }
            }
        }


@app.post("/auth/discord")
async def auth_discord(request: DiscordAuthRequest):
    """
    Authenticate user with Discord ID and return a signed token.
    This allows users to login with their Discord ID instead of a license key.

    Body:
        discord_id: The user's Discord ID (numeric string)
        hwid: The hardware ID of the machine (optional)
        product: The product to authenticate for ("saints-gen" or "saints-shot")

    Returns:
        {"success": true, "token": "...", "username": "...", "expires_at": "...", "product": "..."}
    """
    discord_id = request.discord_id.strip()
    hwid = request.hwid.strip() if request.hwid else ""
    requested_product = request.product.strip().lower() if request.product else ""

    if not discord_id:
        raise HTTPException(status_code=400, detail="Missing Discord ID")

    if not discord_id.isdigit():
        raise HTTPException(status_code=400, detail="Invalid Discord ID format")

    # Require product parameter - blocks old clients that don't send it
    valid_products = ["saints-gen", "saints-shot"]
    if not requested_product:
        return {
            "success": False,
            "error": "Update required! Please download the latest version from Discord."
        }
    if requested_product not in valid_products:
        return {
            "success": False,
            "error": f"Invalid product. Must be one of: {', '.join(valid_products)}"
        }

    try:
        pool = await get_api_pool()
        async with pool.acquire() as conn:
            # Get license for this Discord ID, filtered by product (required)
            row = await conn.fetchrow(
                """SELECT discord_id, discord_name, expires_at, hwid, product, revoked
                   FROM licenses
                   WHERE discord_id = $1 AND revoked = 0 AND product = $2
                   ORDER BY expires_at DESC
                   LIMIT 1""",
                discord_id, requested_product
            )

            if not row:
                if requested_product:
                    product_name = "Saint's Gen" if requested_product == "saints-gen" else "Saint's Shot"
                    return {
                        "success": False,
                        "error": f"No active {product_name} subscription found for this Discord ID"
                    }
                return {
                    "success": False,
                    "error": "No active subscription found for this Discord ID"
                }

            if row["revoked"]:
                return {
                    "success": False,
                    "error": "Your subscription has been revoked"
                }

            # Check expiration
            expires_at = row["expires_at"]
            if isinstance(expires_at, str):
                expires_at = datetime.fromisoformat(expires_at)

            if datetime.utcnow() > expires_at:
                return {
                    "success": False,
                    "error": "Your subscription has expired"
                }

            expires_timestamp = int(expires_at.timestamp())

            # Check HWID binding
            stored_hwid = row["hwid"]
            if stored_hwid and hwid and stored_hwid != hwid:
                return {
                    "success": False,
                    "error": "This subscription is bound to another PC. Contact support to reset."
                }

            # Bind HWID if not already bound (product-specific binding)
            if not stored_hwid and hwid:
                license_product = row["product"] or ""
                if license_product:
                    # Update only the specific product's license
                    await conn.execute(
                        "UPDATE licenses SET hwid = $1 WHERE discord_id = $2 AND product = $3 AND revoked = 0",
                        hwid, discord_id, license_product
                    )
                else:
                    await conn.execute(
                        "UPDATE licenses SET hwid = $1 WHERE discord_id = $2 AND revoked = 0",
                        hwid, discord_id
                    )

            # Generate Ed25519 signed token
            username = row["discord_name"] or "User"
            product = row["product"] or ""
            token = generate_signed_token(discord_id, username, expires_timestamp, product)

            return {
                "success": True,
                "token": token,
                "username": username,
                "avatar_url": "",
                "expires_at": expires_at.isoformat(),
                "product": product
            }

    except Exception as e:
        print(f"Database error in auth_discord: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Database error: {e}")


# ==================== SHOPIFY WEBHOOK ====================

# Import config for Shopify settings
from config import (
    SHOPIFY_WEBHOOK_SECRET, SHOPIFY_PRODUCT_MAP, DEFAULT_LICENSE_DAYS,
    SECRET_KEY, GUILD_ID, SUBSCRIBER_ROLE_ID, SAINTS_SHOT_ROLE_ID
)
from license_crypto import generate_license_key


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
            # Save the license
            await conn.execute(
                """INSERT INTO licenses (license_key, discord_id, discord_name, expires_at, product)
                   VALUES ($1, $2, $3, $4, $5)""",
                license_key, discord_id, customer_name, expires_at, product
            )
            print(f"License saved to database for {discord_id}")

            # Queue notification in database (persists across restarts!)
            await conn.execute(
                """INSERT INTO shopify_notifications
                   (discord_id, license_key, expires_at, product, customer_name, email, order_number)
                   VALUES ($1, $2, $3, $4, $5, $6, $7)""",
                discord_id, license_key, expires_at, product, customer_name, email, str(order_number)
            )
            print(f"Notification queued in database for order #{order_number}")

    except Exception as e:
        print(f"Database error saving license: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Failed to save license")

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
    Get pending Discord notifications from the database.
    Called by the Discord bot to retrieve licenses that need DMs sent.
    """
    try:
        pool = await get_api_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT id, discord_id, license_key, expires_at, product, customer_name, email, order_number
                   FROM shopify_notifications
                   WHERE delivered = 0 AND delivery_attempts < 5
                   ORDER BY created_at ASC
                   LIMIT 50"""
            )
            notifications = [dict(row) for row in rows]
            # Convert datetime to ISO format string
            for notif in notifications:
                if notif.get("expires_at"):
                    notif["expires_at"] = notif["expires_at"].isoformat()
            return {"notifications": notifications}
    except Exception as e:
        print(f"Error fetching pending notifications: {e}")
        return {"notifications": []}


@app.post("/shopify/notification/{notification_id}/delivered")
async def mark_notification_delivered(notification_id: int):
    """Mark a notification as successfully delivered."""
    try:
        pool = await get_api_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """UPDATE shopify_notifications
                   SET delivered = 1, last_attempt_at = $1
                   WHERE id = $2""",
                datetime.utcnow(), notification_id
            )
        return {"success": True}
    except Exception as e:
        print(f"Error marking notification delivered: {e}")
        return {"success": False, "error": str(e)}


@app.post("/shopify/notification/{notification_id}/failed")
async def mark_notification_failed(notification_id: int, error: str = None):
    """Mark a notification attempt as failed (will retry later)."""
    try:
        pool = await get_api_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """UPDATE shopify_notifications
                   SET delivery_attempts = delivery_attempts + 1,
                       last_attempt_at = $1,
                       error_message = $2
                   WHERE id = $3""",
                datetime.utcnow(), error, notification_id
            )
        return {"success": True}
    except Exception as e:
        print(f"Error marking notification failed: {e}")
        return {"success": False, "error": str(e)}


@app.get("/health")
async def health():
    """Health check for Railway."""
    return {"status": "healthy"}


# ==================== VERSION CHECK ====================

# Per-product version requirements
PRODUCT_VERSIONS = {
    "saints-gen": {
        "current": "2.5.1",
        "min": "2.5.1",
        "message": "Please download the latest version from the Discord server."
    },
    "saints-shot": {
        "current": "2.0.0",
        "min": "2.0.0",
        "message": "Please download the latest version from the Discord server."
    }
}

# Default/legacy versions (for clients that don't specify product)
CURRENT_VERSION = "2.5.1"
MIN_VERSION = "2.5.1"

@app.get("/version")
async def get_version(product: Optional[str] = None):
    """Return version info for version checker."""
    if product and product in PRODUCT_VERSIONS:
        version_info = PRODUCT_VERSIONS[product]
        return {
            "version": version_info["current"],
            "min_version": version_info["min"],
            "update_message": version_info["message"],
            "product": product
        }

    # Legacy response for clients that don't specify product
    return {
        "version": CURRENT_VERSION,
        "min_version": MIN_VERSION,
        "update_message": "Please download the latest version from the Discord server."
    }


# ==================== ADMIN ENDPOINTS ====================

ADMIN_SECRET = os.getenv("ADMIN_SECRET", SECRET_KEY)  # Use SECRET_KEY as fallback


@app.post("/admin/reset-all-hwids")
async def reset_all_hwids(
    secret: str = Header(None, alias="X-Admin-Secret"),
    product: Optional[str] = None
):
    """
    Reset all hardware ID bindings (forces everyone to re-authenticate).
    Requires X-Admin-Secret header.

    Query params:
        product: Optional product filter (saints-gen or saints-shot)
    """
    if not secret or secret != ADMIN_SECRET:
        raise HTTPException(status_code=401, detail="Invalid admin secret")

    try:
        pool = await get_api_pool()
        async with pool.acquire() as conn:
            if product:
                result = await conn.execute(
                    "UPDATE licenses SET hwid = NULL WHERE hwid IS NOT NULL AND product = $1",
                    product
                )
            else:
                result = await conn.execute(
                    "UPDATE licenses SET hwid = NULL WHERE hwid IS NOT NULL"
                )
            # Parse "UPDATE N" to get count
            count = int(result.split()[-1]) if result else 0

        return {
            "success": True,
            "count": count,
            "message": f"Reset {count} hardware bindings" + (f" for {product}" if product else "")
        }
    except Exception as e:
        print(f"Error resetting HWIDs: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
