#!/usr/bin/env python3
"""Run the deployed failover watchdog while skipping components that run on another host.

  failover-exclude.py [failover.py arguments] --exclude-component NAME [--exclude-component NAME ...]
  failover-exclude.py --print-components [--exclude-component NAME ...]    print what would be watched and exit

The watchdog's loops read the module-level COMPONENTS tuple when they run, so removing entries before main() is enough:
the deployed failover.py is not modified. An unknown NAME exits 2 (a typo must never silently watch less). The
directory holding failover.py/bgctl.py comes from AISIS_BLUEGREEN_DIR (default ~/.local/share/aisis-yandex/bluegreen).
"""
import os
import sys
from pathlib import Path

BLUEGREEN_DIR = Path(os.environ.get("AISIS_BLUEGREEN_DIR") or Path.home() / ".local/share/aisis-yandex/bluegreen")
sys.path.insert(0, str(BLUEGREEN_DIR))
import failover  # noqa: E402  (deployed watchdog module)


def usage_error(message):
    print(f"failover-exclude: {message}", file=sys.stderr, flush=True)
    sys.exit(2)


def take_excludes(argv):
    """(remaining argv, excluded names, print_only); exits 2 on a malformed flag."""
    rest, excluded, print_only = [], [], False
    it = iter(argv)
    for arg in it:
        if arg == "--print-components":
            print_only = True
        elif arg == "--exclude-component":
            name = next(it, None)
            if not name or name.startswith("--"):
                usage_error("--exclude-component needs a NAME")
            excluded.append(name)
        elif arg.startswith("--exclude-component="):
            name = arg.split("=", 1)[1]
            if not name:
                usage_error("--exclude-component needs a NAME")
            excluded.append(name)
        else:
            rest.append(arg)
    return rest, excluded, print_only


def main():
    rest, excluded, print_only = take_excludes(sys.argv[1:])
    known = [comp.name for comp in failover.COMPONENTS]
    unknown = sorted(set(excluded) - set(known))
    if unknown:
        print(f"failover-exclude: unknown component(s) {', '.join(unknown)}; known: {', '.join(known)}",
              file=sys.stderr, flush=True)
        return 2
    failover.COMPONENTS = tuple(comp for comp in failover.COMPONENTS if comp.name not in excluded)
    watching = [comp.name for comp in failover.COMPONENTS]
    print(f"failover-exclude: watching {', '.join(watching)}; excluded {', '.join(sorted(set(excluded))) or 'none'}", flush=True)
    if print_only:
        return 0
    sys.argv = [sys.argv[0], *rest]
    return failover.main()


if __name__ == "__main__":
    sys.exit(main())
