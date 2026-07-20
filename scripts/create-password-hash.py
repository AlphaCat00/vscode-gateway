#!/usr/bin/env python3
"""Create an Argon2 password hash for the gateway."""

import sys
from getpass import getpass

from vscode_gateway.auth import hash_password


def main() -> None:
    path = "state/password.hash"
    print(f"Password hash will be written to: {path}")
    pw1 = getpass("Enter password: ")
    pw2 = getpass("Confirm password: ")

    if pw1 != pw2:
        print("Passwords do not match.", file=sys.stderr)
        sys.exit(1)

    if len(pw1) < 8:
        print("Password too short (minimum 8 characters).", file=sys.stderr)
        sys.exit(1)

    hashed = hash_password(pw1)

    import os
    from pathlib import Path

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(hashed, encoding="utf-8")
    os.chmod(p, 0o600)
    print(f"Password hash written to {path}")


if __name__ == "__main__":
    main()
