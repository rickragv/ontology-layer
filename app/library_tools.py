"""Ontology tools backed by a running NVIDIA Auto Ontology stack (REST API, x-api-key auth).

The stack's ontology has one term per table; the meanings live in the term's column
attributes (business name, description with coded values, sample values) and the joins in
the attributes' referenced columns. This module reads all of that live over REST, keeps a
short in-memory snapshot, and maps it onto the result shapes of app/tools.py so the
existing UI can render it. SQL execution stays in app/tools.run_sql.

Search: the stack's own search (POST /api/search/global-search with descriptions enabled)
only hits objects whose name or description contains every word of the query, and
GET /api/terms?query= only matches term names. So the stack's hits are combined with a
ranking done here over the same live data: bge-m3 similarity (as the other stores use)
plus word overlap with the term and column texts.
"""

import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from app.config import AUTO_ONTOLOGY_API_TOKEN, AUTO_ONTOLOGY_URL
from app.tools import embed

CACHE_TTL_S = 60          # how long a snapshot of the stack's ontology is reused
TIMEOUT = httpx.Timeout(15, connect=3)
TOP_TERMS = 5
MAX_MATCHED = 3           # matched columns shown per search result
LEX_WEIGHT = 0.15         # bonus for the share of query words found in a text
STACK_HIT_WEIGHT = 0.15   # bonus when the stack's own search returned the object

_lock = threading.Lock()
_snap: dict | None = None
_vectors: dict[str, list[float]] = {}  # embedded text -> vector, rebuilt per snapshot


class LibraryError(Exception):
    """A failure talking to the stack, with a message fit to show the model and the user."""


# ---------------------------------------------------------------- REST access

def _client(timeout=TIMEOUT) -> httpx.Client:
    if not AUTO_ONTOLOGY_API_TOKEN:
        raise LibraryError("AUTO_ONTOLOGY_API_TOKEN is not set, so the NVIDIA Auto Ontology "
                           "stack cannot be queried. Start the server with the stack's API token "
                           "in the environment.")
    return httpx.Client(base_url=AUTO_ONTOLOGY_URL, timeout=timeout,
                        headers={"x-api-key": AUTO_ONTOLOGY_API_TOKEN})


def _call(client: httpx.Client, method: str, path: str, **kwargs):
    try:
        resp = client.request(method, path, **kwargs)
    except httpx.ConnectTimeout:
        raise LibraryError(f"The NVIDIA Auto Ontology stack is not reachable at "
                           f"{AUTO_ONTOLOGY_URL} (no connection). Is it running?") from None
    except httpx.TimeoutException as exc:
        raise LibraryError(f"The NVIDIA Auto Ontology stack at {AUTO_ONTOLOGY_URL} did not "
                           f"answer in time ({type(exc).__name__}).") from None
    except httpx.HTTPError as exc:
        raise LibraryError(f"The NVIDIA Auto Ontology stack is not reachable at "
                           f"{AUTO_ONTOLOGY_URL} ({type(exc).__name__}). Is it running?") from None
    if resp.status_code in (401, 403):
        raise LibraryError(f"The NVIDIA Auto Ontology stack rejected the API token "
                           f"(HTTP {resp.status_code}). Check AUTO_ONTOLOGY_API_TOKEN.")
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        raise LibraryError(f"The NVIDIA Auto Ontology API returned HTTP {resp.status_code} "
                           f"for {method} {path}.")
    try:
        return resp.json()
    except ValueError:
        raise LibraryError(f"The NVIDIA Auto Ontology API sent a non-JSON reply for {method} "
                           f"{path}; is AUTO_ONTOLOGY_URL ({AUTO_ONTOLOGY_URL}) right?") from None


def _rows(body) -> list:
    """The list inside a paged/list response ({"data": [...]}) or a data wrapper."""
    if isinstance(body, dict):
        body = body.get("data", body.get("terms", []))
    return body if isinstance(body, list) else []


# ---------------------------------------------------------------- snapshot

def _unqualifier(schemas: set[str], tables: set[str]):
    """Removes `<db>.<schema>.` / `<schema>.` prefixes so references match our SQLite database."""
    names = [s for s in schemas if s and s.lower() not in {t.lower() for t in tables}]
    if not names:
        return lambda text: text
    alt = "|".join(re.escape(s) for s in sorted(names, key=len, reverse=True))
    q = r'["`\[]?'
    pattern = re.compile(rf'(?<![\w.]){q}(?:[A-Za-z_]\w*{q}\.{q})?(?:{alt}){q}\.(?=["`\[]?[A-Za-z_])',
                         re.IGNORECASE)
    return lambda text: pattern.sub("", text) if isinstance(text, str) else text


