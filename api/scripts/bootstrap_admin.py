"""Creates the first super_admin user. Chicken-and-egg problem: the API
requires a valid JWT to create users, and there can be no valid JWT until at
least one user exists -- this script breaks the cycle by connecting directly
to the database with a bypass session, exactly as an operator with database
access would (it is never exposed as an HTTP endpoint).

Usage:
    python scripts/bootstrap_admin.py --email admin@example.com --password ...

Idempotent: if the email already exists it does nothing (neither overwrites
nor fails loudly) -- safe to run more than once by accident.
"""
import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg  # noqa: E402

from app.security import hash_password  # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", default=None, help="if omitted, prompted interactively (stays out of the shell history)")
    args = ap.parse_args()

    password = args.password or getpass.getpass("Password for the new super_admin: ")
    if len(password) < 8:
        print("password must be at least 8 characters", file=sys.stderr)
        sys.exit(1)

    conninfo = (
        f"host={os.environ.get('PGHOST', '127.0.0.1')} "
        f"port={os.environ.get('PGPORT', '55432')} "
        f"dbname={os.environ.get('PGDATABASE', 'openmdvr')} "
        f"user={os.environ.get('PGUSER', 'app_user')} "
        f"password={os.environ['APP_USER_PASSWORD']}"
    )

    async with await psycopg.AsyncConnection.connect(conninfo) as conn:
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.bypass_rls', 'true', true)")
            existing = await (
                await conn.execute("SELECT id FROM users WHERE email = %s", (args.email,))
            ).fetchone()
            if existing:
                print(f"a user with email {args.email} already exists, nothing was done")
                return
            await conn.execute(
                """INSERT INTO users (tenant_id, email, password_hash, role, is_platform_bypass)
                   VALUES (NULL, %s, %s, 'super_admin', true)""",
                (args.email, hash_password(password)),
            )
    print(f"super_admin {args.email} created")


if __name__ == "__main__":
    asyncio.run(main())
