"""Run the auto-ontology flow without the UI (same code as the /api/gen endpoints).

    uv run python scripts/generate_ontology.py                       # profile + draft
    uv run python scripts/generate_ontology.py --use-docs            # ... with table docs
    uv run python scripts/generate_ontology.py --use-docs --certify-all --publish

--certify-all skips the human review step; use it for demos and tests only.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import generator  # noqa: E402
from app.config import DOCS_DIR, GENERATED_DB  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tables", nargs="+", help="only these tables (default: all)")
    parser.add_argument("--use-docs", action="store_true", help=f"use per-table docs from {DOCS_DIR}")
    parser.add_argument("--certify-all", action="store_true", help="certify every draft (no review)")
    parser.add_argument("--publish", action="store_true", help="embed certified entries and export YAML")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    for _, message in generator.profile_steps():
        print(f"[profile] {message}")
    for event in generator.generate(args.tables, args.use_docs):
        if event["type"] == "progress":
            print(f"[{event['table']}] {event['message']}")
        elif event["type"] == "entry":
            e = event["entry"]
            print(f"   + {e['kind']:<13} {e['id']}" + ("   (needs review)" if e["needs_review"] else ""))
        elif event["type"] == "error":
            print(f"[{event['table'] or 'error'}] ERROR {event['message']}")
        else:
            print(f"done: {event['created']} drafts in {event['elapsed_ms'] / 1000:.0f} s -> {GENERATED_DB}")
    if args.certify_all:
        print(f"certified {generator.bulk_status(None, 'certified')} drafts")
    if args.publish:
        result = generator.publish()
        print(f"published {result['published']} entries, exported {result['yaml_path']}")


if __name__ == "__main__":
    main()
