"""Private process entry point for the local resident coordinator."""

import argparse
from pathlib import Path

from .resident import ResidentCoordinator


def main() -> int:
    parser = argparse.ArgumentParser(prog="agent-os-resident")
    parser.add_argument("--home", type=Path, required=True)
    args = parser.parse_args()
    ResidentCoordinator(args.home).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
