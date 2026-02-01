"""Async SQLite database operations for license management."""
import aiosqlite
from datetime import datetime
from typing import Optional, List, Dict
from config import DATABASE_PATH


async def init_db():
    """Initialize the database and create tables if they don't exist."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS licenses (
                license_key TEXT PRIMARY KEY,
                discord_id TEXT NOT NULL,
                discord_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL,
                revoked INTEGER DEFAULT 0,
                hwid TEXT
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_discord_id ON licenses(discord_id)
        """)
        await db.commit()


async def add_license(
    license_key: str,
    discord_id: str,
    discord_name: str,
    expires_at: datetime
) -> bool:
    """Add a new license to the database."""
    try:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute(
                """INSERT INTO licenses (license_key, discord_id, discord_name, expires_at)
                   VALUES (?, ?, ?, ?)""",
                (license_key, discord_id, discord_name, expires_at.isoformat())
            )
            await db.commit()
        return True
    except aiosqlite.IntegrityError:
        return False  # Key already exists


async def get_license_by_key(license_key: str) -> Optional[Dict]:
    """Get license info by key."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM licenses WHERE license_key = ?",
            (license_key,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return dict(row)
    return None


async def get_license_by_user(discord_id: str) -> Optional[Dict]:
    """Get the most recent active license for a user."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT * FROM licenses
               WHERE discord_id = ? AND revoked = 0
               ORDER BY expires_at DESC LIMIT 1""",
            (discord_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return dict(row)
    return None


async def get_all_licenses_for_user(discord_id: str) -> List[Dict]:
    """Get all licenses for a user."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM licenses WHERE discord_id = ? ORDER BY created_at DESC",
            (discord_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def revoke_license(license_key: str) -> bool:
    """Revoke a license by key."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            "UPDATE licenses SET revoked = 1 WHERE license_key = ?",
            (license_key,)
        )
        await db.commit()
        return cursor.rowcount > 0


async def revoke_user_licenses(discord_id: str) -> int:
    """Revoke all licenses for a user. Returns count of revoked licenses."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            "UPDATE licenses SET revoked = 1 WHERE discord_id = ? AND revoked = 0",
            (discord_id,)
        )
        await db.commit()
        return cursor.rowcount


async def delete_license(license_key: str) -> bool:
    """Permanently delete a license by key."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM licenses WHERE license_key = ?",
            (license_key,)
        )
        await db.commit()
        return cursor.rowcount > 0


async def delete_user_licenses(discord_id: str) -> int:
    """Permanently delete all licenses for a user. Returns count of deleted licenses."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM licenses WHERE discord_id = ?",
            (discord_id,)
        )
        await db.commit()
        return cursor.rowcount


async def get_all_active_licenses() -> List[Dict]:
    """Get all active (non-revoked, non-expired) licenses."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        now = datetime.utcnow().isoformat()
        async with db.execute(
            """SELECT * FROM licenses
               WHERE revoked = 0 AND expires_at > ?
               ORDER BY expires_at ASC""",
            (now,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]


async def get_license_stats() -> Dict:
    """Get license statistics."""
    async with aiosqlite.connect(DATABASE_PATH) as db:
        now = datetime.utcnow().isoformat()

        # Total licenses
        async with db.execute("SELECT COUNT(*) FROM licenses") as cursor:
            total = (await cursor.fetchone())[0]

        # Active licenses
        async with db.execute(
            "SELECT COUNT(*) FROM licenses WHERE revoked = 0 AND expires_at > ?",
            (now,)
        ) as cursor:
            active = (await cursor.fetchone())[0]

        # Revoked licenses
        async with db.execute(
            "SELECT COUNT(*) FROM licenses WHERE revoked = 1"
        ) as cursor:
            revoked = (await cursor.fetchone())[0]

        # Expired licenses
        async with db.execute(
            "SELECT COUNT(*) FROM licenses WHERE revoked = 0 AND expires_at <= ?",
            (now,)
        ) as cursor:
            expired = (await cursor.fetchone())[0]

        return {
            "total": total,
            "active": active,
            "revoked": revoked,
            "expired": expired
        }
