"""Automatic ontology generation: Profile -> Generate -> Certify -> Publish.

1. Profile   (no LLM) statistics, sample values and flags per column, plus
             foreign-key candidates from name matching and value containment.
2. Generate  the chat model drafts entries per table from the profile (and the
             table's documentation, if asked); every draft is checked against the
             database and marked `needs_review` when its meaning is a guess.
3. Certify   a person edits, certifies or rejects drafts (server API).
4. Publish   certified entries are embedded, made searchable for the `generated`
             chat mode and exported to ontology/generated.yaml.

Everything lives in data/generated.db, separate from the curated data/ontology.db.
Works on any SQLite database (DB_PATH); nothing here is specific to the demo data.
"""

import csv
import io
import itertools
import json
import re
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

import httpx
import yaml

from app import tools
from app.config import (
    CHAT_MODEL, DOCS_DIR, FINANCIAL_DB, GENERATED_DB, GENERATED_YAML, OLLAMA_BASE_URL, ROOT,
)

SAMPLE_ROWS = 50_000       # larger tables are profiled on a sample of about this many rows
LOW_CARDINALITY = 30       # columns with at most this many distinct values list their top values
FK_MIN_OVERLAP = 0.5       # share of a column's values that must exist in the referenced key
MAX_SQL_ATTRIBUTES = 2     # per table
STATUSES = ("draft", "certified", "rejected")
CONTENT_FIELDS = ("name", "description", "synonyms", "columns", "sql")
# Column-name words that say little on their own ("type" of what?).
AMBIGUOUS = {"type", "status", "code", "kind", "category", "class", "flag", "level", "mode",
             "state", "symbol", "grade", "group"}
SHORT_WORDS = {"age", "sex", "day", "tax", "fee", "key", "net", "end", "new", "old", "zip", "url"}
PUBLISHED = "published = 1 AND status = 'certified' AND vector IS NOT NULL"
# The model is told to word guesses as guesses; such wording always means "needs review".
HEDGES = re.compile(r"\b(unknown|unclear|uncertain|probably|likely|possibly|perhaps|guess\w*|"
                    r"not documented|undocumented)\b", re.IGNORECASE)

_generate_lock = threading.Lock()


# ---------------------------------------------------------------- stores

_SCHEMA = """
CREATE TABLE IF NOT EXISTS profile_tables (
    name TEXT PRIMARY KEY, row_count INTEGER NOT NULL, sampled_rows INTEGER NOT NULL,
    columns TEXT NOT NULL                  -- JSON list of column profiles
);
CREATE TABLE IF NOT EXISTS fk_candidates (
    from_col TEXT NOT NULL, to_col TEXT NOT NULL, overlap REAL NOT NULL,
    PRIMARY KEY (from_col, to_col)
);
CREATE TABLE IF NOT EXISTS entries (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL, description TEXT NOT NULL,
    synonyms TEXT NOT NULL, columns TEXT NOT NULL, sql TEXT,   -- JSON lists, as in ontology.db
    status TEXT NOT NULL, origin TEXT NOT NULL, needs_review INTEGER NOT NULL,
    review_note TEXT, evidence TEXT NOT NULL,                  -- evidence: JSON object
    published INTEGER NOT NULL DEFAULT 0, embed_text TEXT, vector BLOB  -- set by publish()
);
"""


def _store() -> sqlite3.Connection:
    GENERATED_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(GENERATED_DB, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def _db() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{FINANCIAL_DB.as_posix()}?mode=ro", uri=True)


def _entry(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "kind": row["kind"], "name": row["name"],
        "description": row["description"], "synonyms": json.loads(row["synonyms"]),
        "columns": json.loads(row["columns"]), "sql": row["sql"], "status": row["status"],
        "origin": row["origin"], "needs_review": bool(row["needs_review"]),
        "review_note": row["review_note"], "evidence": json.loads(row["evidence"]),
        "published": bool(row["published"]),
    }


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _ident(conn: sqlite3.Connection, name: str, table: str | None = None) -> str:
    """`name` bare if SQLite accepts it unquoted, else double-quoted (e.g. "order")."""
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        probe = f"SELECT {name} FROM {_q(table)}" if table else f"SELECT 1 FROM {name}"
        try:
            conn.execute(probe + " LIMIT 0")
            return name
        except sqlite3.Error:
            pass
    return _q(name)


def _schema(conn: sqlite3.Connection) -> dict:
    """{table: {"columns": [(name, type)], "pk": [col], "fks": [(col, ref_table, ref_col)]}}"""
    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()]
    out = {}
    for t in names:
        info = conn.execute(f"PRAGMA table_info({_q(t)})").fetchall()
        fks = conn.execute(f"PRAGMA foreign_key_list({_q(t)})").fetchall()
        out[t] = {
            "columns": [(c[1], c[2] or "") for c in info],
            "pk": [c[1] for c in sorted(info, key=lambda c: c[5]) if c[5]],
            "fks": [(f[3], f[2], f[4]) for f in fks],
        }
    return out


def _colmap(conn: sqlite3.Connection) -> dict:
    """{table_lower: (table, {column_lower: column})} for validating table.column names."""
    return {t.lower(): (t, {c.lower(): c for c, _ in info["columns"]})
            for t, info in _schema(conn).items()}


