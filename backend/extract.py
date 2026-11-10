import json
import os
import sqlite3

import requests
from fastapi import APIRouter, HTTPException

router = APIRouter()
DB_FILE = "ledger.db"
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

SYSTEM = (
    "You extract handover claims from project notes. "
    "Each line is written as [chunk_id] text. "
    "Return ONLY a JSON list, with no other text. Each item must have: "
    '"type" (one of: decision, owner, risk, question, contact, next_action, dependency), '
    '"claim" (one short sentence), '
    '"chunk_id" (the id of the line that supports it), '
    '"quote" (an exact copy of words from that line). '
    "Never guess. If a fact is not in the text, leave it out."
)


def db():
    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row
    return con


def init_claims():
    con = db()
    con.execute(
        "CREATE TABLE IF NOT EXISTS claims ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT, claim TEXT, "
        "chunk_id TEXT, quote TEXT, label TEXT, status TEXT)"
    )
    con.commit()
    con.close()


init_claims()


@router.post("/extract")
def extract():
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise HTTPException(500, "GEMINI_API_KEY is not set on the server")
    con = db()
    rows = con.execute(
        "SELECT id, text FROM chunks WHERE visibility = 'team'"
    ).fetchall()
    if not rows:
        con.close()
        raise HTTPException(400, "No chunks found. Run /load-sample first.")
    by_id = {r["id"]: r["text"] for r in rows}
    data = "\n".join(f"[{r['id']}] {r['text']}" for r in rows)

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{MODEL}:generateContent"
    )
    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM}]},
        "contents": [{"role": "user", "parts": [{"text": data}]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }
    try:
        resp = requests.post(
            url, headers={"x-goog-api-key": key}, json=body, timeout=90
        )
    except Exception as e:
        con.close()
        raise HTTPException(502, f"Could not reach Gemini: {e}")
    if resp.status_code != 200:
        con.close()
        raise HTTPException(502, f"Gemini error {resp.status_code}: {resp.text[:300]}")

    try:
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        text = text.replace("```json", "").replace("```", "").strip()
        items = json.loads(text)
    except Exception:
        con.close()
        raise HTTPException(502, "Gemini did not return valid JSON")

    con.execute("DELETE FROM claims")
    for it in items:
        quote = str(it.get("quote", ""))
        chunk_id = str(it.get("chunk_id", ""))
        # Label is set by code, not by the model
        label = "Verified" if quote and quote in by_id.get(chunk_id, "") else "Inferred"
        con.execute(
            "INSERT INTO claims (type, claim, chunk_id, quote, label, status) "
            "VALUES (?, ?, ?, ?, ?, 'pending')",
            (str(it.get("type", "")), str(it.get("claim", "")), chunk_id, quote, label),
        )
    con.commit()
    count = con.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
    con.close()
    return {"claims_created": count}


@router.get("/claims")
def get_claims(status: str = "all"):
    con = db()
    if status == "all":
        rows = con.execute("SELECT * FROM claims").fetchall()
    else:
        rows = con.execute(
            "SELECT * FROM claims WHERE status = ?", (status,)
        ).fetchall()
    con.close()
    return [dict(r) for r in rows]