def _fetch_term(client: httpx.Client, term: dict, has_sql: bool) -> dict:
    tid = term["id"]
    detail = (_call(client, "GET", f"/api/terms/{tid}") or {}).get("data") or {}
    attrs = _rows(_call(client, "GET", f"/api/terms/{tid}/column-attributes"))
    sql_attrs = _rows(_call(client, "GET", f"/api/terms/{tid}/sql-attributes")) if has_sql else []
    return {"term": term, "detail": detail, "attrs": attrs, "sql_attrs": sql_attrs}


def _build_snapshot() -> dict:
    with _client() as client:
        page = _call(client, "GET", "/api/terms") or {}
        terms = page.get("terms") or []
        with_sql = {c.get("term_id") for c in page.get("sql_attribute_counts") or [] if c.get("count")}
        with ThreadPoolExecutor(max_workers=8) as pool:
            raw = list(pool.map(lambda t: _fetch_term(client, t, t["id"] in with_sql), terms))

    schemas, all_tables = set(), set()
    for r in raw:
        schemas.update(r["term"].get("schema_names") or [])
        all_tables.update(t.get("name") for t in r["detail"].get("tables") or [] if t.get("name"))
    unq = _unqualifier(schemas, all_tables)

    entries, table_term, links = [], {}, []
    for r in raw:
        t, d = r["term"], r["detail"]
        tables = [x["name"] for x in d.get("tables") or [] if x.get("name")]
        table_by_id = {x.get("id"): x.get("name") for x in d.get("tables") or []}
        cols = []
        for a in r["attrs"]:
            pc = a.get("primary_column") or {}
            table = pc.get("table_name") or table_by_id.get(a.get("table_id")) or (tables[:1] or [""])[0]
            column = pc.get("column_name") or a.get("source_column") or ""
            cols.append({
                "id": a.get("id"),
                "column": f"{table}.{column}" if table else column,
                "name": a.get("name") or column,
                "description": unq(a.get("description") or ""),
                "type": a.get("datatype"),
                "sample_values": a.get("sample_values") or [],
            })
            for ref in a.get("referenced_columns") or []:
                if ref.get("table_name") and ref.get("column_name") and table and column:
                    links.append((ref["table_name"], ref["column_name"], table, column))
        sqls = [{
            "id": s.get("id"),
            "name": s.get("name") or "",
            "description": unq(s.get("description") or ""),
            "sql": unq(s.get("expression") or s.get("sql") or ""),
        } for s in r["sql_attrs"]]
        entry = {
            "id": t["id"],
            "name": t.get("name") or d.get("name") or t["id"],
            "description": unq(t.get("description") or d.get("description") or ""),
            "synonyms": t.get("synonyms") or d.get("synonyms") or [],
            "tables": tables,
            "related_terms": [{"id": x.get("id"), "name": x.get("name")} for x in d.get("related_terms") or []],
            "attrs": cols,
            "sql_attrs": sqls,
        }
        entries.append(entry)
        for tb in tables:
            table_term.setdefault(tb.lower(), entry)

    rels = []
    for from_t, from_c, to_t, to_c in sorted(set(links)):
        src, dst = table_term.get(from_t.lower()), table_term.get(to_t.lower())
        rels.append({
            "join": f"{from_t}.{from_c} = {to_t}.{to_c}",
            "from_table": from_t, "to_table": to_t,
            "from_term": src and {"id": src["id"], "name": src["name"]},
            "to_term": dst and {"id": dst["id"], "name": dst["name"]},
        })
    return {"fetched": time.monotonic(), "terms": entries,
            "by_id": {e["id"]: e for e in entries}, "relationships": rels}


def _snapshot() -> dict:
    global _snap
    with _lock:
        if _snap is None or time.monotonic() - _snap["fetched"] > CACHE_TTL_S:
            _snap = _build_snapshot()
        return _snap


# ---------------------------------------------------------------- mapping to tool shapes

def _sql_text(entry: dict) -> str | None:
    parts = [f"-- {s['name']}\n{s['sql']}" if s["name"] else s["sql"] for s in entry["sql_attrs"] if s["sql"]]
    return "\n".join(parts) or None


def _column_detail(c: dict) -> dict:
    out = {"column": c["column"], "name": c["name"], "description": c["description"]}
    if c["type"]:
        out["type"] = c["type"]
    samples = [str(v) for v in c["sample_values"]][:10]
    if samples and not all(v in c["description"] for v in samples):  # often already in the text
        out["sample_values"] = samples
    return out


