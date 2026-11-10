"""Google ADK agents on an Ollama model: `ontology` (curated store), `generated` (published
entries of the auto-generated store), `baseline` (schema only) and `library` (the ontology
of a running NVIDIA Auto Ontology stack, read over REST by app/library_tools.py)."""

from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app import generator, library_tools
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
1. Call search_terms for each business concept in the question (every
   measure, category, status, kind of entity or link between entities it mentions).
2. Call get_term on every relevant result to read its columns, code values and
   SQL snippet. Use those exact columns, codes, join paths and filters (copy
   string values exactly, including upper/lower case) in your query.
3. Call get_schema only if you still need a table or column name.
4. Write the SQL and call run_sql.
"""
    + COMMON_RULES
)

GENERATED_INSTRUCTION = ONTOLOGY_INSTRUCTION.replace(
    "A curated business ontology",
    "A business ontology (drafted from the database, then certified by a reviewer)",
)

LIBRARY_INSTRUCTION = (
    """You are a data analyst. You answer questions by querying
the database. A business ontology that the NVIDIA Auto Ontology library generated
from the database tells you what the tables, columns and coded values mean. In
this ontology each term is one table; the meanings are in the term's
column_details (for every column: business name, description with what each
coded value stands for, sample values) and its relationships give the join
conditions to other tables. It has few or no ready-made SQL snippets.

Workflow - follow it for every question:
1. Call search_terms for each business concept in the question (a measure, an
   attribute, a category or status, how two things are linked). Each result is
   a table; matched_columns shows which of its columns matched.
2. Call get_term on every relevant result and read its column_details and
   relationships. Use those exact columns and join conditions, and copy coded
   values exactly as the descriptions give them (including upper/lower case).
   If a phrase in the question has no column of its own (for example a status
   or category described in words), go through every coded value of that
   column, keep each one whose description fits the phrase (often more than
   one) and filter on all of them.
   Read the term of every table your query will use, including tables you
   only join through: their columns may hold a condition the question implies.
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
    if mode == "generated":
        return Agent(
            name="generated_analyst",
            model=_model(),
            instruction=GENERATED_INSTRUCTION,
            tools=[generator.search_terms, generator.get_term, get_schema, run_sql],
            generate_content_config=gen_config,
        )
    if mode == "library":
        return Agent(
            name="library_analyst",
            model=_model(),
            instruction=LIBRARY_INSTRUCTION,
            tools=[library_tools.search_terms, library_tools.get_term, get_schema, run_sql],
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
    for mode in ("ontology", "generated", "baseline", "library")
}
