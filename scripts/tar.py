#!/usr/bin/env python3
"""Nuskaito Teises aktu registro (TAR) duomenis is atviru duomenu saugyklos.

Du klausimai, i kuriuos projektu srautas neatsako:
  1. kas naujai uzregistruota per pastaruosius X dienu;
  2. kas jau priimta, bet dar neisigaliojo.

Antrasis yra pasiruosimo langas: aktas galutinis, tekstas zinomas,
bet dar neveikia.

Saltinis: Lietuvos Respublikos Seimo kanceliarija, rinkinys od000139,
per https://get.data.gov.lt (Spinta API).
"""

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

from filters import excluded, fold, in_scope, load_list

BASE = "https://get.data.gov.lt/datasets/gov/lrsk/teises_aktai/Dokumentas"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "data", "tar.json")

# Kiek dienu atgal ziurime naujai uzregistruotus aktus.
RECENT_DAYS = int(os.environ.get("TAR_RECENT_DAYS", "3"))
# Serveris grazina puslapiais; jei pasiekiamas sis skaicius, langas per platus.
PAGE_LIMIT = int(os.environ.get("TAR_LIMIT", "100"))

_FORMAT = None

# tekstas_lt vidutiniskai 7000 simboliu, todel i saraso uzklausas jo neimame.
LIST_FIELDS = (
    "_id,pavadinimas,rusis,dok_grupe,parengusi_inst,priemusi_inst,"
    "tar_kodas,atv_dok_nr,nuoroda,registracija,priimtas,paskelbta_tar,"
    "isigalioja,negalioja,galioj_busena,ar_nacionalinis,ar_verslo_reg"
)

HEADERS = {
    "User-Agent": "tais-monitor (github actions)",
    "Accept": "application/json",
}


def _get(url: str):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def fetch(query: str):
    """Spinta formato prierasa priima ne visais budais, todel bandome kelis.

    Pirmas suveikes budas isimenamas, kad likusios uzklausos jo nebeieskotu.
    """
    global _FORMAT
    candidates = [_FORMAT] if _FORMAT else ["/:format/json?", "?format(json)&", "?"]
    last = None
    for mode in candidates:
        url = BASE + mode + query
        try:
            body = _get(url)
        except Exception as exc:  # noqa: BLE001
            last = f"{exc}  ties  {url}"
            continue
        _FORMAT = mode
        rows = body.get("_data", body if isinstance(body, list) else [])
        return rows, len(rows) >= PAGE_LIMIT
    raise RuntimeError(last or "nepavyko nuskaityti")


def q(*parts) -> str:
    # Kableliai select() sarase ir dvitaskiai privalo likti neuzkoduoti:
    # %2C Spinta grazina HTTP 500.
    safe = "()<>=\"',-_.:/*"
    return "&".join(urllib.parse.quote(p, safe=safe) for p in parts)


def scope_of(row, municipalities) -> bool:
    return in_scope(
        [str(row.get(k) or "") for k in ("parengusi_inst", "priemusi_inst", "dok_grupe", "pavadinimas")],
        municipalities,
    )


def is_national(row) -> bool:
    v = row.get("ar_nacionalinis")
    return v is True or str(v).lower() in ("true", "1")


def title_hits(row, keywords, excludes=()):
    if excluded(str(row.get("pavadinimas") or ""), excludes):
        return []
    hay = fold(" ".join(str(row.get(k) or "") for k in
                        ("pavadinimas", "rusis", "parengusi_inst", "priemusi_inst")))
    return [k for k in keywords if fold(k) in hay]


def fetch_text(doc_id: str) -> str:
    """Vieno akto pilnas tekstas. Naudojamas tik neisigaliojusiems."""
    url = BASE + "/" + doc_id + (_FORMAT or "/:format/json?") + q("select(tekstas_lt)")
    try:
        body = _get(url)
    except Exception:  # noqa: BLE001
        return ""
    if isinstance(body, dict) and "tekstas_lt" in body:
        return body.get("tekstas_lt") or ""
    rows = body.get("_data", []) if isinstance(body, dict) else []
    return (rows[0].get("tekstas_lt") or "") if rows else ""