def _relationships(snap: dict, entry: dict) -> list[dict]:
    mine = {t.lower() for t in entry["tables"]}
    out = []
    for r in snap["relationships"]:
        if r["from_table"].lower() in mine:
            other = r["to_term"]
        elif r["to_table"].lower() in mine:
            other = r["from_term"]
        else:
            continue
        out.append({"join": r["join"], "term_id": other and other["id"], "term": other and other["name"]})
    return out


def _full_entry(snap: dict, entry: dict) -> dict:
    out = {
        "id": entry["id"],
        "name": entry["name"],
        "kind": "term",
        "description": entry["description"],
        "synonyms": entry["synonyms"],
        "tables": entry["tables"],
        "columns": [c["column"] for c in entry["attrs"]],
        "sql": _sql_text(entry),
        "column_details": [_column_detail(c) for c in entry["attrs"]],
        "relationships": _relationships(snap, entry),
    }
    joined = {r["term_id"] for r in out["relationships"]}
    other = [t["name"] for t in entry["related_terms"] if t["id"] not in joined]
    if other:
        out["related_terms"] = other
    if entry["sql_attrs"]:
        out["sql_attributes"] = entry["sql_attrs"]
    return out


# ---------------------------------------------------------------- search ranking

_STOP = {"a", "an", "and", "are", "as", "at", "be", "by", "each", "for", "from", "has", "have",
         "how", "in", "is", "it", "its", "many", "much", "of", "on", "or", "per", "that", "the",
         "their", "this", "to", "what", "which", "who", "with"}


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _query_tokens(query: str) -> list[str]:
    toks = [w for w in _words(query) if w not in _STOP and len(w) > 1]
    return [w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w for w in toks]


def _lexical(tokens: list[str], text: str) -> float:
    """Share of the query words that start a word of the text (plural-insensitive)."""
    if not tokens:
        return 0.0
    words = set(_words(text))
    return sum(any(w.startswith(t) for w in words) for t in tokens) / len(tokens)