# ---------------------------------------------------------------- 1. profile

def _tokens(name: str) -> list[str]:
    spaced = re.sub(r"([a-z])([A-Z])", r"\1_\2", name).lower()
    return [t for t in re.split(r"[^a-z0-9]+", spaced) if t]


def _is_cryptic(name: str) -> bool:
    """True when no part of the name is a real word: A11, sts_cd, x1."""
    words = [t for t in _tokens(name) if t != "id"]
    return bool(words) and not any(
        t in SHORT_WORDS or (len(t) >= 4 and t.isalpha() and re.search(r"[aeiouy]", t))
        for t in words
    )


def _sample_source(conn, table: str, row_count: int, tmp: str) -> tuple[str, int]:
    """The table itself, or for large tables a temp sample of every k-th rowid: one cheap
    pass, then all statistics run on ~SAMPLE_ROWS rows instead of the whole table."""
    q = _q(table)
    if row_count <= SAMPLE_ROWS:
        return q, row_count
    try:
        step = -(-row_count // SAMPLE_ROWS)
        conn.execute(f"CREATE TEMP TABLE {tmp} AS SELECT * FROM {q} WHERE rowid % {step} = 0")
    except sqlite3.OperationalError:  # WITHOUT ROWID table: take the first rows instead
        conn.execute(f"DROP TABLE IF EXISTS temp.{tmp}")
        conn.execute(f"CREATE TEMP TABLE {tmp} AS SELECT * FROM {q} LIMIT {SAMPLE_ROWS}")
    return f"temp.{tmp}", conn.execute(f"SELECT COUNT(*) FROM temp.{tmp}").fetchone()[0]


def _profile_columns(conn, src: str, sampled: int, row_count: int, columns, pk) -> list[dict]:
    aggs = []
    for name, _ in columns:
        x = _q(name)
        aggs += [
            f"COUNT(DISTINCT {x})", f"SUM({x} IS NULL)",
            f"SUM(typeof({x}) IN ('integer', 'real') OR (typeof({x}) = 'text' AND trim({x}) <> ''"
            f" AND trim({x}) NOT GLOB '*[^0-9.+-]*'))",
            f"SUM(CAST({x} AS TEXT) GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*')",
            f"MAX(length(CAST({x} AS TEXT)))", f"MIN({x} + 0)", f"MAX({x} + 0)", f"MIN({x})", f"MAX({x})",
        ]
    stats = conn.execute(f"SELECT {', '.join(aggs)} FROM {src}").fetchone()
    scale = row_count / sampled if sampled else 1
    out = []
    for i, (name, ctype) in enumerate(columns):
        distinct, nulls, n_num, n_date, max_len, nmin, nmax, vmin, vmax = stats[i * 9:(i + 1) * 9]
        filled = sampled - (nulls or 0)
        is_date = (filled > 0 and n_date == filled) or any(w in ctype.upper() for w in ("DATE", "TIME"))
        is_num = filled > 0 and n_num == filled and not is_date
        is_id = name in pk or _tokens(name)[-1:] == ["id"]
        low = distinct <= LOW_CARDINALITY
        coded = (not is_id and not is_date and filled > 0 and low and distinct * 2 <= filled
                 and (max_len or 0) <= 40 and (not is_num or bool(set(_tokens(name)) & AMBIGUOUS)))
        flags = [f for f, on in (("id", is_id), ("date", is_date), ("numeric", is_num),
                                 ("text", not is_num and not is_date), ("coded", coded),
                                 ("cryptic", _is_cryptic(name))) if on]
        x = _q(name)
        top = conn.execute(
            f"SELECT {x}, COUNT(*) FROM {src} WHERE {x} IS NOT NULL GROUP BY 1 ORDER BY 2 DESC, 1 "
            f"LIMIT {10 if low else 5}"
        ).fetchall()
        out.append({
            "name": name, "type": ctype, "distinct": distinct,
            "null_pct": round(100 * (nulls or 0) / sampled, 2) if sampled else 0.0,
            # counts of low-cardinality values are scaled up from the sample to the whole table
            "samples": [{"value": tools._json_value(v), "count": round(n * scale) if low else n}
                        for v, n in top],
            "min": tools._json_value(nmin if is_num else vmin) if (is_num or is_date) else None,
            "max": tools._json_value(nmax if is_num else vmax) if (is_num or is_date) else None,
            "flags": flags,
        })
    return out


def _stems(table: str) -> set[str]:
    t = table.lower()
    return {t, t[:-1]} if len(t) > 3 and t.endswith("s") else {t}


def _key_columns(info: dict, sampled: int, cols: list[dict]) -> list[str]:
    """The declared single-column primary key, else id-like columns that are unique and never
    NULL in the profile (tables imported from CSV usually declare no keys)."""
    if len(info["pk"]) == 1:
        return info["pk"]
    return [c["name"] for c in cols
            if "id" in c["flags"] and c["null_pct"] == 0 and c["distinct"] == sampled]


def _fk_pairs(schema: dict, keys: dict) -> list[tuple[str, str, str, str]]:
    """(table, column, ref_table, ref_column) worth testing: declared FKs, a column with the
    same name as another table's key, or a column named after another table."""
    pairs = []
    for t, info in schema.items():
        for col, ref_t, ref_c in info["fks"]:
            if ref_t in schema and (ref_c or keys[ref_t]):
                pairs.append((t, col, ref_t, ref_c or keys[ref_t][0]))
        for col, _ in info["columns"]:
            lc = col.lower()
            for u, ks in keys.items():
                if u == t:
                    continue
                named_after = lc in {f"{s}{sfx}" for s in _stems(u) for sfx in ("_id", "id", "")}
                for k in ks:
                    same_key = lc == k.lower() and lc != "id" and col not in keys[t]
                    if same_key or named_after:
                        pairs.append((t, col, u, k))
    return list(dict.fromkeys(pairs))


def profile_steps():
    """Profile every table (no LLM), then test foreign-key candidates by value containment.
    Yields (table, message) as it goes; saves at the end."""
    with closing(_db()) as conn:
        schema = _schema(conn)
        profiles, sources = {}, {}
        for i, (t, info) in enumerate(schema.items()):
            started = time.monotonic()
            row_count = conn.execute(f"SELECT COUNT(*) FROM {_q(t)}").fetchone()[0]
            sources[t], sampled = _sample_source(conn, t, row_count, f"_sample{i}")
            cols = _profile_columns(conn, sources[t], sampled, row_count, info["columns"], info["pk"])
            profiles[t] = (row_count, sampled, cols)
            note = f", sample of {sampled}" if sampled < row_count else ""
            ms = int((time.monotonic() - started) * 1000)
            yield t, f"Profiled {t}: {row_count} rows, {len(cols)} columns{note} ({ms} ms)"
        keys = {t: _key_columns(info, profiles[t][1], profiles[t][2]) for t, info in schema.items()}
        declared = {(t, c) for t, info in schema.items() for c, _, _ in info["fks"]}
        fks = []
        for ft, fc, pt, pc in _fk_pairs(schema, keys):
            total, hits = conn.execute(  # share of the column's distinct values found in the key
                f"SELECT COUNT(*), SUM(v IN (SELECT {_q(pc)} FROM {_q(pt)})) FROM (SELECT DISTINCT "
                f"{_q(fc)} AS v FROM {sources[ft]} WHERE {_q(fc)} IS NOT NULL LIMIT 10000)"
            ).fetchone()
            overlap = round((hits or 0) / total, 4) if total else 0.0
            if overlap >= FK_MIN_OVERLAP or (ft, fc) in declared:
                fks.append((f"{ft}.{fc}", f"{pt}.{pc}", overlap))
    with closing(_store()) as st, st:
        st.execute("DELETE FROM profile_tables")
        st.execute("DELETE FROM fk_candidates")
        st.executemany("INSERT INTO profile_tables VALUES (?, ?, ?, ?)",
                       [(t, rc, s, json.dumps(cols, ensure_ascii=False, default=str))
                        for t, (rc, s, cols) in profiles.items()])
        st.executemany("INSERT OR REPLACE INTO fk_candidates VALUES (?, ?, ?)", fks)


def profile() -> None:
    for _ in profile_steps():
        pass


def _load_profile(st: sqlite3.Connection) -> tuple[dict, list[dict]]:
    tables = {
        r["name"]: {"name": r["name"], "row_count": r["row_count"],
                    "sampled_rows": r["sampled_rows"], "columns": json.loads(r["columns"])}
        for r in st.execute("SELECT * FROM profile_tables ORDER BY rowid").fetchall()
    }
    fks = [{"from": r["from_col"], "to": r["to_col"], "overlap": r["overlap"]}
           for r in st.execute("SELECT * FROM fk_candidates ORDER BY rowid").fetchall()]
    return tables, fks


# ---------------------------------------------------------------- documentation

def _doc_path(table: str) -> Path | None:
    for ext in ("csv", "md", "txt"):
        path = DOCS_DIR / f"{table}.{ext}"
        if path.is_file():
            return path
    return None


def _doc_lines(path: Path, text: str, column: str) -> list[str]:
    """Lines of the documentation about one column (CSV: rows whose first cell is the column)."""
    if path.suffix.lower() == ".csv":
        rows = list(csv.reader(io.StringIO(text)))
        return [" ".join(" | ".join(c.strip() for c in row if c.strip()).split())
                for row in rows[1:] if row and row[0].strip().lower() == column.lower()]
    word = re.compile(rf"(?<![\w]){re.escape(column)}(?![\w])", re.IGNORECASE)
    return [ln.strip() for ln in text.splitlines() if word.search(ln)][:3]


def _doc_excerpt(path: Path, text: str, table: str, columns: list[str]) -> str | None:
    lines = [ln for c in columns if c.split(".", 1)[0] == table
             for ln in _doc_lines(path, text, c.split(".", 1)[1])]
    return "\n".join(lines)[:800] or None


# ---------------------------------------------------------------- validation

def normalize_columns(refs, colmap: dict, default_table: str | None = None) -> tuple[list, list]:
    """Map refs to existing "table.column" names (case fixed). Returns (valid, unknown)."""
    ok, bad = [], []
    for ref in refs:
        r = re.sub(r'["`\[\]]', "", str(ref)).strip()
        t, _, c = r.rpartition(".")
        hit = colmap.get((t or default_table or "").lower())
        col = hit[1].get(c.lower()) if hit else None
        (ok if col else bad).append(f"{hit[0]}.{col}" if col else r)
    return list(dict.fromkeys(ok)), bad


def check_sql(conn: sqlite3.Connection, sql: str, columns: list[str], colmap: dict) -> str | None:
    """None if the snippet compiles against the database as a query, an expression, a WHERE
    condition or a FROM/JOIN fragment over its tables; else the SQLite error. Read-only:
    statements are only compiled (EXPLAIN), under the same authorizer as run_sql."""
    body = sql.strip().rstrip(";").strip()
    tables = {c.split(".", 1)[0] for c in columns}
    for name, _ in colmap.values():  # tables used as qualifiers: loan.status, "order".amount
        if re.search(rf'(?<![\w.])["`\[]?{re.escape(name)}["`\]]?\s*\.', body, re.IGNORECASE):
            tables.add(name)
    names = [_ident(conn, t) for t in sorted(tables)]
    if re.match(r"(select|with)\b", body, re.IGNORECASE):
        attempts = [body]
    else:  # a trailing "-- comment" in the snippet ends at the newline
        frm = ", ".join(names)
        expr = ([f"SELECT {body}\nFROM {frm}", f"SELECT 1 FROM {frm} WHERE {body}\n"] if frm
                else []) + [f"SELECT {body}\n"]  # last: "COUNT(*) FROM t WHERE ..." style
        frag = [f"SELECT 1 FROM {body}\n"] + [f"SELECT 1 FROM {n} {body}\n" for n in names]
        attempts = frag + expr if re.search(r"\bJOIN\b", body, re.IGNORECASE) else expr + frag
    errors = []
    conn.set_authorizer(tools._authorizer)
    try:
        for attempt in attempts:
            try:
                conn.execute(f"EXPLAIN {attempt}")
                return None
            except (sqlite3.Error, sqlite3.Warning) as exc:
                errors.append(str(exc))
    finally:
        conn.set_authorizer(None)
    return errors[0]


# ---------------------------------------------------------------- 2. generate

SYSTEM_PROMPT = """You draft entries for a business ontology. An AI agent that writes SQL reads these
entries to learn what columns and coded values mean, so a wrong meaning causes wrong answers.
Be accurate and honest about what you do not know.

You get the profile of one database table (types, statistics, sample values with counts), the
links found between its columns and other tables, and sometimes the table's documentation.

Rules:
- Use only the profile and the documentation given here. Do not rely on outside knowledge of
  this particular dataset.
- basis: "documented" only if the documentation given here states the meaning. "evident" only
  if the column name and its values make the meaning certain to any reader (e.g. sex 'F'/'M').
  Status letters such as 'A'/'B', abbreviations, foreign-language words and numbers in a column
  with a meaningless name are never evident. Otherwise "guess".
- description: what the column holds, with its unit and range when known. Only for a column
  flagged "coded": list every stored value with its meaning, copied exactly (same case and
  spacing), in this form: "Values: 'S' = shipped; 'C' = cancelled; 'X' = meaning unknown."
- A guessed meaning must read as a guess in the description ("probably ...", "meaning unknown")
  and review_note must say what a person has to confirm. Use "" as review_note otherwise.
- name: a short business name, not the raw column name. synonyms: 3 to 8 words or phrases
  people would use in questions.
- sql: one short SQLite expression or WHERE condition (not a full query) with table-qualified
  columns showing how to use the entry, e.g. orders.sts_cd = 'S'  -- shipped. Write table
  names exactly as shown in the profile (some need double quotes). Use "" if nothing fits.
- sql_attributes: at most 2 reusable calculations (a classification or derived measure) that
  the profile or documentation supports. May be empty.
- relationships: one item per numbered link, index = its number, describing what the link
  means in business terms."""

_ITEM = {
    "type": "object",
    "properties": {
        "columns": {"type": "array", "items": {"type": "string"}},
        "basis": {"type": "string", "enum": ["documented", "evident", "guess"]},
        "name": {"type": "string"},
        "description": {"type": "string"},
        "synonyms": {"type": "array", "items": {"type": "string"}},
        "sql": {"type": "string"},
        "review_note": {"type": "string"},
    },
    "required": ["columns", "basis", "name", "description", "synonyms", "sql", "review_note"],
}
DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "terms": {"type": "array", "items": _ITEM},
        "sql_attributes": {"type": "array", "items": _ITEM},
        "relationships": {"type": "array", "items": {
            "type": "object",
            "properties": {"index": {"type": "integer"}, "name": {"type": "string"},
                           "description": {"type": "string"},
                           "synonyms": {"type": "array", "items": {"type": "string"}}},
            "required": ["index", "name", "description", "synonyms"],
        }},
    },
    "required": ["terms", "sql_attributes", "relationships"],
}


