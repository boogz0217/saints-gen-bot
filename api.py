"""
Web API for license verification.
Runs alongside the Discord bot to provide HTTP endpoints for the macro.
"""
from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, HTMLResponse
from pydantic import BaseModel
from datetime import datetime, timedelta
from typing import Optional
import asyncpg
import aiohttp
import os
import traceback
import hmac
import hashlib
import base64
import json
import re
import secrets
import urllib.parse

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
    version: Optional[str] = None  # Client version for enforcement


def parse_version(version: str) -> tuple:
    """Parse version string to tuple for comparison."""
    try:
        return tuple(int(x) for x in version.split('.'))
    except:
        return (0, 0, 0)


# Per-product version requirements (used for license version enforcement)
PRODUCT_VERSIONS = {
    "saints-gen": {
        "current": "2.5.9",
        "min": "2.5.9",
        "message": "Please download the latest version from the Discord server."
    },
    "saints-shot": {
        "current": "2.1.0",
        "min": "2.1.0",
        "message": "Please download the latest version from the Discord server."
    }
}

# Default/legacy versions (for clients that don't specify product)
CURRENT_VERSION = "2.5.9"
MIN_VERSION = "2.5.9"


def check_version_allowed(product: str, version: str) -> tuple:
    """
    Check if a client version is allowed for a product.
    Returns (allowed: bool, message: str)
    """
    if not version:
        # No version sent = old client, block it
        return (False, "Update required! Please download the latest version from Discord.")

    min_version = PRODUCT_VERSIONS.get(product, {}).get("min", "0.0.0")

    if parse_version(version) < parse_version(min_version):
        return (False, f"Update required! Your version {version} is outdated. Minimum required: {min_version}")

    return (True, "")


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

    # Check version - block old clients that don't send version or are outdated
    if product:
        allowed, msg = check_version_allowed(product, version)
        if not allowed:
            return {"valid": False, "reason": "update_required", "message": msg}

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

    # Check version - block old clients that don't send version or are outdated
    client_version = request.version.strip() if request.version else ""
    allowed, msg = check_version_allowed(requested_product, client_version)
    if not allowed:
        return {
            "success": False,
            "error": msg
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
    # Check note_attributes FIRST (custom checkout fields - most reliable)
    note_attributes = order.get("note_attributes", [])
    print(f"[SHOPIFY] Checking note_attributes: {note_attributes}")
    for attr in note_attributes:
        name = attr.get("name", "").lower()
        value = attr.get("value", "").strip()
        print(f"[SHOPIFY] Attribute: {name} = {value}")
        # Check for "did" (our Discord ID parameter) or "discord" in name
        if (name == "did" or name == "discord_id" or "discord" in name) and value:
            # If it's a numeric ID, return it directly
            if value.isdigit() and len(value) >= 17:
                print(f"[SHOPIFY] Found Discord ID in note_attributes: {value}")
                return value
            # Otherwise return whatever they entered (username)
            print(f"[SHOPIFY] Found Discord value in note_attributes: {value}")
            return value

    # Check custom attributes on line items
    for item in order.get("line_items", []):
        for prop in item.get("properties", []):
            name = prop.get("name", "").lower()
            value = prop.get("value", "").strip()
            if "discord" in name and value:
                print(f"[SHOPIFY] Found Discord in line item property: {value}")
                return value

    # Check order note
    note = order.get("note", "") or ""
    print(f"[SHOPIFY] Checking order note: {note}")

    if note:
        # Look for Discord ID patterns (numeric ID or username#discriminator or just username)
        discord_patterns = [
            r"discord\s*(?:id)?[:\s]+(\d{17,19})",  # Discord ID: 123456789012345678
            r"discord\s*[:\s]+([a-zA-Z0-9_.]+(?:#\d{4})?)",  # Discord: username#1234 or username
            r"(\d{17,19})",  # Just a plain Discord ID number
        ]

        for pattern in discord_patterns:
            match = re.search(pattern, note, re.IGNORECASE)
            if match:
                print(f"[SHOPIFY] Found Discord in note via pattern: {match.group(1)}")
                return match.group(1)

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
    Stores purchase info - customer uses /redeem email@example.com in Discord.
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

    # Get license configuration based on product
    license_config = get_license_config(order)
    product = license_config["product"]
    days = license_config["days"]

    # Get customer name
    customer = order.get("customer", {})
    customer_name = f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip()
    if not customer_name:
        customer_name = email.split("@")[0] if email else "Customer"

    # Store purchase in database
    try:
        pool = await get_api_pool()
        async with pool.acquire() as conn:
            # Create purchases table if not exists
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS purchases (
                    id SERIAL PRIMARY KEY,
                    email TEXT NOT NULL,
                    customer_name TEXT,
                    product TEXT NOT NULL,
                    days INTEGER NOT NULL,
                    order_number TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    redeemed INTEGER DEFAULT 0,
                    redeemed_by TEXT,
                    redeemed_at TIMESTAMP
                )
            """)

            # Insert the purchase
            await conn.execute(
                """INSERT INTO purchases (email, customer_name, product, days, order_number)
                   VALUES ($1, $2, $3, $4, $5)""",
                email.lower().strip(), customer_name, product, days, str(order_number)
            )
            print(f"Purchase saved for {email} - Order #{order_number}")
    except Exception as e:
        print(f"Database error saving purchase: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Failed to save purchase")

    print(f"Order #{order_number}: {email} can use /redeem {email} for {product} ({days} days)")

    return {
        "success": True,
        "order_number": order_number,
        "email": email,
        "product": product,
        "days": days,
        "message": f"Customer should use /redeem {email} in Discord to activate their license."
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


@app.get("/discord-link")
async def get_discord_link():
    """
    Get the direct Discord OAuth link to embed in Shopify.
    This link goes directly to Discord's authorize page.
    """
    if not DISCORD_CLIENT_ID or not DISCORD_REDIRECT_URI:
        return {"error": "Discord OAuth not configured"}

    # Build the direct Discord OAuth URL
    params = {
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": DISCORD_REDIRECT_URI,
        "response_type": "code",
        "scope": "identify email guilds.join"  # guilds.join for auto-joining server
    }
    oauth_url = f"https://discord.com/oauth2/authorize?{urllib.parse.urlencode(params)}"

    return {
        "link": oauth_url,
        "instructions": "Add this link to your Shopify checkout or product page. Customers click it to link their Discord before purchasing."
    }


@app.get("/shopify-script", response_class=HTMLResponse)
async def get_shopify_script():
    """
    Returns the script to add to your Shopify theme.
    This captures the Discord ID from URL and adds it to cart.
    """
    script = """
<!-- Discord ID Capture Script - Add this to your theme.liquid before </head> -->
<script>
(function() {
    // Get Discord ID from URL hash fragment (survives Shopify redirects)
    // Supports both #did=xxx&dname=yyy and ?did=xxx&dname=yyy formats
    var discordId = null;
    var discordName = null;

    // First check hash fragment (preferred - survives redirects)
    if (window.location.hash) {
        var hashParams = new URLSearchParams(window.location.hash.substring(1));
        discordId = hashParams.get('did');
        discordName = hashParams.get('dname');
    }

    // Fallback to query params for backwards compatibility
    if (!discordId) {
        var urlParams = new URLSearchParams(window.location.search);
        discordId = urlParams.get('did');
        discordName = urlParams.get('dname');
    }

    // If Discord ID found, save to localStorage
    if (discordId) {
        localStorage.setItem('discord_id', discordId);
        localStorage.setItem('discord_name', discordName || 'User');
        console.log('Discord ID saved:', discordId);

        // Clean up the URL (remove hash fragment) so it looks nicer
        if (window.location.hash && window.history.replaceState) {
            window.history.replaceState(null, '', window.location.pathname + window.location.search);
        }
    }

    // Add Discord ID to cart before checkout
    const savedDiscordId = localStorage.getItem('discord_id');
    if (savedDiscordId) {
        // Override fetch to intercept cart requests
        const originalFetch = window.fetch;
        window.fetch = function(url, options) {
            if (url.includes('/cart/add') || url.includes('/cart/update')) {
                // Add Discord ID to cart attributes
                if (options && options.body) {
                    try {
                        let body = JSON.parse(options.body);
                        body.attributes = body.attributes || {};
                        body.attributes['did'] = savedDiscordId;
                        body.attributes['discord_name'] = localStorage.getItem('discord_name') || 'User';
                        options.body = JSON.stringify(body);
                    } catch(e) {}
                }
            }
            return originalFetch.apply(this, arguments);
        };

        // Also add to any existing cart via AJAX
        fetch('/cart/update.js', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                attributes: {
                    'did': savedDiscordId,
                    'discord_name': localStorage.getItem('discord_name') || 'User'
                }
            })
        }).catch(function(){});
    }
})();
</script>
"""

    return HTMLResponse(f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Shopify Script</title>
        <style>
            body {{ font-family: Arial, sans-serif; padding: 40px; background: #1a1a2e; color: #fff; }}
            h1 {{ color: #4ecca3; }}
            .code-box {{
                background: #0f0f1a;
                padding: 20px;
                border-radius: 8px;
                overflow-x: auto;
                border: 1px solid #333;
            }}
            pre {{ margin: 0; white-space: pre-wrap; color: #ccc; }}
            .copy-btn {{
                background: #5865F2;
                color: white;
                border: none;
                padding: 10px 20px;
                border-radius: 5px;
                cursor: pointer;
                margin-top: 15px;
            }}
            .copy-btn:hover {{ background: #4752c4; }}
            .instructions {{
                background: rgba(78, 204, 163, 0.1);
                border: 1px solid #4ecca3;
                padding: 20px;
                border-radius: 8px;
                margin: 20px 0;
            }}
            .instructions h3 {{ color: #4ecca3; margin-top: 0; }}
            .instructions ol {{ padding-left: 20px; }}
            .instructions li {{ margin: 10px 0; }}
        </style>
    </head>
    <body>
        <h1>Shopify Integration Script</h1>

        <div class="instructions">
            <h3>Setup Instructions:</h3>
            <ol>
                <li>Go to your Shopify Admin → Online Store → Themes</li>
                <li>Click "Edit code" on your current theme</li>
                <li>Open <code>theme.liquid</code></li>
                <li>Paste this script just before <code>&lt;/head&gt;</code></li>
                <li>Save the file</li>
            </ol>
        </div>

        <div class="code-box">
            <pre id="script-code">{script.replace('<', '&lt;').replace('>', '&gt;')}</pre>
        </div>
        <button class="copy-btn" onclick="copyScript()">Copy Script</button>

        <div class="instructions" style="margin-top: 30px;">
            <h3>How It Works:</h3>
            <ol>
                <li>Customer clicks your Discord link → Authorizes with Discord</li>
                <li>Redirected to your store with <code>?did=DISCORD_ID</code></li>
                <li>Script saves Discord ID to localStorage</li>
                <li>When they checkout, Discord ID is attached to the order</li>
                <li>Webhook receives order → License created → Role assigned</li>
            </ol>
        </div>

        <script>
            function copyScript() {{
                const script = `{script}`;
                navigator.clipboard.writeText(script).then(function() {{
                    alert('Script copied to clipboard!');
                }});
            }}
        </script>
    </body>
    </html>
    """)


