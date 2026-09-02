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
import time
import urllib.parse
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

from filters import excluded, fold, in_scope, load_list

BASE = "https://get.data.gov.lt/datasets/gov/lrsk/teises_aktai/Dokumentas"

# Jei nustatytas, uzklausos eina per tarpini serveri (Cloudflare Worker),
# apeinanti WAF bloka pries GitHub Actions IP. Zr. proxy-worker/README.md.
PROXY = os.environ.get("TAR_PROXY_URL", "").rstrip("/")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "data", "tar.json")

# Du atskiri dalykai, dazniausiai supainiojami:
#
# QUERY_DAYS - kiek atgal KLAUSIAME serverio. Turi buti platus, nes
# rinkinys veluoja kelias dienas (2026-09-01 naujausias irasas buvo
# 2026-08-28 - 4 dienu veluojimas). Per siauras langas grazina "No data".
#
# FRESH_DAYS - kiek atgal RODOME suvestineje. Vartotojui rupi tik
# siandien/vakar priimti aktai, ne visa 10 dienu istorija. Sis filtras
# taikomas JAU gautiems duomenims, klientine puse - serverio uzklausai
# neturi jokios itakos ir nesumazina "No data" rizikos.
QUERY_DAYS = int(os.environ.get("TAR_QUERY_DAYS", "10"))
FRESH_DAYS = int(os.environ.get("TAR_FRESH_DAYS", "1"))  # 1 = siandien + vakar
# Serveris grazina puslapiais; jei pasiekiamas sis skaicius, langas per platus.
PAGE_LIMIT = int(os.environ.get("TAR_LIMIT", "100"))

_FORMAT = None

# tekstas_lt vidutiniskai 7000 simboliu, todel i saraso uzklausas jo neimame.
#
# Sarasas trumpas sazingai: kiekvienas laukas atskirai veikia, bet visi
# sesiolika kartu su filtru grazina HTTP 500. Palikti tik tie, kurie
# tikrai naudojami suvestineje.
# Desimt lauku. Riba nezinoma tiksliai: 10 veikia, 16 grazindavo HTTP 500,
# todel laikomes desimties. paskelbta_tar butinas - juo filtruojama ir
# rikiuojama; rodomos vartotojui priemimo ir isigaliojimo datos.
# galioj_busena isimta - suvestineje niekur nerodoma, o vieta reikalinga
# dokumento numeriui (atv_dok_nr), kuris reikalingas citavimui.
LIST_FIELDS = (
    "_id,pavadinimas,rusis,priemusi_inst,atv_dok_nr,nuoroda,"
    "priimtas,paskelbta_tar,isigalioja,ar_nacionalinis"
)

# Kuo arciau to, ka siuncia narsykle. Ankstesnis variantas su
# "Accept: application/json" ir savo prisistatymu grazindavo HTTP 500.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "lt,en;q=0.8",
}


def _proxied(url: str) -> str:
    """Persuka uzklausa per tarpini serveri, jei jis sukonfiguruotas."""
    upstream_path = url[len("https://get.data.gov.lt"):]
    return PROXY + "/?path=" + urllib.parse.quote(upstream_path, safe="")


