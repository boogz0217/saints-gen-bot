"""Async PostgreSQL database operations for license management."""
import asyncpg
from datetime import datetime, timedelta
from typing import Optional, List, Dict
import os
import sys

# Database URL from environment (Railway provides this automatically)
DATABASE_URL = os.getenv("DATABASE_URL", "")

# Connection pool
_pool: Optional[asyncpg.Pool] = None


async def get_pool() -> asyncpg.Pool:
    """Get or create the connection pool."""
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            print("ERROR: DATABASE_URL environment variable is not set!")
            print("Please add a PostgreSQL database to your Railway project.")
            sys.exit(1)
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    return _pool


async def close_pool():
    """Close the connection pool."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


async def init_db():
    """Initialize the database and create tables if they don't exist."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS licenses (
                license_key TEXT PRIMARY KEY,
                discord_id TEXT NOT NULL,
                discord_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL,
                revoked INTEGER DEFAULT 0,
                hwid TEXT,
                expiry_notified INTEGER DEFAULT 0,
                product TEXT DEFAULT 'saints-gen'
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_discord_id ON licenses(discord_id)
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_product ON licenses(product)
        """)
        # Add columns if they don't exist (for existing databases)
        try:
            await conn.execute("ALTER TABLE licenses ADD COLUMN IF NOT EXISTS expiry_notified INTEGER DEFAULT 0")
        except:
            pass
        try:
            await conn.execute("ALTER TABLE licenses ADD COLUMN IF NOT EXISTS product TEXT DEFAULT 'saints-gen'")
        except:
            pass


async def add_license(
    license_key: str,
    discord_id: str,
    discord_name: str,
    expires_at: datetime,
    product: str = "saints-gen"
) -> bool:
    """Add a new license to the database."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO licenses (license_key, discord_id, discord_name, expires_at, product)
                   VALUES ($1, $2, $3, $4, $5)""",
                license_key, discord_id, discord_name, expires_at, product
            )
        return True
    except asyncpg.UniqueViolationError:
        return False  # Key already exists


async def get_license_by_key(license_key: str) -> Optional[Dict]:
    """Get license info by key."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM licenses WHERE license_key = $1",
            license_key
        )
        if row:
            return dict(row)
    return None


async def get_license_by_user(discord_id: str, product: str = None) -> Optional[Dict]:
    """Get the most recent active license for a user, optionally filtered by product."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if product:
            row = await conn.fetchrow(
                """SELECT * FROM licenses
                   WHERE discord_id = $1 AND revoked = 0 AND product = $2
                   ORDER BY expires_at DESC LIMIT 1""",
                discord_id, product
            )
        else:
            row = await conn.fetchrow(
                """SELECT * FROM licenses
                   WHERE discord_id = $1 AND revoked = 0
                   ORDER BY expires_at DESC LIMIT 1""",
                discord_id
            )
        if row:
            return dict(row)
    return None


async def get_all_licenses_for_user(discord_id: str) -> List[Dict]:
    """Get all licenses for a user."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM licenses WHERE discord_id = $1 ORDER BY created_at DESC",
            discord_id
        )
        return [dict(row) for row in rows]


async def revoke_license(license_key: str) -> bool:
    """Revoke a license by key."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE licenses SET revoked = 1 WHERE license_key = $1",
            license_key
        )
        return result != "UPDATE 0"


async def revoke_user_licenses(discord_id: str) -> int:
    """Revoke all licenses for a user. Returns count of revoked licenses."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE licenses SET revoked = 1 WHERE discord_id = $1 AND revoked = 0",
            discord_id
        )
        # Parse "UPDATE N" to get count
        return int(result.split()[-1]) if result else 0


async def delete_license(license_key: str) -> bool:
    """Permanently delete a license by key."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM licenses WHERE license_key = $1",
            license_key
        )
        return result != "DELETE 0"


async def delete_user_licenses(discord_id: str) -> int:
    """Permanently delete all licenses for a user. Returns count of deleted licenses."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM licenses WHERE discord_id = $1",
            discord_id
        )
        # Parse "DELETE N" to get count
        return int(result.split()[-1]) if result else 0


