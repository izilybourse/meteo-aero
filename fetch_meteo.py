#!/usr/bin/env python3
"""
fetch_meteo.py — récupère METAR + TAF (NOAA Aviation Weather, repli NOAA tgftp)
et écrit data/meteo.json, lu ensuite par le tableau de bord.
Aucune clé API, aucune dépendance (bibliothèque standard uniquement).
"""
import json
import os
import re
import math
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# Aérodromes suivis + stations de repli (codes OACI)
STATIONS = ["FMEE", "FMEP", "FMCZ", "FMCH"]
# Aérodromes pour lesquels on publie les NOTAM en vigueur
NOTAM_STATIONS = ["FMEE", "FMEP", "FMCZ"]

# Observations Météo-France 6 min (API DPObs). La clé est lue dans le secret GitHub MF_APIKEY.
MF_BASE = "https://public-api.meteofrance.fr/public/DPObs/"
# Station Météo-France (8 chiffres) associée à chaque aérodrome — à vérifier : la distance est publiée dans meteo.json
MF_STATIONS = {"FMEE": "97418110", "FMEP": "97416463", "FMCZ": "98508001"}
AIRFIELDS = {"FMEE": (-20.887, 55.510), "FMEP": (-21.321, 55.425), "FMCZ": (-12.805, 45.281)}
_mf_path = [None]

AWC = "https://aviationweather.gov/api/data/"
TGFTP = "https://tgftp.nws.noaa.gov/data/"
UA = {"User-Agent": "izilyfire-meteo/1.0 (+github actions)"}


def get(url, timeout=25):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def awc_json(kind, ids):
    url = f"{AWC}{kind}?ids={','.join(ids)}&format=json" + ("&hours=3" if kind == "metar" else "")
    txt = get(url).strip()
    return json.loads(txt) if txt else []


def metar_time(raw):
    """Horodatage (epoch) déduit du groupe jjhhmmZ d'un METAR brut."""
    m = re.search(r"\b(\d{2})(\d{2})(\d{2})Z\b", raw)
    if not m:
        return None
    now = datetime.now(timezone.utc)
    try:
        d = now.replace(day=int(m[1]), hour=int(m[2]), minute=int(m[3]), second=0, microsecond=0)
    except ValueError:
        return None
    if d > now:  # METAR du mois précédent
        month, year = (now.month - 1, now.year) if now.month > 1 else (12, now.year - 1)
        try:
            d = d.replace(year=year, month=month)
        except ValueError:
            return None
    return int(d.timestamp())


FAA_NOTAM = "https://notams.aim.faa.gov/notamSearch/search"
AUTOROUTER = "https://api.autorouter.aero/v1.0/"


def _ts(v):
    """epoch (s ou ms), ISO 8601 ou 'YYMMDDHHMM' -> epoch en secondes ; None si absent / PERM."""
    if v in (None, "", "PERM"):
        return None
    if isinstance(v, (int, float)):
        v = float(v)
        return int(v / 1000) if v > 1e11 else int(v)
    v = str(v).strip()
    if re.fullmatch(r"\d{10}", v):
        n = int(v)
        return n if 1.0e9 <= n < 2.4e9 else _notam_epoch(v)   # epoch (s) ou YYMMDDHHMM
    if re.fullmatch(r"\d{12,13}", v):
        return int(int(v) / 1000)
    try:
        d = datetime.fromisoformat(v.replace("Z", "+00:00"))
        return int((d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp())
    except ValueError:
        return None


def parse_autorouter(rows, now=None):
    """Lignes JSON autorouter -> liste de NOTAM en vigueur. Parseur tolérant : le schéma exact n'est pas documenté."""
    now = now or time.time()
    out, seen = [], set()
    for r in rows:
        if not isinstance(r, dict):
            continue
        text = next((str(r[k]).strip() for k in ("message", "text", "notam", "raw", "icaoMessage", "iteme", "e", "body")
                     if r.get(k)), "")
        if not text:                       # rien d'exploitable : on garde la ligne brute plutôt que de la perdre
            text = json.dumps(r, ensure_ascii=False)[:600]
        mid = re.search(r"\(?([A-Z]\d{4}/\d{2})", text)
        if mid:
            nid = mid.group(1)
        elif r.get("number") is not None:
            nid = f"{r.get('series', '')}{r.get('number')}/{r.get('year', '')}"
        else:
            nid = str(r.get("id") or "?")
        if nid in seen:
            continue
        seen.add(nid)
        start = next((t for t in (_ts(r.get(k)) for k in ("startvalidity", "startValidity", "start", "from", "validfrom", "itemb")) if t), None)
        end = next((t for t in (_ts(r.get(k)) for k in ("endvalidity", "endValidity", "end", "to", "validto", "itemc")) if t), None)
        mb = re.search(r"\bB\)\s*(\d{10})", text)
        mc = re.search(r"\bC\)\s*(\d{10}|PERM)", text)
        start = start or (_notam_epoch(mb.group(1)) if mb else None)
        perm = bool(mc and mc.group(1) == "PERM")
        end = end or (_notam_epoch(mc.group(1)) if mc and not perm else None)
        if start and start > now:
            continue
        if end and end < now:
            continue
        out.append({"id": nid, "from": start, "to": end, "perm": perm, "text": text})
    out.sort(key=lambda x: x["from"] or 0, reverse=True)
    return out[:40]


def autorouter_token(email, password):
    body = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": email,
                                   "client_secret": password}).encode()
    req = urllib.request.Request(AUTOROUTER + "oauth2/token", data=body, headers={
        **UA, "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8", "replace"))["access_token"]


