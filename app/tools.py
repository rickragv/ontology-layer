"""Tool functions the agents can call. Each returns a plain JSON-serialisable dict."""

import json
import math
import re
import sqlite3
import time
from array import array
from contextlib import closing

import httpx

from app.config import EMBED_MODEL, FINANCIAL_DB, OLLAMA_BASE_URL, ONTOLOGY_DB

MAX_ROWS = 50
SQL_TIMEOUT_S = 15


# ---------------------------------------------------------------- embeddings

def embed(texts: list[str]) -> list[list[float]]:
    """Embed texts with Ollama (/api/embed)."""
    resp = httpx.post(
        f"{OLLAMA_BASE_URL}/api/embed",
        json={"model": EMBED_MODEL, "input": texts},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["embeddings"]


def vector_to_blob(vec: list[float]) -> bytes:
    return array("f", vec).tobytes()


def blob_to_vector(blob: bytes) -> array:
    vec = array("f")
    vec.frombytes(blob)
    return vec


def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def _ontology_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{ONTOLOGY_DB.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _entry_from_row(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "kind": row["kind"],
        "description": row["description"],
        "synonyms": json.loads(row["synonyms"]),
        "columns": json.loads(row["columns"]),
        "sql": row["sql"],
    }


def list_entries() -> list[dict]:
    """All ontology entries (used by the /api/ontology endpoint)."""
    with closing(_ontology_conn()) as conn:
        rows = conn.execute("SELECT * FROM entries ORDER BY rowid").fetchall()
    return [_entry_from_row(r) for r in rows]


# ---------------------------------------------------------------- ontology tools

def search_terms(query: str) -> dict:
    """Semantic search over the business ontology (terms, reviewed SQL attributes,
    and table relationships). Use it for every business concept in the question,
    e.g. "average salary", "monthly statement", "loan in good standing",
    "how client joins to account". Returns the 5 closest entries with their ids.
    """
    try:
        qvec = embed([query])[0]
        with closing(_ontology_conn()) as conn:
            rows = conn.execute(
                "SELECT id, name, kind, description, vector FROM entries"
            ).fetchall()
    except Exception as exc:  # noqa: BLE001 - tools must never raise
        return {"terms": [], "error": f"search failed: {exc}"}
    scored = [
        {
            "id": r["id"],
            "name": r["name"],
            "kind": r["kind"],
            "score": round(_cosine(qvec, blob_to_vector(r["vector"])), 4),
            "description": r["description"],
        }
        for r in rows
    ]
    scored.sort(key=lambda t: t["score"], reverse=True)
    return {"terms": scored[:5]}


def get_term(term_id: str) -> dict:
    """Get the full ontology entry for an id returned by search_terms: exact
    table.column names, coded values and their meanings, and a reviewed SQL
    snippet (expression, filter or join) to reuse in your query.
    """
    try:
        with closing(_ontology_conn()) as conn:
            row = conn.execute(
                "SELECT * FROM entries WHERE id = ?", (term_id.strip(),)
            ).fetchone()
    except Exception as exc:  # noqa: BLE001
        return {"error": f"lookup failed: {exc}"}
    if row is None:
        return {"error": f"no ontology entry with id '{term_id}'. Use an id from search_terms."}
    return _entry_from_row(row)


# ---------------------------------------------------------------- database tools

def _financial_conn() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{FINANCIAL_DB.as_posix()}?mode=ro", uri=True)


def get_schema() -> dict:
    """List every table in the bank database with its column names and SQL types."""
    try:
        with closing(_financial_conn()) as conn:
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
                )
            ]
            result = []
            for t in tables:
                cols = conn.execute(f'PRAGMA table_info("{t}")').fetchall()
                result.append(
                    {"name": t, "columns": [{"name": c[1], "type": c[2]} for c in cols]}
                )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"schema read failed: {exc}"}
    return {"tables": result}


_FENCE = re.compile(r"^```(?:sql|sqlite)?\s*|\s*```$", re.IGNORECASE)
_LEADING_COMMENTS = re.compile(r"^(\s*(--[^\n]*\n|/\*.*?\*/))*\s*", re.DOTALL)

# sqlite3 authorizer action codes that a plain query needs.
_ALLOWED_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}
_ALLOWED_ACTIONS.add(getattr(sqlite3, "SQLITE_RECURSIVE", 33))


def _authorizer(action, *_args):
    return sqlite3.SQLITE_OK if action in _ALLOWED_ACTIONS else sqlite3.SQLITE_DENY


def _json_value(v):
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace")
    if isinstance(v, float):
        return round(v, 6)
    return v


def run_sql(sql: str) -> dict:
    """Run ONE read-only SQLite query (SELECT or WITH) against the bank database.
    Returns columns and up to 50 rows. On error, read the message, fix the SQL
    and call again.
    """
    query = _FENCE.sub("", sql.strip()).strip()
    query = _LEADING_COMMENTS.sub("", query).rstrip().rstrip(";").rstrip()
    if not re.match(r"^(select|with)\b", query, re.IGNORECASE):
        return {"ok": False, "error": "Only a single SELECT or WITH query is allowed."}
    if ";" in query:
        # A ';' left inside the body means more than one statement.
        return {"ok": False, "error": "Only one SQL statement is allowed (remove ';')."}

    deadline = time.monotonic() + SQL_TIMEOUT_S
    try:
        conn = _financial_conn()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"cannot open database: {exc}"}
    try:
        conn.set_authorizer(_authorizer)
        conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
        cur = conn.execute(query)
        columns = [d[0] for d in cur.description or []]
        rows = cur.fetchmany(MAX_ROWS + 1)
    except sqlite3.OperationalError as exc:
        msg = str(exc)
        if "interrupted" in msg:
            msg = f"query exceeded the {SQL_TIMEOUT_S}s time limit; simplify it"
        return {"ok": False, "error": msg}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    finally:
        conn.close()

    truncated = len(rows) > MAX_ROWS
    rows = [[_json_value(v) for v in row] for row in rows[:MAX_ROWS]]
    return {
        "ok": True,
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
    }