def _ask_model(system: str, user: str) -> dict:
    payload = {
        "model": CHAT_MODEL, "stream": False, "format": DRAFT_SCHEMA,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "options": {"temperature": 0, "num_ctx": 16384, "num_predict": 4096},
    }
    if CHAT_MODEL.startswith("qwen3"):
        payload["think"] = False  # thinking is very slow and adds nothing to drafting JSON
    resp = httpx.post(f"{OLLAMA_BASE_URL}/api/chat", json=payload,
                      timeout=httpx.Timeout(900, connect=10))
    resp.raise_for_status()
    return json.loads(resp.json()["message"]["content"])


def _needs_term(col: dict) -> bool:
    flags = set(col["flags"])
    return "id" not in flags and bool(
        flags & {"coded", "cryptic"} or set(_tokens(col["name"])) & AMBIGUOUS)


def _links(conn, table: str, fks: list[dict]) -> list[dict]:
    """Deterministic relationship structures: one per FK candidate from `table`, plus
    "A <-> B through table" when `table` links two other tables (a bridge such as disp)."""
    def split(ref):
        t, c = ref.split(".", 1)
        return t, c, _ident(conn, t), _ident(conn, c, t)

    own = [f for f in fks if f["from"].split(".", 1)[0] == table]
    links = []
    for f in own:
        _, _, ft, fc = split(f["from"])
        _, _, pt, pc = split(f["to"])
        links.append({"label": f"{f['from']} -> {f['to']}", "columns": [f["from"], f["to"]],
                      "overlap": f["overlap"], "sql": f"{ft} JOIN {pt} ON {pt}.{pc} = {ft}.{fc}"})
    for a, b in itertools.combinations(own, 2):
        a_t, _, at, ak = split(a["to"])
        b_t, _, bt, bk = split(b["to"])
        if a_t == b_t or table in (a_t, b_t):
            continue
        _, _, mt, ma = split(a["from"])
        _, _, _, mb = split(b["from"])
        links.append({
            "label": f"{a_t} <-> {b_t} through {table}",
            "columns": [a["to"], a["from"], b["from"], b["to"]],
            "overlap": min(a["overlap"], b["overlap"]),
            "sql": f"{at} JOIN {mt} ON {mt}.{ma} = {at}.{ak} JOIN {bt} ON {bt}.{bk} = {mt}.{mb}",
        })
    return links


