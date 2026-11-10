"""Paths and environment settings shared by the app and the scripts."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
# The SQLite database the agent queries. Point DB_PATH at your own file to use your data.
FINANCIAL_DB = Path(os.environ.get("DB_PATH", DATA_DIR / "financial.sqlite")).resolve()
# The ontology you edit (YAML) and the searchable store built from it.
ONTOLOGY_YAML = Path(os.environ.get("ONTOLOGY_YAML", ROOT / "ontology" / "financial.yaml")).resolve()
ONTOLOGY_DB = Path(os.environ.get("ONTOLOGY_DB", DATA_DIR / "ontology.db")).resolve()
USING_DEMO_DB = "DB_PATH" not in os.environ

# One or two sentences telling the agent what database it is on. Set DB_NOTES for your own data.
DEFAULT_DB_NOTES = (
    "The database is the BIRD `financial` database of a Czech bank: tables account, "
    "card, client, disp, district, loan, order, trans. Many stored values are Czech "
    "codes and district columns are named A2..A16. Quote the table \"order\" as `order`. "
    "Money amounts are in Czech koruna: write them as plain numbers or with \"CZK\", "
    "never with any other currency symbol."
)
DB_NOTES = os.environ.get("DB_NOTES", DEFAULT_DB_NOTES)
STATIC_DIR = ROOT / "static"

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
# On Windows "localhost" tries IPv6 (::1) first; Ollama listens on IPv4 only, which
# adds ~2 s to every request. Use the IPv4 loopback address directly.
OLLAMA_BASE_URL = OLLAMA_BASE_URL.replace("//localhost", "//127.0.0.1")
CHAT_MODEL = os.environ.get("CHAT_MODEL", "qwen3.5:9b")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "bge-m3")
