#!/usr/bin/env python3
"""Nuskaito dienos TAIS projektu srauta ir issaugo ji kaip JSON.

Srautas generuojamas dinamiskai uzklausos metu ir apima tik einamaja diena,
todel praleista diena prarandama negrizatamai. Skriptas leidziamas kelis
kartus per diena; rezultatai sulyginami pagal oid, velesnis paleidimas
papildo ankstesni.

Duomenu saltinis: www.lrs.lt, teikejas: Seimo kanceliarija.
Licencija CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/).
"""

import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from filters import excluded, fold, in_scope, load_list

FEED_URL = "https://apps.lrs.lt/sip/p2b.tais_docs"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_DIR = os.path.join(ROOT, "data", "raw")
DAY_DIR = os.path.join(ROOT, "docs", "data")

# e-seimas atmeta uzklausas be iprasto narsykles antrasctes
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "application/xml,text/xml,*/*",
    "Accept-Language": "lt,en;q=0.8",
}

STATUS_LT = {
    "registered": "Uzregistruotas",
    "inReviewal": "Derinamas",
    "reviewed": "Suderintas",
    "preparedLA": "Parengtas teises aktas",
    "attachedMainTAP": "Prijungtas prie paketo",
    "oldVariant": "Senesnis variantas",
    "prepared": "Parengtas",
}


def download(url: str) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.read().decode("utf-8", errors="replace")


def repair(text: str) -> str:
    """Sutvarko srauto trukumus, del kuriu XML netampa validus."""
    text = text.replace("\u00a0", " ")
    i = text.find("<TAISProjEvents")
    if i > 0:
        text = text[i:]
    text = re.sub(r"<script\b[^>]*/>", "", text)
    text = re.sub(r"<script\b[\s\S]*?</script>", "", text)
    text = re.sub(
        r'title="([\s\S]*?)"(?=\s+(?:projectRegistrationNo|registrationNo)=)',
        lambda m: 'title="' + m.group(1).replace('"', "&quot;") + '"',
        text,
    )
    return text


def parse_xml(text: str):
    try:
        return ET.fromstring(text)
    except ET.ParseError:
        return ET.fromstring(repair(text))


def txt(el, path):
    node = el.find(path)
    return node.text.strip() if node is not None and node.text else ""


def iso_date(s: str) -> str:
    return (s or "")[:10].replace(".", "-")


def read_document(el, today: str) -> dict:
    # Suvestinei uztenka santraukos; pilna informacija lieka data/raw XML archyve.
    status_chain = [
        {
            "from": txt(e, "dateFrom"),
            "op": (e.find("operation").get("clsValue") if e.find("operation") is not None else ""),
        }
        for e in el.findall("./chronology/statusChronology/entry")
    ][-4:]

    seen, coord = set(), []
    for e in el.findall("./chronology/coordinationChronology/entry"):
        org = e.find("executor").get("orgName") if e.find("executor") is not None else ""
        until = iso_date(txt(e, "dateUntil"))
        if (org, until) in seen:
            continue
        seen.add((org, until))
        coord.append({"by": org, "until": until})
    coord_count = len(coord)
    seen_until = [c["until"] for c in coord]
    coord = coord[:30]

    files = []
    for b in el.findall("./bodyAttachments/bodyAttachment")[:8]:
        pdf = None
        for c in b.findall("./convertedAttachments/convertedAttachment"):
            if txt(c, "fileFormat") == "ISO_PDF":
                pdf = txt(c, "url")
                break
        kind = b.find("type")
        files.append({
            "kind": kind.get("clsValue") if kind is not None else "",
            "url": pdf or b.get("url", ""),
        })

    links = []
    for l in el.findall("./Links/Link")[:20]:
        rd = l.find("relatedDoc")
        links.append({
            "type": l.get("typeValue", ""),
            "title": (rd.get("title", "")[:180] if rd is not None else ""),
            "url": rd.get("url", "") if rd is not None else "",
        })

    deadlines = sorted(d for d in seen_until if len(d) == 10)
    future = [d for d in deadlines if d >= today]

    return {
        "oid": el.get("oid", ""),
        "title": el.get("title", ""),
        "sort": el.get("sort", ""),
        "regNo": el.get("projectRegistrationNo", ""),
        "regDate": iso_date(el.get("projectRegistrationDate", "")),
        "status": el.get("currentStatus", ""),
        "statusLt": STATUS_LT.get(el.get("currentStatus", ""), el.get("currentStatus", "")),
        "url": el.get("url", ""),
        "initiators": [o.text.strip() for o in el.findall("./initiatorUnits/orgName") if o.text],
        "preparedBy": [n.text.strip() for n in el.findall("./preparedBy/name") if n.text],
        "statusChain": status_chain,
        "coord": coord,
        "coordCount": coord_count,
        "files": files,
        "links": links,
        "nextDue": future[0] if future else "",
        "lastDue": deadlines[-1] if deadlines else "",
    }