def _fmt(value) -> str:
    return f"'{value}'" if isinstance(value, str) else str(value)


def _table_prompt(conn, prof: dict, others: list[str], links: list[dict], need: list[str],
                  doc_text: str | None) -> str:
    t = prof["name"]
    sampled = prof["sampled_rows"] < prof["row_count"]
    lines = [f"Table {_ident(conn, t)}: {prof['row_count']} rows"
             + (f" (statistics from a sample of {prof['sampled_rows']} rows)" if sampled else ""),
             f"Other tables: {', '.join(others) or 'none'}", "Columns:"]
    for c in prof["columns"]:
        parts = [c["type"] or "no declared type", "flags: " + ", ".join(c["flags"]),
                 f"{c['distinct']} distinct", f"{c['null_pct']}% null"]
        if c["min"] is not None:
            parts.append(f"range {c['min']} .. {c['max']}")
        label = ("all values" if c["distinct"] <= 10 else "top values") \
            if c["distinct"] <= LOW_CARDINALITY else "examples"
        values = ", ".join(f"{json.dumps(s['value'], ensure_ascii=False)} x{s['count']}"
                           for s in c["samples"])
        parts.append(f"{label}: {values}")
        lines.append(f"- {_ident(conn, c['name'], t)}: " + "; ".join(parts))
    if links:
        lines.append("Links (number: join, share of values that match):")
        lines += [f"{i}: {ln['label']}: {ln['sql']} ({ln['overlap']:.0%})" for i, ln in enumerate(links)]
    lines.append(f"Documentation for this table:\n<<<\n{doc_text[:6000].strip()}\n>>>" if doc_text
                 else "No documentation is available for this table.")
    lines.append(
        (f"Task: write term entries for these columns: {', '.join(need)} (one entry per column, "
         "or one entry for a group of closely related columns; cover all of them). " if need
         else "Task: no column needs a term entry; return an empty terms list. ")
        + f"Then at most {MAX_SQL_ATTRIBUTES} sql_attributes. "
        + ("Then one relationships item per numbered link." if links
           else "There are no links; return an empty relationships list."))
    return "\n".join(lines)


