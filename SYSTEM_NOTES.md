# SAINT's Gen/Shot License System - Claude Notes

## Deployment (DigitalOcean App Platform)
**URL:** `https://saints-gen-bot-ohez9.ondigitalocean.app`

**To deploy updates:**
```bash
cd "C:\Users\seven\Desktop\New folder\BEST WORKING VERSION\discord_bot"
git add <files>
git commit -m "message"
git push
```
Auto-deploys from GitHub - changes go live within 1-2 minutes.

**GitHub Repo:** `https://github.com/boogz0217/saints-gen-bot.git`

## Old Railway URL (deprecated)
`worker-production-767a.up.railway.app`

## Products & Roles
| Product | Days | Role Variable |
|---------|------|---------------|
| Saint's Gen | 30 | `SUBSCRIBER_ROLE_ID` |
| Saint's Shot Weekly | 7 | `SAINTS_SHOT_ROLE_ID` |
| Saint's Shot Monthly | 30 | `SAINTS_SHOT_ROLE_ID` |

## How It Works
1. Customer buys on Shopify
2. Webhook sends to `/shopify/webhook`
3. Purchase saved to `purchases` table (email, product, days)
4. Customer uses `/redeem email@example.com` in Discord
5. Bot looks up by email, creates/extends license, assigns role
6. Redemption logged to Discord channel

## Key Rules
- One license per product per user (Saint's Gen + Saint's Shot separate)
- Repurchasing adds days to existing license
- Each purchase can only be redeemed once (marked redeemed=1)
- License tied to Discord ID (user logs in with Discord, no visible key)

## Discord Channels
- **Redemption Log:** `1290509478445322292`
- **Saint's Gen Instructions:** `1467010934613737516`
- **Saint's Shot Instructions:** `1469757937382723727`
- **Guild ID:** `1290387028185448469`

## Key Files
- `bot.py` - Discord bot, /redeem command, role assignment
- `api.py` - FastAPI server, Shopify webhook
- `database.py` - PostgreSQL functions (purchases table, licenses table)
- `config.py` - Environment variables

## Database Tables
- `licenses` - Active licenses (discord_id, product, expires_at, hwid)
- `purchases` - Shopify purchases awaiting redemption (email, product, days, redeemed)

## Shopify Webhook
- URL: `https://worker-production-767a.up.railway.app/shopify/webhook`
- Event: Order payment
- Secret stored in `SHOPIFY_WEBHOOK_SECRET` env var

## User Commands
- `/redeem email@example.com` - Redeem purchase
- `/status` - Check license status
- `/balance` - Show balance publicly

## Admin Commands
- `/generate user days product` - Give user access
- `/revoke` - Revoke license
- `/extend` - Add/remove days
- `/list` - List active licenses
- `/reset-hwid` - Reset hardware binding
