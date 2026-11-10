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
# Auto-generated ontology: workspace store (profiles, drafts, certified entries), optional
# per-table documentation (<table>.csv|md|txt) and the YAML export of certified entries.
GENERATED_DB = Path(os.environ.get("GENERATED_DB", DATA_DIR / "generated.db")).resolve()
DOCS_DIR = Path(os.environ.get("DOCS_DIR", DATA_DIR / "database_description")).resolve()
GENERATED_YAML = Path(os.environ.get("GENERATED_YAML", ROOT / "ontology" / "generated.yaml")).resolve()

# Dataset notes: what the agent is told about the database, and starter questions for the
# chat screen. Read from <ontology>.meta.yaml next to the ontology YAML (override the path
# with DATASET_META, or the notes alone with DB_NOTES). Nothing about any dataset lives in code.
DATASET_META = Path(os.environ.get("DATASET_META", ONTOLOGY_YAML.with_suffix(".meta.yaml"))).resolve()


def _load_meta() -> dict:
    try:
        import yaml

        data = yaml.safe_load(DATASET_META.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, ImportError):
        return {}
    except Exception:  # noqa: BLE001 - a malformed meta file must not stop the app
        return {}


_META = _load_meta()
DB_NOTES = " ".join(str(os.environ.get("DB_NOTES") or _META.get("notes") or "").split())
SUGGESTIONS = [str(q) for q in (_META.get("suggestions") or []) if q]
STATIC_DIR = ROOT / "static"

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
# On Windows "localhost" tries IPv6 (::1) first; Ollama listens on IPv4 only, which
# adds ~2 s to every request. Use the IPv4 loopback address directly.
OLLAMA_BASE_URL = OLLAMA_BASE_URL.replace("//localhost", "//127.0.0.1")
CHAT_MODEL = os.environ.get("CHAT_MODEL", "qwen3.5:9b")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "bge-m3")

# `library` chat mode: a running NVIDIA Auto Ontology stack (REST API). The API token comes
# from the environment only; never write it into this repository.
AUTO_ONTOLOGY_URL = os.environ.get("AUTO_ONTOLOGY_URL", "http://127.0.0.1:3200").rstrip("/")
AUTO_ONTOLOGY_URL = AUTO_ONTOLOGY_URL.replace("//localhost", "//127.0.0.1")
AUTO_ONTOLOGY_API_TOKEN = os.environ.get("AUTO_ONTOLOGY_API_TOKEN", "").strip()