def _cos(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def _texts(entry: dict) -> list[tuple[str, dict | None, str]]:
    """(kind, object, text to rank) for a term itself, its columns and its SQL attributes."""
    head = f"{entry['name']}. {', '.join(entry['synonyms'])}. {entry['description']}"
    out = [("term", None, head)]
    out += [("column", c, f"{entry['name']} - {c['name']} ({c['column']}): {c['description']}")
            for c in entry["attrs"]]
    out += [("sql_attribute", s, f"{entry['name']} - {s['name']}: {s['description']}")
            for s in entry["sql_attrs"]]
    return out


def _embed_all(texts: list[str]) -> dict[str, list[float]]:
    with _lock:
        missing = [t for t in dict.fromkeys(texts) if t not in _vectors]
    if missing:
        vecs = embed(missing)
        with _lock:
            if len(_vectors) > 5000:
                _vectors.clear()
            _vectors.update(zip(missing, vecs))
    with _lock:
        return {t: _vectors[t] for t in texts}


def _stack_hits(query: str) -> set[str]:
    """Ids (and table.column keys) the stack's own search returns for the query."""
    if len(query.strip()) < 2:
        return set()
    with _client() as client:
        body = _call(client, "POST", "/api/search/global-search",
                     json={"search_term": query.strip(), "filters": {"description": True}})
    hits = set()
    for item in _rows(body):
        hits.add(item.get("id"))
        crumbs = item.get("breadcrumbs") or []
        if item.get("type") == "Column" and crumbs:
            hits.add(f"{crumbs[-1].get('name')}.{item.get('name')}".lower())
    return hits


# ---------------------------------------------------------------- tools

def search_terms(query: str) -> dict:
    """Search the business ontology generated by the NVIDIA Auto Ontology library.
    Each term is one table; matched_columns lists the columns of that table whose
    business name or meaning matched the query. Use it for every business concept
    in the question. Returns the 5 closest terms with the ids get_term needs.
    """
    try:
        snap = _snapshot()
    except LibraryError as exc:
        return {"terms": [], "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - tools must never raise
        return {"terms": [], "error": f"reading the Auto Ontology stack failed: {exc}"}
    notes = []
    try:
        hits = _stack_hits(query)
    except Exception as exc:  # noqa: BLE001 - our own ranking still works without it
        hits = set()
        notes.append(f"the stack's own search failed: {exc}")
    tokens = _query_tokens(query)
    cands = {e["id"]: _texts(e) for e in snap["terms"]}
    try:
        qvec = embed([query])[0]
        vecs = _embed_all([text for items in cands.values() for _, _, text in items])
    except Exception as exc:  # noqa: BLE001
        qvec, vecs = None, {}
        notes.append(f"semantic ranking unavailable ({exc}); ranked by word overlap only")

    results = []
    for entry in snap["terms"]:
        scored = []
        for kind, obj, text in cands[entry["id"]]:
            hit = (entry["id"] if obj is None else obj["id"]) in hits or (
                kind == "column" and obj["column"].lower() in hits)
            score = (_cos(qvec, vecs[text]) if qvec else 0.0) \
                + LEX_WEIGHT * _lexical(tokens, text) + STACK_HIT_WEIGHT * hit
            scored.append((score, kind, obj))
        best = max(s for s, _, _ in scored)
        matched = sorted((x for x in scored if x[1] != "term" and x[0] >= best - 0.08),
                         key=lambda x: x[0], reverse=True)[:MAX_MATCHED]
        results.append({
            "id": entry["id"],
            "name": entry["name"],
            "kind": "term",
            "score": round(best, 4),
            "description": entry["description"],
            "tables": entry["tables"],
            "matched_columns": [
                {"column": o["column"], "name": o["name"], "description": o["description"][:300]}
                if k == "column" else {"sql_attribute": o["name"], "description": o["description"][:300]}
                for _, k, o in matched
            ],
        })
    results.sort(key=lambda r: r["score"], reverse=True)
    out = {"terms": results[:TOP_TERMS]}
    if notes:
        out["note"] = "; ".join(notes)
    return out


def _resolve(snap: dict, term_id: str) -> dict | None:
    key = (term_id or "").strip().strip("'\"")
    if key in snap["by_id"]:
        return snap["by_id"][key]
    low = key.lower()
    for e in snap["terms"]:
        if low == e["name"].lower() or low in (t.lower() for t in e["tables"]):
            return e
    if len(low) >= 8:
        found = [e for e in snap["terms"] if e["id"].lower().startswith(low)]
        if len(found) == 1:
            return found[0]
    return None


def get_term(term_id: str) -> dict:
    """Get the full ontology term for an id returned by search_terms: its table,
    column_details (for every column: business name, meaning, coded values and
    what they stand for, sample values), relationships (join conditions to other
    tables) and SQL attributes if the library has any.
    """
    try:
        snap = _snapshot()
        entry = _resolve(snap, term_id)
    except LibraryError as exc:
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - tools must never raise
        return {"error": f"reading the Auto Ontology stack failed: {exc}"}
    if entry is None:
        known = ", ".join(f"{e['name']} ({e['id']})" for e in snap["terms"][:20])
        return {"error": f"no ontology term with id '{term_id}'. Use an id from search_terms. "
                         f"Known terms: {known}"}
    return _full_entry(snap, entry)


# ---------------------------------------------------------------- server helpers

def ontology_entries() -> dict:
    """All terms (with column details), relationships and SQL attributes, in the
    {terms: [...]} shape of /api/ontology."""
    try:
        snap = _snapshot()
    except LibraryError as exc:
        return {"terms": [], "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"terms": [], "error": f"reading the Auto Ontology stack failed: {exc}"}
    out = [_full_entry(snap, e) for e in snap["terms"]]
    for r in snap["relationships"]:
        a, b = r["from_term"], r["to_term"]
        left, right = r["join"].split(" = ")
        out.append({
            "id": f"rel:{r['join']}",
            "name": f"{a['name'] if a else r['from_table']} → {b['name'] if b else r['to_table']}",
            "kind": "relationship",
            "description": f"Join {r['from_table']} to {r['to_table']}.",
            "synonyms": [],
            "columns": [left, right],
            "sql": r["join"],
        })
    for e in snap["terms"]:
        for s in e["sql_attrs"]:
            out.append({"id": s["id"], "name": s["name"], "kind": "sql_attribute",
                        "description": s["description"], "synonyms": [], "columns": [],
                        "sql": s["sql"] or None})
    return {"terms": out}


def health(timeout: float = 1.5) -> dict:
    """{"reachable": bool, "terms": int} from one quick request; never raises."""
    try:
        with _client(httpx.Timeout(timeout)) as client:
            body = _call(client, "GET", "/api/terms", params={"limit": 1})
        if not isinstance(body, dict) or "total" not in body:
            raise LibraryError(f"unexpected reply from {AUTO_ONTOLOGY_URL}/api/terms")
        return {"reachable": True, "terms": int(body["total"])}
    except LibraryError as exc:
        return {"reachable": False, "terms": 0, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"reachable": False, "terms": 0, "error": f"{type(exc).__name__}: {exc}"}
