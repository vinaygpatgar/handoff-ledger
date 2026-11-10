import json
import os
import sqlite3

import requests
from fastapi import APIRouter, HTTPException, Body

router = APIRouter()
DB_FILE = "ledger.db"
MODEL = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash")")

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
    chunk_count = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    if chunk_count == 0:
        con.close()
        raise HTTPException(400, "Database is empty. Please call POST /load-sample first.")

    rows = con.execute("SELECT id, text FROM chunks WHERE visibility = 'team'").fetchall()
    by_id = {r["id"]: r["text"] for r in rows}
    data = "\n".join(f"[{r['id']}] {r['text']}" for r in rows)

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM}]},
        "contents": [{"role": "user", "parts": [{"text": data}]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }
    try:
        resp = requests.post(url, headers={"x-goog-api-key": key}, json=body, timeout=90)
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
        label = "Verified" if quote and quote in by_id.get(chunk_id, "") else "Inferred"
        con.execute(
            "INSERT INTO claims (type, claim, chunk_id, quote, label, status) VALUES (?, ?, ?, ?, ?, 'pending')",
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
        rows = con.execute("SELECT * FROM claims WHERE status = ?", (status,)).fetchall()
    con.close()
    return [dict(r) for r in rows]


@router.post("/claims/{claim_id}/review")
def review_claim(claim_id: int, payload: dict = Body(...)):
    status = payload.get("status")
    new_claim_text = payload.get("claim")
    con = db()
    if status:
        con.execute("UPDATE claims SET status = ? WHERE id = ?", (status, claim_id))
    if new_claim_text:
        con.execute("UPDATE claims SET claim = ? WHERE id = ?", (new_claim_text, claim_id))
    con.commit()
    row = con.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
    con.close()
    if not row:
        raise HTTPException(404, "Claim not found")
    return dict(row)


@router.get("/handover")
def get_handover(role: str = "incoming"):
    con = db()
    if role == "lead":
        rows = con.execute("""
            SELECT c.*, ch.text as chunk_text, ch.source, ch.visibility 
            FROM claims c 
            JOIN chunks ch ON c.chunk_id = ch.id
        """).fetchall()
    else:
        rows = con.execute("""
            SELECT c.*, ch.text as chunk_text, ch.source, ch.visibility 
            FROM claims c 
            JOIN chunks ch ON c.chunk_id = ch.id
            WHERE c.status = 'approved' AND ch.visibility = 'team'
        """).fetchall()
    con.close()
    return [dict(r) for r in rows]