# ==================== DISCORD OAUTH (Link Shopify Purchase) ====================

DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
DISCORD_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI", "")
APP_URL = os.getenv("APP_URL", "")

# In-memory state storage (for OAuth security) - in production use Redis
_oauth_states = {}


STORE_URL = os.getenv("STORE_URL", "https://saintservice.store/")

# Store linked Discord accounts (email -> discord_id)
# In production, this should be in the database
_linked_accounts = {}


@app.get("/", response_class=HTMLResponse)
async def landing_page():
    """Landing page with Link Discord button - customers must link before buying."""
    return HTMLResponse(f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Link Your Discord</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;600;700&display=swap" rel="stylesheet">
        <style>
            * {{ margin: 0; padding: 0; box-sizing: border-box; }}
            body {{
                font-family: 'Poppins', sans-serif;
                background: #0a0a0f;
                color: #fff;
                min-height: 100vh;
                display: flex;
                flex-direction: column;
                align-items: center;
                justify-content: center;
                padding: 20px;
                position: relative;
                overflow: hidden;
            }}
            /* Stars background */
            .stars {{
                position: fixed;
                top: 0;
                left: 0;
                width: 100%;
                height: 100%;
                pointer-events: none;
                background: radial-gradient(ellipse at bottom, #1a1a2e 0%, #0a0a0f 100%);
            }}
            .stars::after {{
                content: '';
                position: absolute;
                top: 0;
                left: 0;
                width: 100%;
                height: 100%;
                background-image:
                    radial-gradient(2px 2px at 20px 30px, #eee, transparent),
                    radial-gradient(2px 2px at 40px 70px, rgba(255,255,255,0.8), transparent),
                    radial-gradient(1px 1px at 90px 40px, #fff, transparent),
                    radial-gradient(2px 2px at 160px 120px, rgba(255,255,255,0.9), transparent),
                    radial-gradient(1px 1px at 230px 80px, #fff, transparent),
                    radial-gradient(2px 2px at 300px 150px, rgba(255,255,255,0.7), transparent),
                    radial-gradient(1px 1px at 350px 50px, #fff, transparent),
                    radial-gradient(2px 2px at 420px 180px, rgba(255,255,255,0.8), transparent),
                    radial-gradient(1px 1px at 500px 90px, #fff, transparent),
                    radial-gradient(2px 2px at 580px 130px, rgba(255,255,255,0.6), transparent);
                background-size: 600px 200px;
                animation: twinkle 5s ease-in-out infinite;
            }}
            @keyframes twinkle {{
                0%, 100% {{ opacity: 1; }}
                50% {{ opacity: 0.5; }}
            }}
            .container {{
                position: relative;
                z-index: 1;
                text-align: center;
                max-width: 600px;
            }}
            h1 {{
                font-size: 2.8rem;
                font-weight: 700;
                margin-bottom: 20px;
                font-style: italic;
            }}
            .subtitle {{
                font-size: 1rem;
                color: #ccc;
                margin-bottom: 10px;
                line-height: 1.6;
            }}
            .privacy {{
                font-size: 0.9rem;
                color: #888;
                margin-bottom: 50px;
            }}
            .login-section {{
                margin-top: 40px;
            }}
            .login-title {{
                font-size: 1.8rem;
                margin-bottom: 25px;
            }}
            .discord-btn {{
                display: inline-flex;
                align-items: center;
                gap: 12px;
                background: #5865F2;
                color: #fff;
                padding: 15px 40px;
                border-radius: 8px;
                text-decoration: none;
                font-size: 1.1rem;
                font-weight: 600;
                transition: all 0.3s ease;
                border: none;
                cursor: pointer;
            }}
            .discord-btn:hover {{
                background: #4752c4;
                transform: translateY(-2px);
                box-shadow: 0 10px 30px rgba(88, 101, 242, 0.4);
            }}
            .discord-btn svg {{
                width: 28px;
                height: 28px;
            }}
            .already-linked {{
                margin-top: 30px;
                padding: 15px;
                background: rgba(76, 175, 80, 0.1);
                border: 1px solid #4CAF50;
                border-radius: 8px;
                display: none;
            }}
        </style>
    </head>
    <body>
        <div class="stars"></div>
        <div class="container">
            <h1>Why Must I Link My Discord?</h1>
            <p class="subtitle">
                Linking your Discord Account allows you to gain access instantly to our server roles,
                allowing you to access your product instantly!
            </p>
            <p class="privacy">We do not collect any personal information.</p>

            <div class="login-section">
                <h2 class="login-title">Login</h2>
                <a href="/auth/start" class="discord-btn">
                    <svg viewBox="0 0 24 24" fill="currentColor">
                        <path d="M20.317 4.37a19.791 19.791 0 0 0-4.885-1.515a.074.074 0 0 0-.079.037c-.21.375-.444.864-.608 1.25a18.27 18.27 0 0 0-5.487 0a12.64 12.64 0 0 0-.617-1.25a.077.077 0 0 0-.079-.037A19.736 19.736 0 0 0 3.677 4.37a.07.07 0 0 0-.032.027C.533 9.046-.32 13.58.099 18.057a.082.082 0 0 0 .031.057a19.9 19.9 0 0 0 5.993 3.03a.078.078 0 0 0 .084-.028a14.09 14.09 0 0 0 1.226-1.994a.076.076 0 0 0-.041-.106a13.107 13.107 0 0 1-1.872-.892a.077.077 0 0 1-.008-.128a10.2 10.2 0 0 0 .372-.292a.074.074 0 0 1 .077-.01c3.928 1.793 8.18 1.793 12.062 0a.074.074 0 0 1 .078.01c.12.098.246.198.373.292a.077.077 0 0 1-.006.127a12.299 12.299 0 0 1-1.873.892a.077.077 0 0 0-.041.107c.36.698.772 1.362 1.225 1.993a.076.076 0 0 0 .084.028a19.839 19.839 0 0 0 6.002-3.03a.077.077 0 0 0 .032-.054c.5-5.177-.838-9.674-3.549-13.66a.061.061 0 0 0-.031-.03zM8.02 15.33c-1.183 0-2.157-1.085-2.157-2.419c0-1.333.956-2.419 2.157-2.419c1.21 0 2.176 1.096 2.157 2.42c0 1.333-.956 2.418-2.157 2.418zm7.975 0c-1.183 0-2.157-1.085-2.157-2.419c0-1.333.955-2.419 2.157-2.419c1.21 0 2.176 1.096 2.157 2.42c0 1.333-.946 2.418-2.157 2.418z"/>
                    </svg>
                    Link Discord
                </a>
            </div>
        </div>
    </body>
    </html>
    """)


@app.get("/auth/start")
async def start_discord_oauth():
    """Start Discord OAuth flow from the landing page."""
    if not DISCORD_CLIENT_ID or not DISCORD_REDIRECT_URI:
        return HTMLResponse("""
            <html><body style="font-family: Arial; padding: 40px; text-align: center; background: #0a0a0f; color: #fff;">
                <h1>Configuration Error</h1>
                <p>Discord OAuth is not configured. Please contact support.</p>
            </body></html>
        """, status_code=500)

    # Generate state token for CSRF protection
    state = secrets.token_urlsafe(32)
    _oauth_states[state] = {
        "type": "pre_purchase",
        "created_at": datetime.utcnow()
    }

    # Clean old states (older than 10 minutes)
    cutoff = datetime.utcnow() - timedelta(minutes=10)
    expired = [k for k, v in _oauth_states.items() if v["created_at"] < cutoff]
    for k in expired:
        del _oauth_states[k]

    # Build Discord OAuth URL - request email scope too
    params = {
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": DISCORD_REDIRECT_URI,
        "response_type": "code",
        "scope": "identify email",
        "state": state
    }
    oauth_url = f"https://discord.com/api/oauth2/authorize?{urllib.parse.urlencode(params)}"

    return RedirectResponse(url=oauth_url)


@app.get("/link")
async def start_discord_link(order: str = None, email: str = None):
    """
    Start Discord OAuth flow to link a Shopify purchase (post-purchase).
    User clicks this link from their order confirmation email.
    """
    if not DISCORD_CLIENT_ID or not DISCORD_REDIRECT_URI:
        return HTMLResponse("""
            <html><body style="font-family: Arial; padding: 40px; text-align: center;">
                <h1>Configuration Error</h1>
                <p>Discord OAuth is not configured. Please contact support.</p>
            </body></html>
        """, status_code=500)

    # If no email provided, redirect to main landing page
    if not order and not email:
        return RedirectResponse(url="/")

    # Generate state token for CSRF protection
    state = secrets.token_urlsafe(32)
    _oauth_states[state] = {
        "type": "post_purchase",
        "order": order,
        "email": email.lower().strip() if email else None,
        "created_at": datetime.utcnow()
    }

    # Clean old states (older than 10 minutes)
    cutoff = datetime.utcnow() - timedelta(minutes=10)
    expired = [k for k, v in _oauth_states.items() if v["created_at"] < cutoff]
    for k in expired:
        del _oauth_states[k]

    # Build Discord OAuth URL
    params = {
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": DISCORD_REDIRECT_URI,
        "response_type": "code",
        "scope": "identify email",
        "state": state
    }
    oauth_url = f"https://discord.com/api/oauth2/authorize?{urllib.parse.urlencode(params)}"

    return RedirectResponse(url=oauth_url)


@app.get("/auth/callback")
async def discord_oauth_callback(code: str = None, state: str = None, error: str = None):
    """
    Discord OAuth callback - receives the auth code and links the account.
    Supports both stateful (from our landing page) and stateless (direct Discord link) flows.
    """
    if error:
        return HTMLResponse(f"""
            <html><body style="font-family: Arial; padding: 40px; text-align: center; background: #0a0a0f; color: #fff;">
                <h1>Authorization Cancelled</h1>
                <p>You cancelled the Discord authorization.</p>
                <p><a href="{STORE_URL}">Go to Store</a></p>
            </body></html>
        """)

    if not code:
        return HTMLResponse("""
            <html><body style="font-family: Arial; padding: 40px; text-align: center; background: #0a0a0f; color: #fff;">
                <h1>Invalid Request</h1>
                <p>Missing authorization code. Please try again.</p>
            </body></html>
        """, status_code=400)

    # Check for state (stateful flow from our pages)
    # If no state, it's a direct Discord link (stateless) - that's fine
    state_data = {}
    if state and state in _oauth_states:
        state_data = _oauth_states.pop(state)

    flow_type = state_data.get("type", "direct")  # Default to direct link flow
    order_id = state_data.get("order")
    provided_email = state_data.get("email")

    # Exchange code for access token
    async with aiohttp.ClientSession() as session:
        token_data = {
            "client_id": DISCORD_CLIENT_ID,
            "client_secret": DISCORD_CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": DISCORD_REDIRECT_URI
        }
        async with session.post("https://discord.com/api/oauth2/token", data=token_data) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                print(f"Discord token error: {error_text}")
                return HTMLResponse("""
                    <html><body style="font-family: Arial; padding: 40px; text-align: center; background: #0a0a0f; color: #fff;">
                        <h1>Authorization Failed</h1>
                        <p>Could not verify your Discord account. Please try again.</p>
                    </body></html>
                """, status_code=400)
            token_json = await resp.json()
            access_token = token_json.get("access_token")

        # Get user info
        headers = {"Authorization": f"Bearer {access_token}"}
        async with session.get("https://discord.com/api/users/@me", headers=headers) as resp:
            if resp.status != 200:
                return HTMLResponse("""
                    <html><body style="font-family: Arial; padding: 40px; text-align: center; background: #0a0a0f; color: #fff;">
                        <h1>Failed to Get User Info</h1>
                        <p>Could not retrieve your Discord information. Please try again.</p>
                    </body></html>
                """, status_code=400)
            user_json = await resp.json()
            discord_id = user_json.get("id")
            discord_name = user_json.get("username")
            discord_email = user_json.get("email")  # From email scope

    if not discord_id:
        return HTMLResponse("""
            <html><body style="font-family: Arial; padding: 40px; text-align: center; background: #0a0a0f; color: #fff;">
                <h1>Error</h1>
                <p>Could not get your Discord ID. Please try again.</p>
            </body></html>
        """, status_code=400)

    # PRE-PURCHASE / DIRECT FLOW: Redirect to store with Discord ID in URL
    if flow_type in ("pre_purchase", "direct"):
        # Redirect to store with Discord ID as hash fragment (not query param)
        # Hash fragments survive Shopify redirects since they're client-side only
        redirect_url = f"{STORE_URL}#did={discord_id}&dname={urllib.parse.quote(discord_name or 'User')}"
        print(f"Discord linked: {discord_id} ({discord_name}) - redirecting to {redirect_url}")

        # Show success page with auto-redirect
        return HTMLResponse(f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Discord Linked!</title>
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;600;700&display=swap" rel="stylesheet">
            <style>
                body {{
                    font-family: 'Poppins', sans-serif;
                    background: #0a0a0f;
                    color: #fff;
                    min-height: 100vh;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    padding: 20px;
                }}
                .container {{
                    text-align: center;
                    max-width: 500px;
                    background: rgba(255,255,255,0.05);
                    padding: 40px;
                    border-radius: 15px;
                }}
                .checkmark {{
                    font-size: 4rem;
                    margin-bottom: 20px;
                }}
                h1 {{
                    color: #4ecca3;
                    margin-bottom: 15px;
                }}
                .info {{
                    background: rgba(78, 204, 163, 0.1);
                    border: 1px solid #4ecca3;
                    padding: 15px;
                    border-radius: 8px;
                    margin: 20px 0;
                }}
                .shop-btn {{
                    display: inline-block;
                    background: #5865F2;
                    color: #fff;
                    padding: 15px 40px;
                    border-radius: 8px;
                    text-decoration: none;
                    font-size: 1.1rem;
                    font-weight: 600;
                    margin-top: 20px;
                    transition: all 0.3s ease;
                }}
                .shop-btn:hover {{
                    background: #4752c4;
                    transform: translateY(-2px);
                }}
                .redirect-msg {{
                    color: #888;
                    font-size: 0.9rem;
                    margin-top: 20px;
                }}
            </style>
            <script>
                // Auto-redirect after 2 seconds
                setTimeout(function() {{
                    window.location.href = "{redirect_url}";
                }}, 2000);
            </script>
        </head>
        <body>
            <div class="container">
                <div class="checkmark">✓</div>
                <h1>Discord Linked!</h1>
                <p>Welcome, <strong>{discord_name}</strong>!</p>
                <div class="info">
                    <p>Your Discord account has been linked.</p>
                    <p>You'll be redirected to the store automatically.</p>
                </div>
                <a href="{redirect_url}" class="shop-btn">Continue to Store →</a>
                <p class="redirect-msg">Redirecting in 2 seconds...</p>
            </div>
        </body>
        </html>
        """)

    # POST-PURCHASE FLOW: Find pending order and create license
    email = provided_email or discord_email
    try:
        from database import get_pending_order_by_email, claim_pending_order, add_license
        from license_crypto import generate_license_key

        pending = await get_pending_order_by_email(email) if email else None

        if not pending:
            return HTMLResponse(f"""
                <html><body style="font-family: Arial; padding: 40px; text-align: center; background: #0a0a0f; color: #fff;">
                    <h1>No Order Found</h1>
                    <p>No pending order found for <strong>{email}</strong>.</p>
                    <p>If you already linked your account, you're all set!</p>
                    <p>Otherwise, please contact support.</p>
                </body></html>
            """)

        product = pending["product"]
        days = pending["days"]
        order_number = pending.get("order_number", "Unknown")

        # Generate license
        expires_at = datetime.utcnow() + timedelta(days=days)
        license_key, _ = generate_license_key(SECRET_KEY, discord_id, days, discord_name)

        # Add to database
        await add_license(license_key, discord_id, discord_name, expires_at, product)

        # Mark as claimed
        await claim_pending_order(pending["id"], discord_id)

        prod_name = "Saint's Gen" if product == "saints-gen" else "Saint's Shot"

        return HTMLResponse(f"""
            <html>
            <head>
                <style>
                    body {{ font-family: Arial, sans-serif; padding: 40px; text-align: center; background: #1a1a2e; color: #eee; }}
                    .success {{ background: #16213e; border-radius: 10px; padding: 30px; max-width: 500px; margin: 0 auto; }}
                    h1 {{ color: #4ecca3; }}
                    .info {{ background: #0f3460; padding: 15px; border-radius: 5px; margin: 20px 0; }}
                    .highlight {{ color: #4ecca3; font-weight: bold; }}
                </style>
            </head>
            <body>
                <div class="success">
                    <h1>✓ Account Linked!</h1>
                    <p>Your Discord account has been linked to your purchase.</p>
                    <div class="info">
                        <p><strong>Product:</strong> <span class="highlight">{prod_name}</span></p>
                        <p><strong>Order:</strong> #{order_number}</p>
                        <p><strong>Discord:</strong> {discord_name}</p>
                        <p><strong>Expires:</strong> {expires_at.strftime("%B %d, %Y")}</p>
                    </div>
                    <p>You can now open the app and login with your Discord account!</p>
                    <p style="margin-top: 30px; color: #888;">You can close this window.</p>
                </div>
            </body>
            </html>
        """)

    except Exception as e:
        print(f"Error linking account: {e}")
        traceback.print_exc()
        return HTMLResponse(f"""
            <html><body style="font-family: Arial; padding: 40px; text-align: center;">
                <h1>Error</h1>
                <p>An error occurred while linking your account. Please contact support.</p>
                <p style="color: #888; font-size: 12px;">{str(e)}</p>
            </body></html>
        """, status_code=500)


# ==================== VERSION CHECK ====================

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
