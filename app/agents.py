"""Two Google ADK agents on a local Ollama model: `ontology` and `baseline`."""

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.config import CHAT_MODEL, DB_NOTES, OLLAMA_BASE_URL
from app.tools import get_schema, get_term, run_sql, search_terms

APP_NAME = "app"  # matches this package's directory, as ADK expects
USER_ID = "demo"

COMMON_RULES = f"""
The database is SQLite. {DB_NOTES}
Never guess what a column or code means.
Rules for SQL: one SELECT (or WITH) statement per run_sql call; use SQLite date
functions (STRFTIME, DATE, JULIANDAY).
If run_sql returns an error, fix the query and call run_sql again. If it returns
no rows, NULL or 0 where you expected data, check the stored values (for example
SELECT DISTINCT column FROM table) and retry with the exact spelling and case.
Final answer: one to three short sentences that state the number or result
directly. Do not invent results; only report what run_sql returned.
"""

ONTOLOGY_INSTRUCTION = (
    """You are a data analyst. You answer questions by querying
the database. A curated business ontology tells you exactly which columns,
codes, joins and reviewed SQL expressions correspond to business terms.

Workflow - follow it for every question:
1. Call search_terms for each business concept in the question (for example
   "average salary", "weekly statement", "owner of account", "loan status").
2. Call get_term on every relevant result to read its columns, code values and
   SQL snippet. Use those exact columns, codes, join paths and filters (copy
   string values exactly, including upper/lower case) in your query.
3. Call get_schema only if you still need a table or column name.
4. Write the SQL and call run_sql.
"""
    + COMMON_RULES
)

BASELINE_INSTRUCTION = (
    """You are a data analyst. You answer questions by querying
the database.

Workflow - follow it for every question:
1. Call get_schema to see the tables and columns.
2. Write the SQL and call run_sql.
"""
    + COMMON_RULES
)


def _model() -> LiteLlm:
    extra = {}
    if CHAT_MODEL.startswith("qwen3"):
        # Thinking makes each tool step much slower; the tools carry the reasoning load.
        extra["reasoning_effort"] = "none"
    return LiteLlm(
        model=f"ollama_chat/{CHAT_MODEL}",
        api_base=OLLAMA_BASE_URL,
        num_ctx=16384,  # Ollama's default 4096 would silently cut off the instructions
        timeout=300,
        **extra,
    )


def build_agent(mode: str) -> Agent:
    gen_config = types.GenerateContentConfig(temperature=0.0)
    if mode == "ontology":
        return Agent(
            name="ontology_analyst",
            model=_model(),
            instruction=ONTOLOGY_INSTRUCTION,
            tools=[search_terms, get_term, get_schema, run_sql],
            generate_content_config=gen_config,
        )
    if mode == "baseline":
        return Agent(
            name="baseline_analyst",
            model=_model(),
            instruction=BASELINE_INSTRUCTION,
            tools=[get_schema, run_sql],
            generate_content_config=gen_config,
        )
    raise ValueError(f"unknown mode: {mode}")


session_service = InMemorySessionService()
runners = {
    mode: Runner(
        app_name=APP_NAME,
        agent=build_agent(mode),
        session_service=session_service,
        auto_create_session=True,
    )
    for mode in ("ontology", "baseline")
}
