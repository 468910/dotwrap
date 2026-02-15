#!/usr/bin/env python3
"""dotwrap: a small CLI overlay tool with providers.

CLI:
  python dotwrap.py install [provider]
  python dotwrap.py uninstall [provider]
  python dotwrap.py doctor [provider]

Config:
  aliases.toml must live next to dotwrap.py and uses:

    [providers.gh.aliases]
    dw_name = "..."  # or TOML multiline strings
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

import tomllib


EXIT_OK = 0
EXIT_ENV = 1
EXIT_INVALID = 2

PREFIX = "dw_"
DEFAULT_PROVIDER = "gh"


def _out(message: str) -> None:
    print(f"dotwrap: {message}")


def _err(message: str) -> None:
    print(f"dotwrap: {message}", file=sys.stderr)


def require_python_311() -> None:
    if sys.version_info < (3, 11):
        _err("requires Python 3.11+")
        raise SystemExit(EXIT_ENV)


def require_gh() -> None:
    if shutil.which("gh") is None:
        _err("missing required tool: gh (GitHub CLI) not found on PATH")
        raise SystemExit(EXIT_ENV)


def config_path() -> Path:
    return Path(__file__).resolve().parent / "aliases.toml"


def collapse_whitespace(value: str) -> str:
    return " ".join(value.split())


def _load_config() -> dict:
    path = config_path()
    if not path.is_file():
        _err("missing aliases.toml next to dotwrap.py")
        raise SystemExit(EXIT_ENV)

    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except Exception as exc:
        _err(f"invalid aliases.toml: {exc}")
        raise SystemExit(EXIT_ENV)


def _get_provider_aliases(data: dict, provider: str) -> dict[str, str]:
    providers = data.get("providers")
    if not isinstance(providers, dict):
        _err("aliases.toml must define [providers.<name>.aliases]")
        raise SystemExit(EXIT_ENV)

    provider_table = providers.get(provider)
    if not isinstance(provider_table, dict):
        _err(f"unknown provider in aliases.toml: {provider}")
        raise SystemExit(EXIT_ENV)

    aliases = provider_table.get("aliases")
    if not isinstance(aliases, dict) or not aliases:
        _err(f"missing or empty [providers.{provider}.aliases]")
        raise SystemExit(EXIT_ENV)

    out: dict[str, str] = {}
    for name, cmd in aliases.items():
        if not isinstance(name, str) or not name:
            _err("alias keys must be non-empty strings")
            raise SystemExit(EXIT_ENV)
        if not name.startswith(PREFIX):
            _err(f"invalid alias key (must start with {PREFIX}): {name}")
            raise SystemExit(EXIT_ENV)
        if not isinstance(cmd, str) or not cmd.strip():
            _err(f"alias command must be a non-empty string: {name}")
            raise SystemExit(EXIT_ENV)
        out[name] = collapse_whitespace(cmd)

    return out


def _run_gh(args: list[str], *, check: bool) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["gh", *args],
        text=True,
        capture_output=True,
        check=check,
    )


def _provider_supported(provider: str) -> bool:
    return provider == "gh"


def _require_provider(provider: str) -> None:
    if not _provider_supported(provider):
        _err(f"invalid provider: {provider}")
        raise SystemExit(EXIT_INVALID)


def cmd_install(provider: str) -> int:
    _require_provider(provider)
    require_gh()
    aliases = _get_provider_aliases(_load_config(), provider)

    for name in sorted(aliases):
        cmd = aliases[name]
        try:
            _run_gh(["alias", "set", "--clobber", name, cmd], check=True)
        except subprocess.CalledProcessError as exc:
            details = (exc.stderr or "").strip()
            if details:
                _err(f"gh alias set failed for {name}: {details}")
            else:
                _err(f"gh alias set failed for {name}")
            return EXIT_ENV

    return EXIT_OK


def cmd_uninstall(provider: str) -> int:
    _require_provider(provider)
    require_gh()
    aliases = _get_provider_aliases(_load_config(), provider)

    for name in sorted(aliases):
        # Requirement: ignore non-zero delete results.
        _run_gh(["alias", "delete", name], check=False)

    return EXIT_OK


def cmd_doctor(provider: str) -> int:
    _require_provider(provider)
    require_gh()

    try:
        proc = _run_gh(["alias", "list"], check=True)
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or "").strip()
        if details:
            _err(f"gh alias list failed: {details}")
        else:
            _err("gh alias list failed")
        return EXIT_ENV

    for line in (proc.stdout or "").splitlines():
        if line.lstrip().startswith(PREFIX):
            _out(line)

    return EXIT_OK


class DotwrapArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # noqa: D401
        _err(f"invalid usage: {message}")
        raise SystemExit(EXIT_INVALID)


def build_parser() -> argparse.ArgumentParser:
    parser = DotwrapArgumentParser(prog="dotwrap", add_help=True)
    sub = parser.add_subparsers(dest="command", required=True)

    p_install = sub.add_parser("install")
    p_install.add_argument("provider", nargs="?", default=DEFAULT_PROVIDER)

    p_uninstall = sub.add_parser("uninstall")
    p_uninstall.add_argument("provider", nargs="?", default=DEFAULT_PROVIDER)

    p_doctor = sub.add_parser("doctor")
    p_doctor.add_argument("provider", nargs="?", default=DEFAULT_PROVIDER)

    return parser


def main(argv: list[str]) -> int:
    require_python_311()
    parser = build_parser()
    ns = parser.parse_args(argv)

    provider = getattr(ns, "provider", DEFAULT_PROVIDER)
    try:
        if ns.command == "install":
            return cmd_install(provider)
        if ns.command == "uninstall":
            return cmd_uninstall(provider)
        if ns.command == "doctor":
            return cmd_doctor(provider)
    except BrokenPipeError:
        return EXIT_OK
    except SystemExit:
        raise
    except Exception as exc:
        _err(str(exc))
        return EXIT_ENV

    return EXIT_INVALID


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

