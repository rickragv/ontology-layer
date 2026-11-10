"""Pre-create the two pgvector collections at the embedding model's dimension.

Auto-Ontology creates `vdb.data_objects_layer` and `vdb.semantic_layer` lazily on
first write with PostgresVDB's hard-coded default `vector_size=2048` (NVIDIA
nemotron embed). bge-m3 returns 1024-dim vectors, which pgvector would reject.
PostgresVDB only creates a table when it does not exist yet, and nothing in the
app drops these tables (resets delete rows), so creating them once with the app's
own code and `vector_size=1024` is enough. Run BEFORE the first ingest:

  docker compose up -d --wait postgres
  docker compose run --rm --no-deps -e PYTHONPATH=/app -v ./_out/scripts:/scripts:ro \
      --entrypoint python auto-ontology-migrate /scripts/init_vdb_1024.py
  (Git Bash: prefix with MSYS_NO_PATHCONV=1)
"""

import os

import psycopg

from auto_ontology.infra.postgres import get_postgres_connection_string
from auto_ontology.vdb import DATA_VDB_COLLECTION, SEMANTIC_VDB_COLLECTION, VDB_SCHEMA
from auto_ontology.vdb.postgres import PostgresVDB

DIM = int(os.environ.get("EMBED_DIMENSIONS", "1024"))
conn_str = get_postgres_connection_string()

for collection in (DATA_VDB_COLLECTION, SEMANTIC_VDB_COLLECTION):
    vdb = PostgresVDB(
        connection_string=conn_str,
        collection_name=collection,
        schema_name=VDB_SCHEMA,
        vector_size=DIM,
    )
    vdb.create_index()
    vdb.close()

with psycopg.connect(conn_str) as conn, conn.cursor() as cur:
    cur.execute(
        """
        SELECT c.relname, format_type(a.atttypid, a.atttypmod)
        FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s AND a.attname = 'embedding'
        """,
        (VDB_SCHEMA,),
    )
    for name, typ in cur.fetchall():
        print(f"{VDB_SCHEMA}.{name}.embedding -> {typ}")
