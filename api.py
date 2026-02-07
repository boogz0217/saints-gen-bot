"""
Web API for license verification.
Runs alongside the Discord bot to provide HTTP endpoints for the macro.
"""
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime
from typing import Optional
import asyncpg
import os
import traceback

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


@app.get("/health")
async def health():
    """Health check for Railway."""
    return {"status": "healthy"}
