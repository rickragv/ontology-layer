"""Download the BIRD dev set and extract the `financial` database into data/.

Produces:
  data/financial.sqlite
  data/database_description/*.csv   (column meanings for the financial tables)
  data/financial_dev.json           (all BIRD dev questions with db_id == "financial")
  questions/build.json              (half used while authoring the ontology)
  questions/test.json               (held-out half; not used for authoring)

The archive is only opened as zip/JSON/SQLite data; nothing in it is executed.
"""

import json
import shutil
import sqlite3
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RAW = DATA / "raw"
QUESTIONS = ROOT / "questions"
DEV_URL = "https://bird-bench.oss-cn-beijing.aliyuncs.com/dev.zip"
DB_ID = "financial"


def download(url: str, dest: Path) -> None:
    if dest.exists():
        print(f"already downloaded: {dest}")
        return
    print(f"downloading {url} (~350 MB) ...")
    tmp = dest.with_name(dest.name + ".part")
    with urllib.request.urlopen(url, timeout=60) as resp, open(tmp, "wb") as out:
        total = int(resp.headers.get("Content-Length", 0))
        done = 0
        while chunk := resp.read(1 << 20):
            out.write(chunk)
            done += len(chunk)
            if total:
                print(f"\r  {done / 1e6:7.1f} / {total / 1e6:.1f} MB", end="", flush=True)
    print()
    tmp.replace(dest)


def find_member(zf: zipfile.ZipFile, suffix: str) -> str:
    names = [n for n in zf.namelist() if n.replace("\\", "/").endswith(suffix) and "__MACOSX" not in n]
    if not names:
        sys.exit(f"could not find '{suffix}' in {zf.filename}")
    return min(names, key=len)


def copy_member(zf: zipfile.ZipFile, name: str, dest: Path) -> None:
    """Write one archive member to a fixed destination (never extractall)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    with zf.open(name) as src, open(tmp, "wb") as out:
        shutil.copyfileobj(src, out)
    tmp.replace(dest)


def main() -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    dev_zip = RAW / "dev.zip"
    download(DEV_URL, dev_zip)

    with zipfile.ZipFile(dev_zip) as outer:
        questions = json.loads(outer.read(find_member(outer, "/dev.json")))
        inner_name = find_member(outer, "dev_databases.zip")
        inner_path = RAW / "dev_databases.zip"
        if not inner_path.exists():
            print("extracting dev_databases.zip ...")
            copy_member(outer, inner_name, inner_path)

    with zipfile.ZipFile(inner_path) as inner:
        copy_member(inner, find_member(inner, f"{DB_ID}/{DB_ID}.sqlite"), DATA / "financial.sqlite")
        desc_dir = DATA / "database_description"
        for name in inner.namelist():
            norm = name.replace("\\", "/")
            if f"/{DB_ID}/database_description/" in norm and norm.lower().endswith(".csv"):
                copy_member(inner, name, desc_dir / Path(norm).name)

    # Sanity check: the file must be a readable SQLite database.
    conn = sqlite3.connect(f"file:{(DATA / 'financial.sqlite').as_posix()}?mode=ro", uri=True)
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    conn.close()
    print(f"financial.sqlite tables: {sorted(tables)}")

    fin = sorted((q for q in questions if q.get("db_id") == DB_ID), key=lambda q: q["question_id"])
    (DATA / "financial_dev.json").write_text(json.dumps(fin, indent=2, ensure_ascii=False), encoding="utf-8")

    # Deterministic 50/50 split: alternate by question_id order.
    QUESTIONS.mkdir(exist_ok=True)
    build, test = fin[0::2], fin[1::2]
    (QUESTIONS / "build.json").write_text(json.dumps(build, indent=2, ensure_ascii=False), encoding="utf-8")
    (QUESTIONS / "test.json").write_text(json.dumps(test, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"{len(fin)} financial questions -> build {len(build)}, test {len(test)}")


if __name__ == "__main__":
    main()
