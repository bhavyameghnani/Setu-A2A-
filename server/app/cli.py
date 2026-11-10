"""Operator commands. `bootstrap` creates the first org, department and org_admin.

    uv run python -m app.cli bootstrap --org Acme --dept-name Operations --dept-slug ops \\
        --email admin@acme.test --name "Ada Admin"          # password: prompt or ADMIN_PASSWORD

Run once per org. After that, the admin logs in and creates everything else through the API.
"""

import argparse
import asyncio
import getpass
import os
import sys
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core import auth
from app.db.models import Department, Human, Org, Participant
from app.db.session import make_engine, make_sessionmaker


async def bootstrap_org(
    session: AsyncSession,
    *,
    org_name: str,
    dept_name: str,
    dept_slug: str,
    email: str,
    display_name: str,
    password: str,
    handle: str | None = None,
) -> uuid.UUID:
    """Create org + first department + an org_admin human. Returns the admin's participant id."""
    org = Org(name=org_name)
    session.add(org)
    await session.flush()
    dept = Department(org_id=org.id, name=dept_name, slug=dept_slug)
    session.add(dept)
    await session.flush()
    admin = Participant(
        org_id=org.id,
        department_id=dept.id,
        kind="human",
        display_name=display_name,
        handle=handle or email.split("@", 1)[0].lower(),
    )
    session.add(admin)
    await session.flush()
    session.add(
        Human(
            participant_id=admin.id,
            email=email.strip().lower(),
            password_hash=auth.hash_secret(password),
            roles=["org_admin", "dept_admin"],
        )
    )
    await session.commit()
    return admin.id


async def _run(args: argparse.Namespace) -> None:
    password = os.environ.get("ADMIN_PASSWORD") or getpass.getpass("Admin password: ")
    if len(password) < 10:
        sys.exit("password must be at least 10 characters")
    engine = make_engine(get_settings().database_url)
    try:
        async with make_sessionmaker(engine)() as session:
            pid = await bootstrap_org(
                session,
                org_name=args.org,
                dept_name=args.dept_name,
                dept_slug=args.dept_slug,
                email=args.email,
                display_name=args.name,
                password=password,
            )
        print(f"created org {args.org!r}; admin participant {pid}")
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(prog="app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("bootstrap", help="create the first org, department and org_admin")
    b.add_argument("--org", required=True)
    b.add_argument("--dept-name", required=True)
    b.add_argument("--dept-slug", required=True)
    b.add_argument("--email", required=True)
    b.add_argument("--name", required=True)
    asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    main()