def _review(kind: str, cols: list[str], basis: str, model_note: str, documented: bool,
            pcols: dict) -> tuple[bool, str | None]:
    """needs_review and a plain note, decided by rules; the model only proposes a basis."""
    if documented:
        return False, None
    cryptic = [c for c in cols if "cryptic" in pcols.get(c, {}).get("flags", [])]
    if basis == "evident" and not cryptic:
        return False, None
    reasons = []
    for c in cols:
        p = pcols.get(c) or {}
        if "coded" in p.get("flags", []):
            vals = ", ".join(_fmt(s["value"]) for s in sorted(p["samples"], key=lambda s: str(s["value"])))
            more = f" (and {p['distinct'] - len(p['samples'])} more)" if p["distinct"] > len(p["samples"]) else ""
            reasons.append(f"Values {vals}{more} found in {c}; the meaning of each code is a guess - confirm.")
        elif c in cryptic:
            reasons.append(f"The name {c} does not say what it holds; its meaning was guessed "
                           "from its values - confirm.")
    if not reasons:
        reasons.append("Calculation suggested by the model without documentation - confirm it "
                       "matches the business definition." if kind == "sql_attribute"
                       else "Meaning inferred by the model without documentation - confirm.")
    if model_note.strip():
        reasons.append(f"Model: {model_note.strip()}")
    return True, " ".join(reasons)