def body_hits(text, keywords, existing):
    """Iesko raktazodziu akto tekste ir grazina trumpa istrauka."""
    if not text:
        return [], ""
    ft = fold(text)
    found, excerpt = [], ""
    for k in keywords:
        fk = fold(k)
        i = ft.find(fk)
        if i == -1:
            continue
        found.append(k)
        if not excerpt and k not in existing:
            a, b = max(0, i - 130), min(len(text), i + len(fk) + 170)
            excerpt = ("…" if a else "") + " ".join(text[a:b].split()) + ("…" if b < len(text) else "")
    return found, excerpt


def clean(row):
    return {k: (v[:10] if k in DATE_FIELDS and isinstance(v, str) else v)
            for k, v in row.items() if v not in (None, "")}


DATE_FIELDS = {"registracija", "priimtas", "paskelbta_tar", "isigalioja", "negalioja"}


def main():
    keywords = load_list("keywords.txt")
    text_keywords = load_list("keywords-text.txt")
    excludes = load_list("exclude.txt")
    municipalities = load_list("savivaldybes.txt")
    today = date.today()
    since = (today - timedelta(days=RECENT_DAYS)).isoformat()
    tomorrow = (today + timedelta(days=1)).isoformat()
    notes = []

    # 1. Naujai uzregistruoti. Filtras butinas: be jo mazejantis rikiavimas
    #    isstumia i prieki irasus su tuscia registracijos data.
    recent, cut = fetch(q(
        f'registracija>="{since}"',
        "sort(-registracija)",
        f"select({LIST_FIELDS})",
        f"limit({PAGE_LIMIT})",
    ))
    if cut:
        notes.append(f"Naujai registruotu sarasas nukirstas ties {PAGE_LIMIT}; sumazinkite TAR_RECENT_DAYS.")

    # 2. Priimti, bet dar neisigalioje.
    upcoming, cut = fetch(q(
        f'isigalioja>="{tomorrow}"',
        "sort(isigalioja)",
        f"select({LIST_FIELDS})",
        f"limit({PAGE_LIMIT})",
    ))
    if cut:
        notes.append(f"Neisigaliojusiu sarasas nukirstas ties {PAGE_LIMIT}.")

    for row in recent:
        row["hits"] = title_hits(row, keywords, excludes)
        row["national"] = is_national(row)
        row["scope"] = scope_of(row, municipalities)

    # Neisigaliojusiu grupeje ieskome ir akto tekste: daug aktualiu normu
    # slepiasi aktuose, kuriu pavadinime raktazodziu nera.
    #
    # Bet tik nacionaliniuose aktuose ir tik su siauresniu zodynu. Savivaldybiu
    # isakymai "del draudimo rukyti daugiabucio namo..." kitaip uzgozia viska:
    # bandymas su placiais raktazodziais dave 38 atitikmenis is 50.
    for row in upcoming:
        skip = excluded(str(row.get("pavadinimas") or ""), excludes)
        hits = [] if skip else title_hits(row, keywords)
        row["national"] = is_national(row)
        row["scope"] = scope_of(row, municipalities)
        excerpt = ""
        # Teksto paieska tik stebimiems: nacionaliniams ir sarase esancioms
        # savivaldybems. Tarifu sprendimai yra savivaldos lygmens, todel
        # riboti vien nacionaliniais butu klaida.
        if text_keywords and row["scope"] and not skip:
            deeper, excerpt = body_hits(fetch_text(row.get("_id", "")), text_keywords, hits)
            hits = sorted(set(hits) | set(deeper))
        row["hits"] = hits
        if excerpt:
            row["excerpt"] = excerpt

    payload = {
        "source": "www.lrs.lt / Seimo kanceliarija, rinkinys od000139, CC BY 4.0",
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "recentSince": since,
        "notes": notes,
        "recent": [clean(r) for r in recent],
        "upcoming": [clean(r) for r in upcoming],
    }

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))

    inscope = [r for r in recent + upcoming if r.get("scope") and r.get("hits")]
    print(f"TAR: {len(recent)} naujai registruotu (nuo {since}), "
          f"{len(upcoming)} dar neisigaliojusiu, "
          f"{len(inscope)} atitinka raktazodzius stebimame rate")
    for n in notes:
        print("  ! " + n)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        # TAR sutrikimas neturi sugriauti projektu stebesenos.
        print("TAR klaida (praleidziama):", exc, file=sys.stderr)
        print("  Uzklausos pavyzdys naudotas:", BASE, file=sys.stderr)
        sys.exit(0)
