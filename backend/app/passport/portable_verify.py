"""Portable Passport verifier: ``python -m backend.app.passport.portable_verify``.

Runs on any OS with only ``cryptography`` and ``pydantic`` installed (no Typer,
no Windows key storage, no local trust registry). Trust comes solely from the
``--fingerprint`` pin, so this is the entry point for CI runners and the
``verify-passport`` GitHub Action. Exit codes and JSON match ``sentinel verify``:
VALID 0, INVALID 1, INDETERMINATE 2, usage error 3.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import NoReturn, Sequence

from backend.app.passport.es256 import normalize_fingerprint
from backend.app.passport.verify import verify_bundle

EXIT_CODES = {"VALID": 0, "INVALID": 1, "INDETERMINATE": 2}
USAGE_EXIT = 3


def _emit(value: object, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, sort_keys=True, separators=(",", ":")))
    else:
        print(json.dumps(value, sort_keys=True, indent=2))


class _UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    """argparse exits 2 on bad syntax; 2 means INDETERMINATE here, so raise instead."""

    def error(self, message: str) -> NoReturn:
        raise _UsageError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="python -m backend.app.passport.portable_verify",
        description="Verify a .sentinel Passport bundle against a pinned signer fingerprint.",
    )
    parser.add_argument("bundle", metavar="BUNDLE", type=Path, help="Path to the .sentinel bundle.")
    parser.add_argument("--fingerprint", required=True,
                        help="Expected signer fingerprint (the trust root).")
    parser.add_argument("--json", action="store_true", help="Print compact JSON.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in args_list
    try:
        args = _parser().parse_args(args_list)
        if not args.bundle.is_file():
            raise _UsageError("Provide an existing bundle path.")
        try:
            pinned = normalize_fingerprint(args.fingerprint)
        except ValueError as exc:
            raise _UsageError(str(exc)) from exc
    except _UsageError as exc:
        _emit({"verdict": "USAGE_ERROR", "reason": str(exc)}, as_json=as_json)
        return USAGE_EXIT
    result = verify_bundle(args.bundle, expected_fingerprint=pinned, use_registry=False)
    _emit(asdict(result), as_json=as_json)
    return EXIT_CODES[result.verdict]


if __name__ == "__main__":
    sys.exit(main())