def _unexplained_values(description: str, cols: list[str], pcols: dict) -> str | None:
    """Values of a coded column (up to 10 values) that the description never mentions."""
    missing = []
    for c in cols:
        p = pcols.get(c) or {}
        if "coded" in p.get("flags", []) and p["distinct"] <= 10:
            missing += [f"{_fmt(s['value'])} of {c}" for s in p["samples"] if str(s["value"]).strip()
                        and not re.search(rf"(?<!\w){re.escape(str(s['value']))}(?!\w)", description)]
    return ", ".join(missing) or None


def _clean_list(values) -> list[str]:
    return list(dict.fromkeys(" ".join(str(v).split()) for v in values or [] if str(v).strip()))[:12]


def _evidence(table: str, cols: list[str], pcols: dict, prof: dict, doc_excerpt, extra=None) -> dict:
    per = 10 if len(cols) == 1 else 5
    stats = {"row_count": prof["row_count"]}
    if prof["sampled_rows"] < prof["row_count"]:
        stats["sampled_rows"] = prof["sampled_rows"]
    stats.update({c: {k: pcols[c][k] for k in ("type", "distinct", "null_pct", "min", "max", "flags")}
                  for c in cols if c in pcols})
    stats.update(extra or {})
    return {"table": table,
            "samples": [{"column": c, **s} for c in cols for s in pcols.get(c, {}).get("samples", [])[:per]],
            "stats": stats, "doc_excerpt": doc_excerpt}


def _draft_table(conn, table: str, ptables: dict, fks: list[dict], use_docs: bool,
                 colmap: dict) -> list[dict]:
    """Ask the model for one table's drafts and turn them into checked entries."""
    prof = ptables[table]
    pcols = {f"{t}.{c['name']}": c for t, p in ptables.items() for c in p["columns"]}
    need = [c["name"] for c in prof["columns"] if _needs_term(c)]
    links = _links(conn, table, fks)
    if not need and not links:
        return []
    doc_path = _doc_path(table) if use_docs else None
    doc_text = doc_path.read_text(encoding="utf-8-sig", errors="replace") if doc_path else None
    others = [t for t in ptables if t != table]
    data = _ask_model(SYSTEM_PROMPT, _table_prompt(conn, prof, others, links, need, doc_text))

    entries = []
    items = [("term", i) for i in data.get("terms") or []] + \
        [("sql_attribute", i) for i in (data.get("sql_attributes") or [])[:MAX_SQL_ATTRIBUTES]]
    for kind, item in items:
        if not isinstance(item, dict):
            continue
        cols, bad = normalize_columns(item.get("columns") or [], colmap, table)
        name = " ".join(str(item.get("name") or "").split())
        desc = " ".join(str(item.get("description") or "").split())
        if not name or not desc or (kind == "term" and not cols):
            continue  # a term must point at columns that exist
        notes = [f"Dropped unknown column(s): {', '.join(bad)}."] if bad else []
        sql = str(item.get("sql") or "").strip() or None
        if sql and (err := check_sql(conn, sql, cols, colmap)):
            notes.append(f"Generated SQL was dropped because it does not compile: {sql} ({err}).")
            sql = None
        excerpt = _doc_excerpt(doc_path, doc_text, table, cols) if doc_text else None
        # "documented" needs the model's claim AND every column present in this table's docs
        documented = bool(doc_text and cols) and item.get("basis") == "documented" and all(
            c.split(".", 1)[0] == table and _doc_lines(doc_path, doc_text, c.split(".", 1)[1])
            for c in cols)
        needs_review, note = _review(kind, cols, item.get("basis", "guess"),
                                     str(item.get("review_note") or ""), documented, pcols)
        if kind == "term" and (missing := _unexplained_values(desc, cols, pcols)):
            needs_review = True
            notes.insert(0, f"The description does not explain {missing}; add their meaning.")
        if not needs_review and HEDGES.search(desc):
            needs_review = True
            notes.insert(0, "The description says part of the meaning is unknown or uncertain - confirm.")
        entries.append({
            "kind": kind, "name": name, "description": desc,
            "synonyms": _clean_list(item.get("synonyms")), "columns": cols, "sql": sql,
            "needs_review": needs_review, "review_note": " ".join([note or ""] + notes).strip() or None,
            "evidence": _evidence(table, cols, pcols, prof, excerpt),
        })

    named = {r.get("index"): r for r in data.get("relationships") or [] if isinstance(r, dict)}
    for i, link in enumerate(links):
        item = named.get(i) or {}
        sql, notes = link["sql"], []
        if err := check_sql(conn, sql, link["columns"], colmap):
            notes.append(f"Join SQL was dropped because it does not compile: {sql} ({err}).")
            sql = None
        desc = " ".join(str(item.get("description") or "").split()) or f"{link['label']}: {link['sql']}."
        review = link["overlap"] < 0.99
        if review:
            notes.insert(0, f"Only {link['overlap']:.0%} of the values match ({link['label']}); "
                            "confirm this is a real link.")
        elif HEDGES.search(desc):
            review = True
            notes.insert(0, "The join is certain, but the description says its meaning is uncertain - confirm.")
        entries.append({
            "kind": "relationship",
            "name": " ".join(str(item.get("name") or "").split()) or link["label"],
            "description": desc,
            "synonyms": _clean_list(item.get("synonyms")), "columns": link["columns"], "sql": sql,
            "needs_review": review, "review_note": " ".join(notes) or None,
            "evidence": _evidence(table, [], pcols, prof,
                                  _doc_excerpt(doc_path, doc_text, table, link["columns"]) if doc_text else None,
                                  {"overlap": link["overlap"], "link": link["label"]}),
        })
    return entries


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "entry"


