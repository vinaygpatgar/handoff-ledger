import json
import re
import sqlite3
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Handoff Ledger API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_FILE = "ledger.db"
SAMPLE_DIR = Path(__file__).resolve().parent.parent / "sample-data"


def db():
    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = db()
    con.execute(
        "CREATE TABLE IF NOT EXISTS chunks ("
        "id TEXT PRIMARY KEY, source TEXT, text TEXT, visibility TEXT)"
    )
    con.commit()
    con.close()


init_db()


def redact(text):
    # Hide phone numbers like "90000 00000" before any AI model sees them
    return re.sub(r"\b\d{5}\s?\d{5}\b", "[REDACTED_PHONE]", text)


def split_file(path):
    content = path.read_text(encoding="utf-8")
    if path.suffix == ".md":
        return [
            line.strip()
            for line in content.splitlines()
            if line.strip() and not line.startswith("#")
        ]
    if path.suffix == ".csv":
        return [line.strip() for line in content.splitlines()[1:] if line.strip()]
    if path.suffix == ".json":
        return [json.dumps(item) for item in json.loads(content)]
    return []


@app.get("/")
def health():
    return {"status": "ok", "app": "Handoff Ledger"}


@app.post("/load-sample")
def load_sample():
    con = db()
    con.execute("DELETE FROM chunks")
    total = 0
    for path in sorted(SAMPLE_DIR.iterdir()):
        if path.suffix not in (".md", ".csv", ".json"):
            continue
        visibility = "lead-only" if "private" in path.name else "team"
        for i, piece in enumerate(split_file(path), start=1):
            chunk_id = f"{path.stem}-{i}"
            con.execute(
                "INSERT INTO chunks VALUES (?, ?, ?, ?)",
                (chunk_id, path.name, redact(piece), visibility),
            )
            total += 1
    con.commit()
    con.close()
    return {"loaded_chunks": total}


@app.get("/chunks")
def get_chunks(role: str = "incoming"):
    con = db()
    if role == "lead":
        rows = con.execute("SELECT * FROM chunks").fetchall()
    else:
        rows = con.execute(
            "SELECT * FROM chunks WHERE visibility = 'team'"
        ).fetchall()
    con.close()
    return [dict(r) for r in rows]
