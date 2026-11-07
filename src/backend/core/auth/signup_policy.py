"""Who may create an account on this instance (D-037).

Photon's trust model (D-035) is "every account is a trusted colleague", so
account creation is the real perimeter. Configured by env vars:

  PHOTON_SIGNUP                 open | invite | closed     (default: open)
      open    anyone who reaches the login page may sign up (subject to the
              allow-list below);
      invite  password sign-up needs PHOTON_SIGNUP_INVITE_CODE; Google
              sign-up still works for allow-listed emails;
      closed  no self sign-up at all; accounts come from the admin CLI
              (`python -m src.backend.cli user add ...`).
  PHOTON_SIGNUP_ALLOWED_DOMAINS comma list, e.g. "iitm.ac.in,lab.org"
  PHOTON_SIGNUP_ALLOWED_EMAILS  comma list of individual addresses
      If either list is set, a new account's email must match one of them.
      Only Google proves the address (email_verified); a password sign-up
      can type any address, which is why "invite" exists.
  PHOTON_SIGNUP_INVITE_CODE     shared code for password sign-up in invite mode.

Existing accounts can always log in; the policy only gates new ones.
"""

from __future__ import annotations

import hmac
import os
from dataclasses import dataclass

from ...config import config  # noqa: F401  (loads .env before from_env reads it)

MODES = ("open", "invite", "closed")


def _csv(name: str) -> frozenset[str]:
    return frozenset(x.strip().lower().lstrip("@") for x in os.getenv(name, "").split(",") if x.strip())


@dataclass(frozen=True)
class SignupPolicy:
    mode: str
    domains: frozenset[str]
    emails: frozenset[str]
    invite_code: str

    @classmethod
    def from_env(cls) -> "SignupPolicy":
        mode = os.getenv("PHOTON_SIGNUP", "open").strip().lower()
        if mode not in MODES:
            raise ValueError(f"PHOTON_SIGNUP must be one of {MODES}, got {mode!r}")
        code = os.getenv("PHOTON_SIGNUP_INVITE_CODE", "")
        if mode == "invite" and not code:
            raise ValueError("PHOTON_SIGNUP=invite needs PHOTON_SIGNUP_INVITE_CODE")
        return cls(mode, _csv("PHOTON_SIGNUP_ALLOWED_DOMAINS"),
                   _csv("PHOTON_SIGNUP_ALLOWED_EMAILS"), code)

    @property
    def restricted(self) -> bool:
        return bool(self.domains or self.emails)

    def email_allowed(self, email: str) -> bool:
        if not self.restricted:
            return True
        email = email.strip().lower()
        return email in self.emails or email.rpartition("@")[2] in self.domains

    def check_password_signup(self, email: str, invite_code: str | None) -> str | None:
        """None if allowed, else the user-facing reason."""
        if self.mode == "closed":
            return "Sign-up is closed on this instance. Ask an admin for an account."
        if self.mode == "invite" and not hmac.compare_digest(
                (invite_code or "").encode(), self.invite_code.encode()):
            return "A valid invite code is required to sign up."
        if not self.email_allowed(email):
            return "This email address is not allowed to sign up here."
        return None

    def check_google_signup(self, email: str, email_verified: bool) -> str | None:
        if self.mode == "closed":
            return "Sign-up is closed on this instance. Ask an admin for an account."
        if self.restricted and not email_verified:
            return "Your Google email address is not verified."
        if not self.email_allowed(email):
            return "This Google account is not allowed to sign up here."
        return None

    def describe(self) -> dict:
        """For the login page (no secrets)."""
        return {"signup_mode": self.mode, "invite_required": self.mode == "invite",
                "signup_restricted": self.restricted}

    def warning(self) -> str | None:
        if self.mode == "open" and not self.restricted:
            return ("PHOTON_SIGNUP=open with no allow-list: anyone who can reach this "
                    "server can create an account and run code in the sandbox.")
        return None


policy = SignupPolicy.from_env()
