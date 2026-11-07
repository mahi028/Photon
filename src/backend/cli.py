"""Admin CLI. Run from the repo root:

    python -m src.backend.cli user list
    python -m src.backend.cli user add <username> <email>      # prompts for password
    python -m src.backend.cli user passwd <username-or-email>  # reset password

Needed when PHOTON_SIGNUP=closed (core/auth/signup_policy.py): accounts are
created here instead of through the sign-up form. Talks to sessions.db
directly; the web app doesn't need restarting.
"""

from __future__ import annotations

import argparse
import getpass
import sys
import uuid
from datetime import datetime

from werkzeug.security import generate_password_hash

from .core.session import store

_MIN_PASSWORD_LENGTH = 8


def _read_password(args) -> str:
    if args.password_stdin:
        pw = sys.stdin.readline().rstrip("\n")
    else:
        pw = getpass.getpass("Password: ")
        if getpass.getpass("Repeat: ") != pw:
            sys.exit("Passwords don't match.")
    if len(pw) < _MIN_PASSWORD_LENGTH:
        sys.exit(f"Password must be at least {_MIN_PASSWORD_LENGTH} characters.")
    return pw


def cmd_list(args) -> None:
    users = store.list_users()
    for u in users:
        print(f"{u['username']:<20} {u['email']:<32} {u['provider'] or 'password':<9} "
              f"created {u['created_at'][:10]}  last login {u['last_login'][:10]}")
    print(f"{len(users)} user(s)")


def cmd_add(args) -> None:
    email = args.email.strip().lower()
    if store.find_user_by_username_or_email(args.username) or store.find_user_by_username_or_email(email):
        sys.exit("Username or email already in use.")
    pw = _read_password(args)
    now = datetime.utcnow().isoformat()
    store.create_user(user_id=uuid.uuid4().hex, username=args.username, email=email,
                      password_hash=generate_password_hash(pw), provider=None, provider_id=None,
                      display_name=args.display_name or args.username, avatar_url="", created_at=now)
    print(f"Created {args.username} <{email}>.")


def cmd_passwd(args) -> None:
    u = store.find_user_by_username_or_email(args.user)
    if not u:
        sys.exit("No such user.")
    store.set_user_password(u["user_id"], generate_password_hash(_read_password(args)))
    print(f"Password set for {u['username']}.")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="python -m src.backend.cli")
    sub = ap.add_subparsers(dest="group", required=True)
    user = sub.add_parser("user", help="manage accounts").add_subparsers(dest="cmd", required=True)
    user.add_parser("list").set_defaults(fn=cmd_list)
    p = user.add_parser("add")
    p.add_argument("username"); p.add_argument("email")
    p.add_argument("--display-name"); p.add_argument("--password-stdin", action="store_true")
    p.set_defaults(fn=cmd_add)
    p = user.add_parser("passwd")
    p.add_argument("user", help="username or email"); p.add_argument("--password-stdin", action="store_true")
    p.set_defaults(fn=cmd_passwd)
    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
