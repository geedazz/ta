#!/usr/bin/env python3
"""Papildo dienos JSON Claude ivertinimu.

Vertinami tik tie projektai, kurie jau praejo raktazodziu filtra, todel
uzklausu skaicius lieka mazas. Be ANTHROPIC_API_KEY skriptas tyliai nutyla.
"""

import json
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DAY_DIR = os.path.join(ROOT, "docs", "data")
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = "claude-sonnet-4-6"

PROFILE = """Vertintojas yra Lietuvoje dirbanti imones teises vadove.
Jos sritys: silumos ukis ir energetikos reguliavimas, biokuro birza,
daugiabuciu namu administravimas ir bendrojo naudojimo objektu valdymas,
viesieji pirkimai, vertybiniu popieriu rinka ir emitentu prievoles,
asmens duomenu apsauga, imoniu teise ir M&A."""

INSTRUCTION = """Kiekvienam projektui pateik:
- "score": 0-3 (0 nesvarbu, 1 verta zinoti, 2 svarbu, 3 butina reaguoti)
- "why": vienas sakinys lietuviskai, kodel butent sitai vertintojai tai aktualu

Atsakyk TIK JSON masyvu, be jokio ivadinio teksto ir be markdown zymu.
Formatas: [{"oid":"...","score":2,"why":"..."}]"""


def ask(items):
    payload = {
        "model": MODEL,
        "max_tokens": 2000,
        "system": PROFILE,
        "messages": [{
            "role": "user",
            "content": INSTRUCTION + "\n\nProjektai:\n" + json.dumps(items, ensure_ascii=False),
        }],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode(),
        headers={
            "content-type": "application/json",
            "x-api-key": API_KEY,
            "anthropic-version": "2023-06-01",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        body = json.load(r)
    text = "".join(b.get("text", "") for b in body.get("content", []) if b.get("type") == "text")
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(text)


def latest_day_file():
    days = sorted(f for f in os.listdir(DAY_DIR) if f[:2] == "20" and f.endswith(".json"))
    return os.path.join(DAY_DIR, days[-1]) if days else None


def main():
    if not API_KEY:
        print("ANTHROPIC_API_KEY nenustatytas, praleidziama")
        return

    path = latest_day_file()
    if not path:
        print("Nera ka vertinti")
        return

    with open(path, encoding="utf-8") as f:
        day = json.load(f)

    todo = [d for d in day["docs"] if d.get("hits") and "score" not in d]
    if not todo:
        print("Nauju projektu vertinimui nera")
        return

    scored = {}
    for i in range(0, len(todo), 15):
        chunk = [{
            "oid": d["oid"],
            "title": d["title"][:400],
            "sort": d["sort"],
            "initiator": (d["initiators"] or [""])[0],
            "status": d.get("statusLt", ""),
            "due": d.get("nextDue", ""),
        } for d in todo[i:i + 15]]
        try:
            for r in ask(chunk):
                scored[r["oid"]] = r
        except Exception as exc:  # noqa: BLE001
            print("Vertinimo klaida:", exc, file=sys.stderr)

    for d in day["docs"]:
        r = scored.get(d["oid"])
        if r:
            d["score"] = r.get("score", 0)
            d["why"] = r.get("why", "")

    day["scored"] = len(scored)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(day, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Ivertinta {len(scored)} projektu")


if __name__ == "__main__":
    main()
