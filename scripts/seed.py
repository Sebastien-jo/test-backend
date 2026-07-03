"""Idempotent seed: two organizations, each with one user.

Runs automatically at container startup (after migrations)
"""

import asyncio

from sqlalchemy import select

from app.core.db import SessionFactory
from app.core.security import hash_password
from app.models import Organization, User

# (organization name, user email, password)
SEED_DATA = [
    ("Acme", "alice@acme.test", "password123"),
    ("Globex", "bob@globex.test", "password123"),
]


async def seed() -> None:
    created = 0
    async with SessionFactory() as session:
        for org_name, email, password in SEED_DATA:
            existing = await session.scalar(select(User).where(User.email == email))
            if existing is not None:
                continue

            org = await session.scalar(select(Organization).where(Organization.name == org_name))
            if org is None:
                org = Organization(name=org_name)
                session.add(org)
                await session.flush()  # assign org.id before creating the user

            session.add(
                User(
                    organization_id=org.id,
                    email=email,
                    hashed_password=hash_password(password),
                )
            )
            created += 1

        await session.commit()

    print(f"Seed complete: {created} user(s) created, {len(SEED_DATA) - created} already present.")


if __name__ == "__main__":
    asyncio.run(seed())
