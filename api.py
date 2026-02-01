"""
Web API for license verification.
Runs alongside the Discord bot to provide HTTP endpoints for the macro.
"""
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import aiosqlite
from datetime import datetime
from config import DATABASE_PATH

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
async def verify_license(key: str):
    """
    Verify if a license key is valid and not revoked.

    Query params:
        key: The license key to verify

    Returns:
        {"valid": true/false, "reason": "..."}
    """
    if not key or not key.startswith("SAINT-"):
        return {"valid": False, "reason": "invalid_format"}

    try:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT revoked, expires_at FROM licenses WHERE license_key = ?",
                (key,)
            ) as cursor:
                row = await cursor.fetchone()

                if not row:
                    # Key not in database - might be valid offline key
                    # Return valid=True but with a note
                    return {"valid": True, "reason": "not_in_db"}

                # Check if revoked
                if row["revoked"]:
                    return {"valid": False, "reason": "revoked"}

                # Check if expired
                expires_at = datetime.fromisoformat(row["expires_at"])
                if expires_at < datetime.utcnow():
                    return {"valid": False, "reason": "expired"}

                return {"valid": True, "reason": "active"}

    except Exception as e:
        # On database error, fail open (allow access)
        # This prevents lockout if database is temporarily unavailable
        return {"valid": True, "reason": "db_error"}


@app.get("/health")
async def health():
    """Health check for Railway."""
    return {"status": "healthy"}
