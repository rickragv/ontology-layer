"""FastAPI server: static frontend + JSON/SSE API for the ontology demo."""

import json
import re
import sqlite3
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from typing import Literal

import httpx
from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from google.adk.agents.run_config import RunConfig
from google.genai import types
from pydantic import BaseModel

from app import generator, library_tools
from app.agents import USER_ID, runners
from app.config import (
    CHAT_MODEL, EMBED_MODEL, FINANCIAL_DB, OLLAMA_BASE_URL, ONTOLOGY_DB, STATIC_DIR, SUGGESTIONS,
)
from app.tools import list_entries

app = FastAPI(title="Ontology layer demo")
app.mount("/static", StaticFiles(directory=STATIC_DIR, check_dir=False), name="static")
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
_health_pool = ThreadPoolExecutor(max_workers=2)

_THINK = re.compile(r"<think>.*?(</think>|$)", re.DOTALL)


@app.get("/")
def index():
    page = STATIC_DIR / "index.html"
    if not page.exists():
        return JSONResponse({"error": "static/index.html not found"}, status_code=404)
    # no-cache: the browser must revalidate, so page edits show up on the next load
    return FileResponse(page, headers={"Cache-Control": "no-cache"})


@app.get("/api/health")
def health():
    # The Auto Ontology check (1.5 s timeout) runs alongside the checks below.
    library = _health_pool.submit(library_tools.health)
    try:
        ollama = httpx.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=3).status_code == 200
    except httpx.HTTPError:
        ollama = False
    try:
        with closing(sqlite3.connect(f"file:{FINANCIAL_DB.as_posix()}?mode=ro", uri=True)) as conn:
            db = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0] > 0
    except sqlite3.Error:
        db = False
    try:
        with closing(sqlite3.connect(f"file:{ONTOLOGY_DB.as_posix()}?mode=ro", uri=True)) as conn:
            terms = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    except sqlite3.Error:
        terms = 0
    try:
        generated_terms = generator.published_count()
    except sqlite3.Error:
        generated_terms = 0
    return {"ollama": ollama, "model": CHAT_MODEL, "embed_model": EMBED_MODEL, "db": db,
            "terms": terms, "generated_terms": generated_terms, "library": library.result()}


@app.get("/api/ontology")
def ontology(source: Literal["curated", "generated", "library"] = "curated"):
    if source == "library":
        return library_tools.ontology_entries()  # {"terms": [...]} plus "error" if the stack fails
    try:
        return {"terms": generator.published_entries() if source == "generated" else list_entries()}
    except sqlite3.Error:
        return {"terms": []}


@app.get("/api/suggestions")
def suggestions():
    return {"questions": SUGGESTIONS}


class ChatRequest(BaseModel):
    session_id: str
    message: str
    mode: Literal["ontology", "generated", "baseline", "library"]


async def chat_events(req: ChatRequest):
    """Run the agent and translate ADK events into the API's event dicts."""
    runner = runners[req.mode]
    message = types.Content(role="user", parts=[types.Part(text=req.message)])
    # ADK gives every function call an id and copies it onto the matching response;
    # the fallback below only matters if a call ever arrives without one.
    unnamed: dict[str, list[str]] = {}  # tool name -> generated ids awaiting a result
    answered = False
    async for event in runner.run_async(
        user_id=USER_ID,
        session_id=f"{req.mode}:{req.session_id}",
        new_message=message,
        run_config=RunConfig(max_llm_calls=15),  # stop runaway tool loops
    ):
        if event.error_message:
            answered = True
            yield {"type": "error", "message": event.error_message}
        if not event.content or not event.content.parts:
            continue
        has_calls = bool(event.get_function_calls())
        for part in event.content.parts:
            if part.thought:
                continue
            if part.function_call:
                call = part.function_call
                call_id = call.id
                if not call_id:
                    call_id = f"call_{uuid.uuid4().hex[:8]}"
                    unnamed.setdefault(call.name, []).append(call_id)
                yield {"type": "tool_call", "id": call_id, "name": call.name, "args": call.args or {}}
            elif part.function_response:
                resp = part.function_response
                resp_id = resp.id or (unnamed.get(resp.name) or [None]).pop(0)
                yield {"type": "tool_result", "id": resp_id, "name": resp.name, "result": resp.response or {}}
            elif part.text and not has_calls:
                text = _THINK.sub("", part.text)
                if text.strip():
                    answered = True
                    yield {"type": "text", "delta": text}
    if not answered:
        yield {"type": "error", "message": "The model finished without giving an answer."}


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"


@app.post("/api/chat")
async def chat(req: ChatRequest):
    async def stream():
        started = time.monotonic()
        last_error = None
        try:
            async for payload in chat_events(req):
                if payload["type"] == "error":
                    last_error = payload["message"]
                yield _sse(payload)
        except Exception as exc:  # noqa: BLE001 - report every failure to the client
            if str(exc) != last_error:  # ADK may already have sent it as an error event
                yield _sse({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        yield _sse({"type": "done", "elapsed_ms": int((time.monotonic() - started) * 1000)})

    return StreamingResponse(stream(), media_type="text/event-stream", headers=SSE_HEADERS)


# ---------------------------------------------------------------- auto-generated ontology
# Profile -> Generate -> Certify -> Publish; the logic is in app/generator.py.

@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    if not request.url.path.startswith("/api/gen/"):
        return await request_validation_exception_handler(request, exc)
    message = "; ".join(f"{'.'.join(map(str, e['loc'][1:])) or 'body'}: {e['msg']}" for e in exc.errors())
    return JSONResponse({"error": message}, status_code=422)


class GenerateRequest(BaseModel):
    tables: list[str] | None = None
    use_docs: bool = False


class EntryPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    synonyms: list[str] | None = None
    columns: list[str] | None = None
    sql: str | None = None
    status: Literal["draft", "certified", "rejected"] | None = None


class BulkRequest(BaseModel):
    ids: list[str] | None = None
    status: Literal["draft", "certified", "rejected"]


@app.get("/api/gen/state")
def gen_state():
    return generator.state()


@app.post("/api/gen/profile")
def gen_profile():
    try:
        generator.profile()
    except sqlite3.Error as exc:
        return JSONResponse({"error": f"profiling failed: {exc}"}, status_code=500)
    return generator.state()


@app.post("/api/gen/generate")
def gen_generate(req: GenerateRequest):
    # A sync generator: Starlette runs it in a worker thread, so blocking model calls are fine.
    events = (_sse(event) for event in generator.generate(req.tables, req.use_docs))
    return StreamingResponse(events, media_type="text/event-stream", headers=SSE_HEADERS)


@app.patch("/api/gen/entries/{entry_id}")
def gen_patch_entry(entry_id: str, body: EntryPatch):
    try:
        return generator.update_entry(entry_id, body.model_dump(exclude_unset=True))
    except KeyError:
        return JSONResponse({"error": f"no entry with id '{entry_id}'"}, status_code=404)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.post("/api/gen/entries/bulk")
def gen_bulk(req: BulkRequest):
    return {"updated": generator.bulk_status(req.ids, req.status)}


@app.post("/api/gen/publish")
def gen_publish():
    try:
        return generator.publish()
    except (httpx.HTTPError, sqlite3.Error) as exc:
        return JSONResponse({"error": f"publish failed: {exc}"}, status_code=502)


@app.post("/api/gen/reset")
def gen_reset():
    generator.reset()
    return {"ok": True}