def _insert(st: sqlite3.Connection, e: dict) -> dict:
    t, n = _slug(e["evidence"]["table"]), _slug(e["name"])
    base = (n if n == t or n.startswith(t + "_") else f"{t}_{n}")[:60].rstrip("_")
    entry_id, i = base, 1
    while st.execute("SELECT 1 FROM entries WHERE id = ?", (entry_id,)).fetchone():
        i += 1
        entry_id = f"{base}_{i}"
    e = {"id": entry_id, **e, "status": "draft", "origin": "generated", "published": False}
    st.execute(
        "INSERT INTO entries (id, kind, name, description, synonyms, columns, sql, status, origin,"
        " needs_review, review_note, evidence, published) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
        (e["id"], e["kind"], e["name"], e["description"], json.dumps(e["synonyms"], ensure_ascii=False),
         json.dumps(e["columns"]), e["sql"], e["status"], e["origin"], int(e["needs_review"]),
         e["review_note"], json.dumps(e["evidence"], ensure_ascii=False, default=str)),
    )
    return e


def _generate(tables: list[str] | None, use_docs: bool):
    with closing(_store()) as st:
        profiled = st.execute("SELECT COUNT(*) FROM profile_tables").fetchone()[0] > 0
    if not profiled:
        for t, message in profile_steps():
            yield {"type": "progress", "table": t, "message": message}
    with closing(_store()) as st:
        ptables, fks = _load_profile(st)
    with closing(_db()) as conn:
        colmap = _colmap(conn)
        for t in (list(ptables) if tables is None else tables):
            if t not in ptables:
                yield {"type": "error", "table": t, "message": f"unknown table '{t}' (not in the profile)"}
                continue
            started = time.monotonic()
            need = sum(_needs_term(c) for c in ptables[t]["columns"])
            docs = ("with its documentation" if _doc_path(t) else "no documentation found") \
                if use_docs else "without documentation"
            yield {"type": "progress", "table": t,
                   "message": f"Drafting {t} with {CHAT_MODEL}: {need} column(s) to explain, {docs}"}
            try:
                drafts = _draft_table(conn, t, ptables, fks, use_docs, colmap)
                with closing(_store()) as st, st:  # replace this table's untouched drafts
                    st.execute("DELETE FROM entries WHERE status = 'draft' AND origin = 'generated'"
                               " AND json_extract(evidence, '$.table') = ?", (t,))
                    saved = [_insert(st, e) for e in drafts]
            except Exception as exc:  # noqa: BLE001 - one table failing must not stop the rest
                yield {"type": "error", "table": t, "message": f"{type(exc).__name__}: {exc}"}
                continue
            for e in saved:
                yield {"type": "entry", "entry": e}
            review = sum(e["needs_review"] for e in saved)
            yield {"type": "progress", "table": t,
                   "message": f"{len(saved)} drafts for {t}, {review} need review "
                              f"({time.monotonic() - started:.0f} s)"}


def generate(tables: list[str] | None = None, use_docs: bool = False):
    """Draft entries for `tables` (None = all). Yields progress/entry/error events; the last
    event is always {"type": "done", ...}. Profiles first if the database is not profiled."""
    started, created = time.monotonic(), 0
    if _generate_lock.acquire(blocking=False):
        try:
            for event in _generate(tables, use_docs):
                created += event["type"] == "entry"
                yield event
        except Exception as exc:  # noqa: BLE001
            yield {"type": "error", "table": None, "message": f"{type(exc).__name__}: {exc}"}
        finally:
            _generate_lock.release()
    else:
        yield {"type": "error", "table": None, "message": "Another generation is already running."}
    yield {"type": "done", "elapsed_ms": int((time.monotonic() - started) * 1000), "created": created}


# ---------------------------------------------------------------- 3. certify

def update_entry(entry_id: str, changes: dict) -> dict:
    """Apply a person's edits/status change. Raises KeyError (no entry) or ValueError."""
    with closing(_store()) as st, st:
        row = st.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
        if row is None:
            raise KeyError(entry_id)
        old = _entry(row)
        new = dict(old)
        for field in ("name", "description"):
            if field in changes:
                new[field] = " ".join(str(changes[field] or "").split())
                if not new[field]:
                    raise ValueError(f"{field} must not be empty")
        if "synonyms" in changes:
            new["synonyms"] = _clean_list(changes["synonyms"])
        if "sql" in changes:
            new["sql"] = str(changes["sql"] or "").strip() or None
        with closing(_db()) as conn:
            colmap = _colmap(conn)
            if "columns" in changes:
                new["columns"], bad = normalize_columns(changes["columns"] or [], colmap,
                                                        old["evidence"].get("table"))
                if bad:
                    raise ValueError(f"unknown column(s): {', '.join(bad)} "
                                     "(use table.column names that exist in the database)")
            if new["sql"] and ("sql" in changes or "columns" in changes):
                if err := check_sql(conn, new["sql"], new["columns"], colmap):
                    raise ValueError(f"invalid SQL: {err}")
        status = changes.get("status") or old["status"]
        if status not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)}")
        edited = any(new[f] != old[f] for f in CONTENT_FIELDS)
        new.update(status=status, origin="edited" if edited else old["origin"],
                   published=old["published"] and not edited and status == "certified")
        st.execute(
            "UPDATE entries SET name = ?, description = ?, synonyms = ?, columns = ?, sql = ?,"
            " status = ?, origin = ?, published = ?"
            + ("" if new["published"] else ", embed_text = NULL, vector = NULL") + " WHERE id = ?",
            (new["name"], new["description"], json.dumps(new["synonyms"], ensure_ascii=False),
             json.dumps(new["columns"]), new["sql"], new["status"], new["origin"],
             int(new["published"]), entry_id),
        )
    return new


