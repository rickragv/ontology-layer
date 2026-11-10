"""FastAPI server: static frontend + JSON/SSE API for the ontology demo."""

import json
import re
import sqlite3
import time
import uuid
from contextlib import closing
from typing import Literal

import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from google.adk.agents.run_config import RunConfig
from google.genai import types
from pydantic import BaseModel

from app.agents import USER_ID, runners
from app.config import (
    CHAT_MODEL, EMBED_MODEL, FINANCIAL_DB, OLLAMA_BASE_URL, ONTOLOGY_DB, STATIC_DIR, USING_DEMO_DB,
)
from app.tools import list_entries

app = FastAPI(title="Ontology layer demo")
app.mount("/static", StaticFiles(directory=STATIC_DIR, check_dir=False), name="static")

SUGGESTIONS = [
    "How many accounts with statements issued after each transaction are in East Bohemia?",
    "Which district has the highest average salary, and what is it?",
    "How many loans are running contracts where the client is in debt?",
    "How many female clients own an account with weekly statements?",
    "What percentage of clients living in the Prague region are women?",
    "Which district's unemployment rate rose the most from 1995 to 1996, in percentage points?",
]

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
    return {"ollama": ollama, "model": CHAT_MODEL, "embed_model": EMBED_MODEL, "db": db, "terms": terms}


@app.get("/api/ontology")
def ontology():
    try:
        return {"terms": list_entries()}
    except sqlite3.Error:
        return {"terms": []}


@app.get("/api/suggestions")
def suggestions():
    return {"questions": SUGGESTIONS if USING_DEMO_DB else []}


class ChatRequest(BaseModel):
    session_id: str
    message: str
    mode: Literal["ontology", "baseline"]


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

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
