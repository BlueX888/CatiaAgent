"""Generate the root contract schemas, registry and TypeScript copy.

Deterministic: the same source models produce byte-identical files.  ``--check``
writes nothing and exits non-zero on any difference or missing file.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root).resolve()
    server_root = repo_root / "catia-v5-mcp-server"
    sys.path.insert(0, str(server_root))
    from catia_mcp.contracts.export import build_registry, render_files

    files = render_files()
    missing: list[str] = []
    for relative, data in sorted(files.items()):
        target = repo_root / relative
        if args.check:
            if not target.is_file() or target.read_bytes() != data:
                missing.append(relative)
            continue
        if target.is_file() and target.read_bytes() == data:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    registry = build_registry()
    if args.check and missing:
        print("contract export differs: " + ", ".join(missing))
        return 1
    print(f"contracts={len(registry['contracts'])} legacy={len(registry['legacy'])} "
          f"files={len(files)} mode={'check' if args.check else 'write'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