def bulk_status(ids: list[str] | None, status: str) -> int:
    """Set the status of `ids` (None = all current drafts). Returns how many were updated."""
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    unpublish = "" if status == "certified" else ", published = 0, embed_text = NULL, vector = NULL"
    with closing(_store()) as st, st:
        if ids is None:
            ids = [r[0] for r in st.execute("SELECT id FROM entries WHERE status = 'draft'").fetchall()]
        return sum(st.execute(f"UPDATE entries SET status = ?{unpublish} WHERE id = ?",
                              (status, i)).rowcount for i in ids)


def reset() -> None:
    """Delete all generated entries (profiles are kept)."""
    with closing(_store()) as st, st:
        st.execute("DELETE FROM entries")


# ---------------------------------------------------------------- 4. publish

class _IndentedDumper(yaml.SafeDumper):
    """Indent list items under their key, as in the hand-written financial.yaml."""

    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, False)


def publish() -> dict:
    """Embed every certified entry, make it searchable, unpublish the rest, export YAML."""
    with closing(_store()) as st, st:
        rows = [_entry(r) for r in st.execute(
            "SELECT * FROM entries WHERE status = 'certified' ORDER BY rowid").fetchall()]
        texts = [tools.embed_text(e) for e in rows]
        vectors = tools.embed(texts) if texts else []
        st.execute("UPDATE entries SET published = 0, embed_text = NULL, vector = NULL"
                   " WHERE status != 'certified'")
        st.executemany("UPDATE entries SET published = 1, embed_text = ?, vector = ? WHERE id = ?",
                       [(txt, tools.vector_to_blob(v), e["id"]) for e, txt, v in zip(rows, texts, vectors)])
    GENERATED_YAML.parent.mkdir(parents=True, exist_ok=True)
    keys = ("id", "kind", "name", "description", "synonyms", "columns", "sql")
    body = yaml.dump({"entries": [{k: e[k] for k in keys} for e in rows]}, Dumper=_IndentedDumper,
                     sort_keys=False, allow_unicode=True, width=100, default_flow_style=None)
    GENERATED_YAML.write_text(
        f"# Generated ontology for {FINANCIAL_DB.name}: the certified entries of the auto-ontology\n"
        "# flow (Profile -> Generate -> Certify -> Publish, app/generator.py). Written by publish;\n"
        "# do not edit by hand. Same format as financial.yaml.\n\n" + body, encoding="utf-8")
    try:
        yaml_path = GENERATED_YAML.relative_to(ROOT).as_posix()
    except ValueError:
        yaml_path = str(GENERATED_YAML)
    return {"published": len(rows), "yaml_path": yaml_path}


# ---------------------------------------------------------------- 5. answer + state

def search_terms(query: str) -> dict:
    return tools.search_store(_store, query, PUBLISHED)


def get_term(term_id: str) -> dict:
    return tools.get_store_entry(_store, term_id, PUBLISHED)


# Same names and descriptions as the curated tools, so the agent sees identical tools.
search_terms.__doc__ = tools.search_terms.__doc__
get_term.__doc__ = tools.get_term.__doc__


def published_entries() -> list[dict]:
    with closing(_store()) as st:
        rows = st.execute(f"SELECT * FROM entries WHERE {PUBLISHED} ORDER BY rowid").fetchall()
    return [tools._entry_from_row(r) for r in rows]


def published_count() -> int:
    with closing(_store()) as st:
        return st.execute(f"SELECT COUNT(*) FROM entries WHERE {PUBLISHED}").fetchone()[0]


def state() -> dict:
    with closing(_store()) as st:
        ptables, fks = _load_profile(st)
        entries = [_entry(r) for r in st.execute("SELECT * FROM entries ORDER BY rowid").fetchall()]
    tables = [{"name": t["name"], "row_count": t["row_count"],
               "has_docs": _doc_path(t["name"]) is not None, "columns": t["columns"]}
              for t in ptables.values()]
    if tables:
        docs_available = any(t["has_docs"] for t in tables)
    else:
        try:
            with closing(_db()) as conn:
                docs_available = any(_doc_path(t) for t in _schema(conn))
        except sqlite3.Error:
            docs_available = False
    counts = {s: sum(e["status"] == s for e in entries) for s in STATUSES}
    counts["published"] = sum(e["published"] for e in entries)
    return {"profiled": bool(tables), "tables": tables, "fk_candidates": fks, "entries": entries,
            "counts": counts, "docs_available": docs_available}
