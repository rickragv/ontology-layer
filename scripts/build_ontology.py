"""Embed ontology/financial.yaml with Ollama and store it in data/ontology.db."""

import json
import sqlite3
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import EMBED_MODEL, ONTOLOGY_DB, ONTOLOGY_YAML  # noqa: E402
from app.tools import embed, vector_to_blob  # noqa: E402

KINDS = {"term", "sql_attribute", "relationship"}


def load_entries() -> list[dict]:
    entries = yaml.safe_load(ONTOLOGY_YAML.read_text(encoding="utf-8"))["entries"]
    seen = set()
    for e in entries:
        assert e["id"] not in seen, f"duplicate id {e['id']}"
        assert e["kind"] in KINDS, f"bad kind for {e['id']}"
        seen.add(e["id"])
        e["description"] = " ".join(e["description"].split())
        e.setdefault("synonyms", [])
        e.setdefault("columns", [])
        e.setdefault("sql", None)
    return entries


def embed_text(e: dict) -> str:
    return f"{e['name']}. Synonyms: {', '.join(e['synonyms'])}. {e['description']}"


def main() -> None:
    entries = load_entries()
    print(f"embedding {len(entries)} entries with {EMBED_MODEL} ...")
    vectors = embed([embed_text(e) for e in entries])

    ONTOLOGY_DB.parent.mkdir(parents=True, exist_ok=True)
    ONTOLOGY_DB.unlink(missing_ok=True)
    conn = sqlite3.connect(ONTOLOGY_DB)
    conn.executescript(
        """
        CREATE TABLE entries (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            synonyms TEXT NOT NULL,   -- JSON list
            columns TEXT NOT NULL,    -- JSON list of table.column
            sql TEXT,
            embed_text TEXT NOT NULL,
            vector BLOB NOT NULL      -- float32 array
        );
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        """
    )
    conn.executemany(
        "INSERT INTO entries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                e["id"], e["kind"], e["name"], e["description"],
                json.dumps(e["synonyms"]), json.dumps(e["columns"]), e["sql"],
                embed_text(e), vector_to_blob(v),
            )
            for e, v in zip(entries, vectors)
        ],
    )
    conn.executemany(
        "INSERT INTO meta VALUES (?, ?)",
        [("embed_model", EMBED_MODEL), ("dimensions", str(len(vectors[0])))],
    )
    conn.commit()
    conn.close()
    print(f"wrote {ONTOLOGY_DB} ({len(entries)} entries, {len(vectors[0])}-dim vectors)")


if __name__ == "__main__":
    main()
