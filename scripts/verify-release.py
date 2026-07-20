#!/usr/bin/env python3
"""Verify a gateway release artifact against the expected SHA-256 digest."""

import hashlib
import sys


def main() -> None:
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <file> <expected_sha256>", file=sys.stderr)
        sys.exit(1)

    path = sys.argv[1]
    expected = sys.argv[2].lower()

    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)

    actual = hasher.hexdigest()
    if actual == expected:
        print(f"OK: {path} matches {expected}")
        sys.exit(0)
    else:
        print(f"MISMATCH: {path}\n  expected: {expected}\n  actual:   {actual}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
