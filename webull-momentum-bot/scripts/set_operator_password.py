#!/usr/bin/env python3
"""
Sets a real password for an existing user -- specifically, the "user 1"
account seeded by migrations/versions/0002_cutover_backfill_user_id.py's
production cutover, which starts with an unusable placeholder
password_hash on purpose (never embed a real password in a migration
file). Run this once, interactively, right after that migration.

Usage: python scripts/set_operator_password.py you@example.com
Prompts for the new password twice (not echoed, not passed as an argv/
env var, so it never ends up in shell history or process listings).
"""
from __future__ import annotations

import getpass
import sys

from webull_bot.auth.security import hash_password
from webull_bot.config import get_settings
from webull_bot.db.models import User
from webull_bot.db.session import get_session_factory

_MIN_PASSWORD_LENGTH = 8


def main() -> None:
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    email = sys.argv[1].strip().lower()

    password = getpass.getpass("New password: ")
    if len(password) < _MIN_PASSWORD_LENGTH:
        print(f"Password must be at least {_MIN_PASSWORD_LENGTH} characters.")
        sys.exit(1)
    if getpass.getpass("Confirm password: ") != password:
        print("Passwords did not match.")
        sys.exit(1)

    session_factory = get_session_factory(get_settings())
    with session_factory() as session:
        user = session.query(User).filter(User.email == email).one_or_none()
        if user is None:
            print(f"No user found with email {email!r}.")
            sys.exit(1)
        user.password_hash = hash_password(password)
        session.commit()

    print(f"Password set for {email}.")


if __name__ == "__main__":
    main()
