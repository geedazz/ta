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

# Kiek dienu atgal ziurime naujai paskelbtus aktus.
#
# Atviru duomenu rinkinys veluoja kelias dienas: 2026-09-01 naujausi irasai
# buvo 2026-08-28. Todel langas turi buti platesnis uz veluojima, kitaip
# uzklausa grazina "No data". Kartojimosi nera - suvestine lygina pagal _id.
RECENT_DAYS = int(os.environ.get("TAR_RECENT_DAYS", "14"))
# Serveris grazina puslapiais; jei pasiekiamas sis skaicius, langas per platus.
PAGE_LIMIT = int(os.environ.get("TAR_LIMIT", "100"))

_FORMAT = None

# tekstas_lt vidutiniskai 7000 simboliu, todel i saraso uzklausas jo neimame.
#
# Sarasas trumpas sazingai: kiekvienas laukas atskirai veikia, bet visi
# sesiolika kartu su filtru grazina HTTP 500. Palikti tik tie, kurie
# tikrai naudojami suvestineje.
LIST_FIELDS = (
    "_id,pavadinimas,rusis,priemusi_inst,atv_dok_nr,nuoroda,"
    "paskelbta_tar,isigalioja,galioj_busena,ar_nacionalinis"
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

    # Kiekvienas pjuvis atskirai. Vienas nulutes, kitas privalo isliktis.
    #
    # Neisigalioje atrenka kelias desimtis irasu ir veikia patikimai.
    # Naujai paskelbti gali apimti tukstancius, ir serveris tada grazina 500,
    # todel bandome siaurinti langa, o nepavykus - praleidziame.
    upcoming, cut = [], False
    try:
        upcoming, cut = fetch(q(
            f'isigalioja>="{tomorrow}"',
            "sort(isigalioja)",
            f"select({LIST_FIELDS})",
            f"limit({PAGE_LIMIT})",
        ))
        if cut:
            notes.append(f"Neisigaliojusiu sarasas nukirstas ties {PAGE_LIMIT}.")
    except Exception as exc:  # noqa: BLE001
        notes.append(f"Neisigaliojusiu pjuvio nepavyko nuskaityti: {exc}")
        print("  ! neisigalioje:", exc, file=sys.stderr)

    # Naudojame paskelbta_tar, o ne registracija: pastaroji senesniuose
    # irasuose tuscia, o paskelbimas TAR yra oficialaus paskelbimo momentas.
    recent = []
    for days in (RECENT_DAYS, 7, 3):
        since = (today - timedelta(days=days)).isoformat()
        try:
            recent, cut = fetch(q(
                f'paskelbta_tar>="{since}"',
                "sort(-paskelbta_tar)",
                f"select({LIST_FIELDS})",
                f"limit({PAGE_LIMIT})",
            ))
        except Exception as exc:  # noqa: BLE001
            print(f"  ! {days} d. langas nepavyko: {exc}", file=sys.stderr)
            continue
        if cut:
            notes.append(f"Naujai paskelbtu sarasas nukirstas ties {PAGE_LIMIT} ({days} d. langas).")
        break
    else:
        notes.append("Naujai paskelbtu pjuvio nuskaityti nepavyko; rodomi tik neisigalioje.")

    if not recent and not notes:
        notes.append(f"Nuo {since} naujai paskelbtu nerasta - rinkinys gali veluoti labiau.")

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
        "recentField": "paskelbta_tar",
        "notes": notes,
        "recent": [clean(r) for r in recent],
        "upcoming": [clean(r) for r in upcoming],
    }

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))

    inscope = [r for r in recent + upcoming if r.get("scope") and r.get("hits")]
    newest = max((r.get("paskelbta_tar") or "")[:10] for r in recent) if recent else "-"
    print(f"TAR: {len(recent)} naujai paskelbtu (nuo {since}, naujausias {newest}), "
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