def autorouter_notams(token, icao):
    """GET /notam?itemas=["ICAO"]&offset=..&limit=.. (paginé), puis filtrage des NOTAM en vigueur."""
    rows, offset, limit = [], 0, 100
    for _ in range(5):
        url = f"{AUTOROUTER}notam?itemas={urllib.parse.quote(json.dumps([icao]))}&offset={offset}&limit={limit}"
        req = urllib.request.Request(url, headers={**UA, "Authorization": "Bearer " + token, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            j = json.loads(r.read().decode("utf-8", "replace"))
        page = j if isinstance(j, list) else (j.get("rows") or j.get("notams") or j.get("items") or j.get("data") or [])
        rows += page
        total = j.get("total") if isinstance(j, dict) else None
        offset += limit
        if len(page) < limit or (isinstance(total, int) and offset >= total):
            break
    return parse_autorouter(rows)


def _notam_epoch(g):
    """'YYMMDDHHMM' (UTC) -> epoch ; None si absent."""
    try:
        return int(datetime(2000 + int(g[0:2]), int(g[2:4]), int(g[4:6]), int(g[6:8]), int(g[8:10]),
                            tzinfo=timezone.utc).timestamp())
    except (ValueError, TypeError):
        return None


def parse_faa(j, now=None):
    """Transforme la réponse FAA en liste de NOTAM actuellement en vigueur."""
    now = now or time.time()
    out, seen = [], set()
    for n in (j.get("notamList") or []):
        text = (n.get("icaoMessage") or n.get("traditionalMessage") or "").strip()
        if not text:
            continue
        mid = re.search(r"\(?([A-Z]\d{4}/\d{2})", text)
        nid = mid.group(1) if mid else str(n.get("notamNumber") or "?")
        if nid in seen:
            continue
        seen.add(nid)
        mb = re.search(r"\bB\)\s*(\d{10})", text)
        mc = re.search(r"\bC\)\s*(\d{10}|PERM)", text)
        start = _notam_epoch(mb.group(1)) if mb else None
        perm = bool(mc and mc.group(1) == "PERM")
        end = _notam_epoch(mc.group(1)) if mc and not perm else None
        if start and start > now:      # pas encore en vigueur
            continue
        if end and end < now:          # expiré
            continue
        out.append({"id": nid, "from": start, "to": end, "perm": perm, "text": text})
    out.sort(key=lambda x: x["from"] or 0, reverse=True)
    return out[:40]


def faa_notams(icao):
    body = urllib.parse.urlencode({
        "searchType": "0", "designatorsForLocation": icao, "designatorForAccountable": "",
        "latDegrees": "", "latMinutes": "0", "latSeconds": "0", "longDegrees": "", "longMinutes": "0",
        "longSeconds": "0", "radius": "10", "sortColumns": "5 false", "sortDirection": "true",
        "designatorForNotamNumberSearch": "", "notamNumber": "", "radiusSearchOnDesignator": "false",
        "radiusSearchDegrees": "", "flightPathText": "", "flightPathDivisor": "", "flightPathBuffer": "4",
        "flightPathIncludeNavaids": "true", "flightPathIncludeArtcc": "false", "flightPathIncludeTfr": "true",
        "flightPathIncludeRegulatory": "false", "flightPathResultsType": "All NOTAMs", "archiveDate": "",
        "archiveDesignator": "", "offset": "0", "notamsOnly": "false", "filters": "", "formatType": "ICAO",
    }).encode()
    req = urllib.request.Request(FAA_NOTAM, data=body, headers={
        **UA, "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return parse_faa(json.loads(r.read().decode("utf-8", "replace")))


def _num(v):
    try:
        f = float(v)
        return None if f != f else f
    except (TypeError, ValueError):
        return None


def _celsius(v):
    f = _num(v)
    return None if f is None else round(f - 273.15 if f > 100 else f, 2)      # K -> °C


def _hpa(v):
    f = _num(v)
    return None if f is None else round(f / 100 if f > 2000 else f, 1)        # Pa -> hPa


def _km(la1, lo1, la2, lo2):
    r = math.pi / 180
    a = math.sin((la2 - la1) * r / 2) ** 2 + math.cos(la1 * r) * math.cos(la2 * r) * math.sin((lo2 - lo1) * r / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(a))


def parse_mf_obs(j, icao, now=None):
    """Réponse DPObs (GeoJSON ou lignes plates) -> série compacte [t, T°C, HR%, pmer hPa, pstation hPa, pluie 6 min mm]."""
    now = now or time.time()
    if isinstance(j, dict) and j.get("features") is not None:
        items = [(f.get("properties") or {}, (f.get("geometry") or {}).get("coordinates")) for f in j["features"]]
    else:
        rows = j if isinstance(j, list) else ((j.get("rows") or j.get("data") or j.get("observations") or []) if isinstance(j, dict) else [])
        items = [(r, [r.get("lon"), r.get("lat")]) for r in rows if isinstance(r, dict)]
    series, seen, coords = [], set(), None
    for pr, c in items:
        t = _ts(pr.get("validity_time") or pr.get("reference_time") or pr.get("date"))
        if not t or t in seen or t < now - 25 * 3600:
            continue
        seen.add(t)
        if coords is None and c and len(c) >= 2 and c[0] is not None and c[1] is not None:
            coords = c
        u, rr = _num(pr.get("u")), _num(pr.get("rr_per"))
        series.append([t, _celsius(pr.get("t")), None if u is None else int(round(u)),
                       _hpa(pr.get("pmer")), _hpa(pr.get("pres")), None if rr is None else round(rr, 2)])
    series.sort(key=lambda r: r[0])
    dist = None
    if coords and icao in AIRFIELDS:
        dist = round(_km(AIRFIELDS[icao][0], AIRFIELDS[icao][1], float(coords[1]), float(coords[0])), 1)
    return {"series": series, "dist": dist}


def mf_get(path, key):
    """GET authentifié : en-tête « apikey », puis « Authorization: Bearer » si refusé."""
    last = None
    for hdr in ({"apikey": key}, {"Authorization": "Bearer " + key}):
        req = urllib.request.Request(MF_BASE + path, headers={**UA, **hdr, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            last = e
            if e.code not in (401, 403):
                raise
    raise last


def mf_obs(key, icao, sid):
    """Interroge DPObs (v2 puis chemin sans version, formats json puis geojson) ; en cas d'échec, l'erreur contient un extrait de la réponse."""
    bases = [_mf_path[0]] if _mf_path[0] else ["v2/station/infrahoraire-6m", "station/infrahoraire-6m", "v1/station/infrahoraire-6m"]
    notes = []
    for base in bases:
        for fmt in ("json", "geojson"):
            try:
                j = mf_get(f"{base}?id_station={sid}&format={fmt}", key)
            except urllib.error.HTTPError as e:
                notes.append(f"{base}/{fmt}: HTTP {e.code}")
                if e.code in (401, 403, 404, 400):
                    continue
                raise
            res = parse_mf_obs(j, icao)
            if res["series"]:
                _mf_path[0] = base
                res["id"] = sid
                return res
            try:
                snip = json.dumps(j, ensure_ascii=False)[:220]
            except Exception:
                snip = str(j)[:220]
            notes.append(f"{base}/{fmt}: réponse sans observation exploitable {snip}")
    raise ValueError(" | ".join(notes)[:900])


def main():
    out = {"updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "stations": {}, "errors": []}
    metars, tafs = {}, {}

    # 1) API JSON NOAA (toutes les stations en une requête)
    try:
        for m in awc_json("metar", STATIONS):
            cur = metars.get(m["icaoId"])
            if not cur or m.get("obsTime", 0) > cur.get("obsTime", 0):
                metars[m["icaoId"]] = m
    except Exception as e:
        out["errors"].append(f"awc metar: {e}")
    try:
        for t in awc_json("taf", STATIONS):
            tafs.setdefault(t["icaoId"], t)
    except Exception as e:
        out["errors"].append(f"awc taf: {e}")

    # 2) Repli : fichiers texte NOAA, station par station
    for st in STATIONS:
        if st not in metars:
            try:
                lines = [l.strip() for l in get(f"{TGFTP}observations/metar/stations/{st}.TXT").splitlines() if l.strip()]
                raw = re.sub(r"^(METAR|SPECI)\s+", "", lines[-1])
                if raw.startswith(st):
                    metars[st] = {"icaoId": st, "rawOb": raw, "obsTime": metar_time(raw), "raw_only": True}
            except Exception as e:
                out["errors"].append(f"tgftp metar {st}: {e}")
        if st not in tafs:
            try:
                lines = [l.strip() for l in get(f"{TGFTP}forecasts/taf/stations/{st}.TXT").splitlines() if l.strip()]
                raw = " ".join(lines[1:])
                if st in raw:
                    tafs[st] = {"icaoId": st, "rawTAF": raw}
            except Exception as e:
                out["errors"].append(f"tgftp taf {st}: {e}")

    for st in STATIONS:
        out["stations"][st] = {"metar": metars.get(st), "taf": tafs.get(st)}

    # NOTAM en vigueur : autorouter en priorité (identifiants dans les secrets GitHub), repli FAA
    out["notamUpdated"] = out["updated"]
    out["notams"], out["notamSrc"] = {}, {}
    token = None
    em, pw = os.environ.get("AUTOROUTER_EMAIL"), os.environ.get("AUTOROUTER_PASSWORD")
    if em and pw:
        try:
            token = autorouter_token(em, pw)
        except Exception as e:
            out["errors"].append(f"autorouter jeton: {e}")
    else:
        out["errors"].append("autorouter: secrets AUTOROUTER_EMAIL / AUTOROUTER_PASSWORD absents")
    for st in NOTAM_STATIONS:
        res, src = None, None
        if token:
            try:
                res, src = autorouter_notams(token, st), "autorouter"
            except Exception as e:
                out["errors"].append(f"autorouter {st}: {e}")
        if res is None:
            try:
                res, src = faa_notams(st), "FAA"
            except Exception as e:
                out["errors"].append(f"faa {st}: {e}")
        out["notams"][st] = res            # None = source indisponible (différent de « aucun NOTAM »)
        out["notamSrc"][st] = src

    # Observations Météo-France 6 min : température, humidité, pression, pluie (24 h glissantes)
    out["obsUpdated"] = out["updated"]
    out["obs"], out["obsErr"] = {}, {}
    mf_key = os.environ.get("MF_APIKEY")
    for icao, sid in MF_STATIONS.items():
        if not mf_key:
            out["obsErr"][icao] = "secret MF_APIKEY absent"
            continue
        try:
            res = mf_obs(mf_key, icao, sid)
            out["obs"][icao] = res
            if res["dist"] is not None and res["dist"] > 20:
                out["errors"].append(f"mf {icao}: station {sid} à {res['dist']} km de l'aérodrome, vérifier l'identifiant")
        except Exception as e:
            out["obsErr"][icao] = str(e)[:900]
            out["errors"].append(f"mf {icao}: {str(e)[:900]}")

    if not metars:
        print("Aucun METAR récupéré — fichier non publié.", out["errors"], file=sys.stderr)
        sys.exit(1)

    os.makedirs("data", exist_ok=True)
    with open("data/meteo.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print(f"OK : {len(metars)} METAR, {len(tafs)} TAF, {len(out['errors'])} avertissement(s)")


if __name__ == "__main__":
    main()