async def extend_license(license_key: str, days: int) -> Optional[str]:
    """Extend a license by adding days. Returns new expiry date or None if not found."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT expires_at FROM licenses WHERE license_key = $1",
            license_key
        )
        if not row:
            return None

        # Parse current expiry and add days
        current_expiry = row["expires_at"]
        if isinstance(current_expiry, str):
            current_expiry = datetime.fromisoformat(current_expiry)
        now = datetime.utcnow()

        # If already expired, extend from now; otherwise extend from current expiry
        base = max(current_expiry, now)
        new_expiry = base + timedelta(days=days)

        await conn.execute(
            "UPDATE licenses SET expires_at = $1, revoked = 0 WHERE license_key = $2",
            new_expiry, license_key
        )
        return new_expiry.isoformat()


async def extend_user_license(discord_id: str, days: int) -> Optional[str]:
    """Extend the most recent license for a user. Returns new expiry date or None."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT license_key, expires_at FROM licenses WHERE discord_id = $1 ORDER BY expires_at DESC LIMIT 1",
            discord_id
        )
        if not row:
            return None

        return await extend_license(row["license_key"], days)


async def get_all_active_licenses(product: str = None) -> List[Dict]:
    """Get all active (non-revoked, non-expired) licenses, optionally filtered by product."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        now = datetime.utcnow()
        if product:
            rows = await conn.fetch(
                """SELECT * FROM licenses
                   WHERE revoked = 0 AND expires_at > $1 AND product = $2
                   ORDER BY expires_at ASC""",
                now, product
            )
        else:
            rows = await conn.fetch(
                """SELECT * FROM licenses
                   WHERE revoked = 0 AND expires_at > $1
                   ORDER BY expires_at ASC""",
                now
            )
        return [dict(row) for row in rows]


async def get_license_stats(product: str = None) -> Dict:
    """Get license statistics, optionally filtered by product."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        now = datetime.utcnow()

        if product:
            total = await conn.fetchval(
                "SELECT COUNT(*) FROM licenses WHERE product = $1", product
            )
            active = await conn.fetchval(
                "SELECT COUNT(*) FROM licenses WHERE revoked = 0 AND expires_at > $1 AND product = $2",
                now, product
            )
            revoked = await conn.fetchval(
                "SELECT COUNT(*) FROM licenses WHERE revoked = 1 AND product = $1", product
            )
            expired = await conn.fetchval(
                "SELECT COUNT(*) FROM licenses WHERE revoked = 0 AND expires_at <= $1 AND product = $2",
                now, product
            )
        else:
            total = await conn.fetchval("SELECT COUNT(*) FROM licenses")
            active = await conn.fetchval(
                "SELECT COUNT(*) FROM licenses WHERE revoked = 0 AND expires_at > $1",
                now
            )
            revoked = await conn.fetchval(
                "SELECT COUNT(*) FROM licenses WHERE revoked = 1"
            )
            expired = await conn.fetchval(
                "SELECT COUNT(*) FROM licenses WHERE revoked = 0 AND expires_at <= $1",
                now
            )

        return {
            "total": total,
            "active": active,
            "revoked": revoked,
            "expired": expired
        }


async def reset_hwid_by_key(license_key: str) -> bool:
    """Reset hardware ID binding for a license. Returns True if found and reset."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE licenses SET hwid = NULL WHERE license_key = $1",
            license_key
        )
        return result != "UPDATE 0"


async def reset_hwid_by_user(discord_id: str) -> int:
    """Reset hardware ID binding for all licenses of a user. Returns count reset."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE licenses SET hwid = NULL WHERE discord_id = $1",
            discord_id
        )
        # Parse "UPDATE N" to get count
        return int(result.split()[-1]) if result else 0


async def get_hwid_by_key(license_key: str) -> Optional[str]:
    """Get the hardware ID bound to a license."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT hwid FROM licenses WHERE license_key = $1",
            license_key
        )
        if row:
            return row["hwid"]
    return None


async def get_newly_expired_licenses() -> List[Dict]:
    """Get licenses that expired but haven't been notified yet."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        now = datetime.utcnow()
        rows = await conn.fetch(
            """SELECT * FROM licenses
               WHERE expires_at <= $1 AND revoked = 0 AND expiry_notified = 0""",
            now
        )
        return [dict(row) for row in rows]


async def mark_expiry_notified(license_key: str) -> bool:
    """Mark a license as having been notified about expiry."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE licenses SET expiry_notified = 1 WHERE license_key = $1",
            license_key
        )
        return result != "UPDATE 0"


async def has_active_license(discord_id: str) -> bool:
    """Check if a user has any active (non-expired, non-revoked) license."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        now = datetime.utcnow()
        count = await conn.fetchval(
            """SELECT COUNT(*) FROM licenses
               WHERE discord_id = $1 AND revoked = 0 AND expires_at > $2""",
            discord_id, now
        )
        return count > 0