def _get(url: str):
    target = _proxied(url) if PROXY else url
    req = urllib.request.Request(target, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = ""
        if "<html" in body[:200].lower():
            # HTML puslapis vietoj JSON klaidos rodo, kad blokuoja apsaugos
            # sistema (WAF) pagal siuncianti IP, o ne pati Spinta API.
            # Turinio pakeisti nepadeda - GitHub Actions serveriu adresai
            # priklauso zinomiems debesijos diapazonams.
            hint = (" (per proxy - vadinasi, ir jis blokuojamas)" if PROXY
                    else " (be proxy - zr. proxy-worker/README.md)")
            raise RuntimeError(
                f"{exc.code} {exc.reason} - WAF grazino HTML vietoj JSON{hint}."
            ) from None
        raise RuntimeError(f"{exc.code} {exc.reason} | serveris atsake: {body[:400]}") from None


def fetch(query: str, attempts: int = 5):
    """Spinta formato prierasa priima ne visais budais, todel bandome kelis.

    Pirmas suveikes budas isimenamas, kad likusios uzklausos jo nebeieskotu.
    Klaidos dazniausiai laikinos (saugykla ispeja apie aktyvu vystyma, ne
    stabilu IP bloka - ta pati uzklausa kartais praeina, kartais ne), todel
    kiekviena uzklausa kartojame kelis kartus su trumpa pauze pries pasiduodant.
    """
    global _FORMAT
    candidates = [_FORMAT] if _FORMAT else ["/:format/json?", "?format(json)&"]
    last = None
    for mode in candidates:
        url = BASE + mode + query
        for attempt in range(1, attempts + 1):
            try:
                body = _get(url)
            except Exception as exc:  # noqa: BLE001
                last = f"{exc}  ties  {url}"
                if attempt < attempts:
                    # 3s, 6s, 12s, 24s - saugykla nestabili, ne blokuojanti
                    # nuolat, todel platesnis langas duoda geresne tikimybe.
                    time.sleep(3 * (2 ** (attempt - 1)))
                continue
            _FORMAT = mode
            rows = body.get("_data", body if isinstance(body, list) else [])
            return rows, len(rows) >= PAGE_LIMIT
    raise RuntimeError(last or "nepavyko nuskaityti")


def q(*parts) -> str:
    """Perkoduoja uzklausa taip pat, kaip tai daro narsykle.

    Du dalykai, kuriuos isaiskino bandymai:
      - kableliai select() sarase privalo likti NEUZKODUOTI (%2C -> HTTP 500);
      - kabutes filtro reiksmeje privalo buti UZKODUOTOS (%22). Narsykles
        adreso juostoje matote kabutes, bet i serverva keliauja %22, ir
        neapdorotos kabutes serverui yra netaisyklingas URL.
    """
    safe = "()<>=',-_.:/*"
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
    since = (today - timedelta(days=QUERY_DAYS)).isoformat()
    tomorrow = (today + timedelta(days=1)).isoformat()
    notes = []

    # Kiekvienas pjuvis bandomas atskirai, patikimiausias pirma.
    #
    # Bandymai parode, kad "priimtas" ir "paskelbta_tar" filtrai patikimai
    # suveikia, o "isigalioja" (ateities data) nuosekliai nepavyksta - net
    # su pakartojimais, net per proxy. Tikriausia priezastis: tas laukas
    # rasto blogiau indeksuotas, nes dauguma uzklausu einta atgal, ne pirmyn.
    #
    # Todel tvarka: pirma paskelbta_tar (patikimas), tada isigalioja
    # (nepatikimas, bet unikalus - tik jis parodo, kas jau priimta bet dar
    # neveikia). Jei isigalioja nepavyksta, suvestine vis tiek turi naujai
    # paskelbtu sarasa, o ne tuscia puslapi.

    recent = []
    for days in (QUERY_DAYS, 14, 5):
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
        notes.append("Naujai paskelbtu pjuvio nuskaityti nepavyko.")

    if not recent and not notes:
        notes.append(f"Nuo {since} naujai paskelbtu nerasta - rinkinys gali veluoti labiau.")

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
        notes.append(
            "Neisigaliojusiu pjuvio siandien nuskaityti nepavyko - saugykla "
            "nestabili. Naujai paskelbtu sarasas veliau."
        )
        print("  ! neisigalioje:", exc, file=sys.stderr)

    # Filtruojame iki tikrai svieziu - serveriui uzklausem platesnio lango
    # del veluojimo, bet rodyti norime tik pastarasias FRESH_DAYS dienas.
    fresh_since = (today - timedelta(days=FRESH_DAYS)).isoformat()
    older = [r for r in recent if (r.get("paskelbta_tar") or "")[:10] < fresh_since]
    recent = [r for r in recent if (r.get("paskelbta_tar") or "")[:10] >= fresh_since]
    if older:
        print(f"  (atmesta {len(older)} senesniu nei {FRESH_DAYS} d. - rodomi tik svieziausi)")

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

    # SVARBU: nesekmingas pjuvis niekada neistrina anksciau surinktu
    # duomenu. Serveris nepastovus - jei siandien "isigalioja" nepavyko, o
    # vakar pavyko, senas rezultatas islieka, kol jo nepakeicia naujesnis
    # sekmingas paleidimas. Be sito kiekvienas nepavykes bandymas tyliai
    # istrindavo viska, ka jau buvome surinke.
    previous = {"recent": [], "upcoming": []}
    if os.path.exists(OUT):
        try:
            with open(OUT, encoding="utf-8") as f:
                previous = json.load(f)
        except Exception:  # noqa: BLE001
            pass

    def merge(new_rows, old_rows, got_new):
        if got_new:
            fresh = {r["_id"]: clean(r) for r in new_rows if r.get("_id")}
            old = {r["_id"]: r for r in old_rows if r.get("_id")}
            old.update(fresh)
            return list(old.values())
        return old_rows  # nesekme - paliekame kas buvo

    merged_recent = merge(recent, previous.get("recent", []), got_new=bool(recent) or "Naujai paskelbtu pjuvio nuskaityti nepavyko." not in notes)
    merged_upcoming = merge(upcoming, previous.get("upcoming", []),
                             got_new=not any("Neisigaliojusiu pjuvio" in n for n in notes))

    stale = []
    if merged_recent and not recent:
        stale.append("naujai paskelbti")
    if merged_upcoming and not upcoming:
        stale.append("neisigalioje")
    if stale:
        notes.append(f"Rodomi ankstesnio sekmingo paleidimo duomenys ({', '.join(stale)}) - siandien nuskaityti nepavyko.")

    payload = {
        "source": "www.lrs.lt / Seimo kanceliarija, rinkinys od000139, CC BY 4.0",
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "recentSince": since,
        "recentField": "paskelbta_tar",
        "notes": notes,
        "recent": merged_recent,
        "upcoming": merged_upcoming,
    }

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))

    inscope = [r for r in merged_recent + merged_upcoming if r.get("scope") and r.get("hits")]
    newest = max((r.get("paskelbta_tar") or "")[:10] for r in merged_recent) if merged_recent else "-"
    if PROXY:
        print(f"TAR: naudojamas proxy {PROXY}")
    print(f"TAR: siandien gauta {len(recent)} naujai paskelbtu, {len(upcoming)} neisigaliojusiu "
          f"| issaugota is viso {len(merged_recent)} + {len(merged_upcoming)} "
          f"(naujausias {newest}), {len(inscope)} atitinka raktazodzius stebimame rate")
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
