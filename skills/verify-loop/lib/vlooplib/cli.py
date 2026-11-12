"""`vloop` dispatcher (§2.10.1). `hook` takes a fast path without building the argparse tree."""
from __future__ import annotations

import json
import os
import sys

MODULES = ("admin", "hooks", "loop", "metrics", "qatools", "setup")
PLATFORM_FLAG = "--platform"


def version_info() -> dict:
    from . import CONTRACT, NAME, VERSION, paths

    return {"name": NAME, "version": VERSION, "contract": CONTRACT, "skill_dir": str(paths.skill_dir()),
            "cli": str(paths.cli())}


def cmd_version(args) -> int:
    info = version_info()
    if args.json:
        print(json.dumps(info, ensure_ascii=False))
    else:
        print(f"{info['name']} {info['version']} (contract {info['contract']}) — {info['cli']}")
    return 0


def build_parser():
    import argparse
    import importlib

    from . import platforms

    parser = argparse.ArgumentParser(prog="vloop", description="verify-loop: tracking, gate and verification loop")
    parser.add_argument(PLATFORM_FLAG, choices=platforms.NAMES, default=None,
                        help="agent platform (default: detected)")
    sub = parser.add_subparsers(dest="cmd", metavar="<command>")
    sub.required = True
    v = sub.add_parser("version", help="name, version, contract and paths of this vloop")
    v.add_argument("--json", action="store_true")
    v.set_defaults(fn=cmd_version)
    for name in MODULES:
        try:
            mod = importlib.import_module(f"vlooplib.{name}")
            mod.register(sub)
        except (ImportError, AttributeError):
            if os.environ.get("VLOOP_STRICT") == "1":
                raise
    return parser


def split_platform(argv: list[str]) -> tuple[str | None, list[str]]:
    """A leading `--platform X` / `--platform=X`; the rest of argv unchanged."""
    if argv and argv[0] == PLATFORM_FLAG and len(argv) > 1:
        return argv[1], argv[2:]
    if argv and argv[0].startswith(PLATFORM_FLAG + "="):
        return argv[0].split("=", 1)[1], argv[1:]
    return None, argv


def main(argv=None) -> int:
    from . import platforms

    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        plat, rest = split_platform(argv)
        if rest and rest[0] == "hook" and len(rest) > 1 and not rest[1].startswith("-"):
            platforms.set_override(plat if plat in platforms.NAMES else None)
            from . import hooks

            return hooks.main(rest[1:])
        args = build_parser().parse_args(argv)
        platforms.set_override(getattr(args, "platform", None))
        return int(args.fn(args) or 0)
    except KeyboardInterrupt:
        return 130
    except SystemExit:
        raise
    except TimeoutError as e:
        sys.stderr.write(f"vloop: error: {e} (another vloop process holds the lock; retry)\n")
        return 1
    except BrokenPipeError:
        try:
            sys.stdout = open(os.devnull, "w")
        except OSError:
            pass
        return 0
    except Exception as e:  # runtime error
        sys.stderr.write(f"vloop: error: {e}\n")
        return 1