def match(doc: dict, keywords, excludes=()) -> list:
    if excluded(doc["title"], excludes):
        return []
    hay = fold(" ".join([
        doc["title"], doc["sort"],
        " ".join(doc["initiators"]), " ".join(doc["preparedBy"]),
        " ".join(l["title"] for l in doc["links"]),
    ]))
    return [k for k in keywords if fold(k) in hay]


def main():
    os.makedirs(RAW_DIR, exist_ok=True)
    os.makedirs(DAY_DIR, exist_ok=True)

    xml_text = download(FEED_URL)
    root = parse_xml(xml_text)

    events_date = root.get("eventsDate") or datetime.now(timezone.utc).date().isoformat()
    creation = root.get("creationDate", "")

    with open(os.path.join(RAW_DIR, events_date + ".xml"), "w", encoding="utf-8") as f:
        f.write(xml_text)

    keywords = load_list("keywords.txt")
    excludes = load_list("exclude.txt")
    municipalities = load_list("savivaldybes.txt")
    found = root.findall("./Documents/Document")
    if not found:
        raise RuntimeError(
            "Srauto strukturoje nerasta Documents/Document. "
            "Seimo kanceliarija ispeja apie galimus strukturos pakeitimus - "
            "patikrinkite issaugota data/raw XML."
        )
    docs = [read_document(d, events_date) for d in found]
    for d in docs:
        d["hits"] = match(d, keywords, excludes)
        # Treciadalis projektu ateina is savivaldybiu, todel tas pats
        # stebimu savivaldybiu ratas taikomas ir cia.
        d["scope"] = in_scope([d["title"]] + d["initiators"], municipalities)

    payload = {
        "source": "www.lrs.lt / Seimo kanceliarija, CC BY 4.0",
        "eventsDate": events_date,
        "creationDate": creation,
        "fetchedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": len(docs),
        "hitCount": sum(1 for d in docs if d["hits"]),
        "docs": docs,
    }

    day_path = os.path.join(DAY_DIR, events_date + ".json")

    # Srautas dinamiskas: velesnis paleidimas mato daugiau ivykiu, bet
    # anksciau matytas projektas is jo dingti neturetu. Sulyginame pagal oid,
    # kad nei vienas paleidimas nieko neistrintu.
    if os.path.exists(day_path):
        with open(day_path, encoding="utf-8") as f:
            previous = json.load(f)
        merged = {d["oid"]: d for d in previous.get("docs", [])}
        for d in docs:
            old = merged.get(d["oid"], {})
            for carried in ("score", "why"):
                if carried in old:
                    d[carried] = old[carried]
            merged[d["oid"]] = d
        docs = list(merged.values())
        payload["docs"] = docs
        payload["count"] = len(docs)
        payload["hitCount"] = sum(1 for d in docs if d["hits"])
        payload["runs"] = previous.get("runs", 1) + 1

    with open(day_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))

    index_path = os.path.join(DAY_DIR, "index.json")
    index = []
    if os.path.exists(index_path):
        with open(index_path, encoding="utf-8") as f:
            index = json.load(f).get("days", [])
    index = [d for d in index if d["date"] != events_date]
    index.append({
        "date": events_date,
        "count": payload["count"],
        "hits": payload["hitCount"],
        "creationDate": creation,
    })
    index.sort(key=lambda d: d["date"], reverse=True)

    with open(index_path, "w", encoding="utf-8") as f:
        json.dump({"updated": payload["fetchedAt"], "days": index}, f,
                  ensure_ascii=False, indent=1)

    print(f"{events_date}: {payload['count']} projektu, "
          f"{payload['hitCount']} atitinka raktazodzius "
          f"(paleidimas Nr. {payload.get('runs', 1)})")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print("KLAIDA:", exc, file=sys.stderr)
        sys.exit(1)
