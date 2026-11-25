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

# Observations Météo-France 6 min (API DPObs). La clé est lue dans le secret GitHub MF_APIKEY.
MF_BASE = "https://public-api.meteofrance.fr/public/DPObs/"
# Station Météo-France (8 chiffres) associée à chaque aérodrome — à vérifier : la distance est publiée dans meteo.json
MF_STATIONS = {"FMEE": "97418110", "FMEP": "97416463", "FMCZ": "98508001"}
AIRFIELDS = {"FMEE": (-20.887, 55.510), "FMEP": (-21.321, 55.425), "FMCZ": (-12.805, 45.281)}
_mf_path = [None]

AWC = "https://aviationweather.gov/api/data/"
TGFTP = "https://tgftp.nws.noaa.gov/data/"
UA = {"User-Agent": "izilyfire-meteo/1.0 (+github actions)"}


def get(url, timeout=20, tries=3):
    """GET avec nouveaux essais (429, 5xx, délai dépassé, coupure réseau) et pauses croissantes."""
    last = None
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            last = e
            if e.code not in (429, 500, 502, 503, 504):
                raise
        except (urllib.error.URLError, OSError) as e:      # inclut TimeoutError / ConnectionError
            last = e
        if k < tries - 1:
            time.sleep(2 * (k + 1))
    raise last


def awc_json(kind, ids, hours=3):
    url = f"{AWC}{kind}?ids={','.join(ids)}&format=json" + (f"&hours={hours}" if kind == "metar" else "")
    for k in range(2):
        txt = get(url).strip()
        if not txt:
            return []
        try:
            return json.loads(txt)
        except ValueError:                 # page d'erreur HTML renvoyée avec un code 200
            if k == 1:
                raise
            time.sleep(3)


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


def _notam_epoch(g):
    """'YYMMDDHHMM' (UTC) -> epoch ; None si absent."""
    try:
        return int(datetime(2000 + int(g[0:2]), int(g[2:4]), int(g[4:6]), int(g[6:8]), int(g[8:10]),
                            tzinfo=timezone.utc).timestamp())
    except (ValueError, TypeError):
        return None


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


def load_prev():
    """Dernier meteo.json publié : fichier récupéré par le workflow, sinon lecture publique de la branche data.
    Sert à conserver l'historique des observations et à reprendre les dernières données valides si une source échoue."""
    try:
        with open("data/meteo.json", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        pass
    repo = os.environ.get("GITHUB_REPOSITORY")
    if repo:
        try:
            req = urllib.request.Request(f"https://raw.githubusercontent.com/{repo}/data/meteo.json?t={int(time.time())}", headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8")) or {}
        except Exception:
            pass
    return {}


def load_prev_obs():
    return load_prev().get("obs") or {}


def merge_series(prev, new, now=None):
    now = now or time.time()
    rows = {int(r[0]): r for r in (prev or []) if r and r[0] >= now - 25 * 3600}
    for r in new:
        rows[int(r[0])] = r
    return [rows[k] for k in sorted(rows)]


OM_VARS = ("current=temperature_2m,relative_humidity_2m,wind_speed_10m,wind_direction_10m,wind_gusts_10m,pressure_msl"
           "&hourly=pressure_msl,temperature_2m,relative_humidity_2m,dew_point_2m,wind_speed_10m,wind_gusts_10m,visibility,precipitation"
           "&minutely_15=precipitation&past_hours=24&forecast_hours=1&past_minutely_15=8&forecast_minutely_15=4"
           "&wind_speed_unit=kn&timezone=UTC")


def fetch_model():
    """Modèle Open-Meteo pour les 3 aérodromes en UNE requête (évite les limites côté visiteurs). {icao: réponse}."""
    ids = list(AIRFIELDS)
    url = ("https://api.open-meteo.com/v1/forecast?latitude=" + ",".join(str(AIRFIELDS[i][0]) for i in ids)
           + "&longitude=" + ",".join(str(AIRFIELDS[i][1]) for i in ids) + "&" + OM_VARS)
    j = json.loads(get(url, timeout=25, tries=3))
    arr = j if isinstance(j, list) else [j]
    if len(arr) != len(ids) or not all(isinstance(x, dict) and not x.get("error") and x.get("current")
                                        and (x.get("hourly") or {}).get("pressure_msl") for x in arr):
        raise ValueError("réponse incomplète : " + (str(j.get("reason")) if isinstance(j, dict) else "format inattendu"))
    return dict(zip(ids, arr))


def _age_min(iso_or_none, now=None):
    try:
        t = datetime.strptime(iso_or_none, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
        return ((now or time.time()) - t) / 60
    except Exception:
        return 1e9


def main():
    now = time.time()
    out = {"updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "stations": {}, "errors": []}
    prev = load_prev()
    prev_st = prev.get("stations") or {}
    metars, tafs = {}, {}

    def warn(msg):
        if msg not in out["errors"]:
            out["errors"].append(msg)

    # 1) API JSON NOAA (toutes les stations en une requête)
    try:
        for m in awc_json("metar", STATIONS):
            cur = metars.get(m["icaoId"])
            if not cur or m.get("obsTime", 0) > cur.get("obsTime", 0):
                metars[m["icaoId"]] = m
    except Exception as e:
        warn(f"awc metar: {e}")
    try:
        for t in awc_json("taf", STATIONS):
            tafs.setdefault(t["icaoId"], t)
    except Exception as e:
        warn(f"awc taf: {e}")

    # 2) Repli : fichiers texte NOAA, station par station
    for st in STATIONS:
        if st not in metars:
            try:
                lines = [l.strip() for l in get(f"{TGFTP}observations/metar/stations/{st}.TXT", tries=2).splitlines() if l.strip()]
                raw = re.sub(r"^(METAR|SPECI)\s+", "", lines[-1])
                if raw.startswith(st):
                    metars[st] = {"icaoId": st, "rawOb": raw, "obsTime": metar_time(raw), "raw_only": True}
            except Exception as e:
                warn(f"tgftp metar {st}: {e}")
        if st not in tafs:
            try:
                lines = [l.strip() for l in get(f"{TGFTP}forecasts/taf/stations/{st}.TXT", tries=2).splitlines() if l.strip()]
                raw = " ".join(lines[1:])
                if st in raw:
                    tafs[st] = {"icaoId": st, "rawTAF": raw}
            except Exception as e:
                warn(f"tgftp taf {st}: {e}")

    # 3) Reprise de la dernière donnée valide si NOAA n'a rien donné (METAR ≤ 6 h, TAF ≤ 12 h) : la page affiche son âge réel
    for st in STATIONS:
        p = prev_st.get(st) or {}
        pm, pt = p.get("metar"), p.get("taf")
        if st not in metars and pm and pm.get("obsTime") and now - pm["obsTime"] < 6 * 3600:
            metars[st] = {**pm, "carried": True}
        if st not in tafs and pt and now - (_ts(pt.get("issueTime")) or _ts(pt.get("bulletinTime")) or now) < 12 * 3600:
            tafs[st] = {**pt, "carried": True}

    # Historique METAR 6 h (affiché en popup par la page) ; repris tel quel si la requête échoue
    hist = {}
    try:
        for m in awc_json("metar", STATIONS, 6):
            if m.get("rawOb") and m.get("obsTime"):
                hist.setdefault(m["icaoId"], []).append({"icaoId": m["icaoId"], "obsTime": m["obsTime"], "rawOb": m["rawOb"]})
    except Exception as e:
        warn(f"awc metar 6h: {e}")
        for st in STATIONS:
            h = [x for x in ((prev_st.get(st) or {}).get("history") or []) if x.get("obsTime", 0) > now - 6 * 3600]
            if h:
                hist[st] = h

    for st in STATIONS:
        out["stations"][st] = {"metar": metars.get(st), "taf": tafs.get(st),
                               "history": sorted(hist.get(st, []), key=lambda x: x["obsTime"])}

    # Observations Météo-France 6 min : température, humidité, pression, pluie (24 h glissantes)
    out["obsUpdated"] = out["updated"]
    out["obs"], out["obsErr"] = {}, {}
    prev_obs = prev.get("obs") or {}
    mf_key = os.environ.get("MF_APIKEY")
    for icao, sid in MF_STATIONS.items():
        if not mf_key:
            out["obsErr"][icao] = "secret MF_APIKEY absent"
            continue
        try:
            res = mf_obs(mf_key, icao, sid)
            res["series"] = merge_series((prev_obs.get(icao) or {}).get("series"), res["series"])
            out["obs"][icao] = res
            if res["dist"] is not None and res["dist"] > 20:
                warn(f"mf {icao}: station {sid} à {res['dist']} km de l'aérodrome, vérifier l'identifiant")
        except Exception as e:
            old = prev_obs.get(icao)
            if old and old.get("series"):                 # on garde l'historique ; avertissement seulement si la dernière mesure est ancienne
                old["series"] = merge_series(old["series"], [])
                out["obs"][icao] = old
                last = old["series"][-1][0] if old["series"] else 0
                if now - last > 30 * 60:
                    warn(f"mf {icao}: dernière mesure il y a {int((now - last) / 60)} min ({str(e)[:200]})")
            else:
                out["obsErr"][icao] = str(e)[:900]
                warn(f"mf {icao}: {str(e)[:900]}")

    # Modèle Open-Meteo relayé (toutes les ~10 min, une requête groupée) ; la page n'appelle plus Open-Meteo directement
    pm_ = prev.get("model") or {}
    if pm_ and _age_min(prev.get("modelUpdated")) < 10 and all(i in pm_ for i in AIRFIELDS):
        out["model"], out["modelUpdated"] = pm_, prev["modelUpdated"]
    else:
        try:
            out["model"], out["modelUpdated"] = fetch_model(), out["updated"]
        except Exception as e:
            if pm_ and _age_min(prev.get("modelUpdated")) < 120:
                out["model"], out["modelUpdated"] = pm_, prev.get("modelUpdated")
                if _age_min(prev.get("modelUpdated")) > 40:
                    warn(f"open-meteo: {str(e)[:200]} (données de il y a {int(_age_min(prev.get('modelUpdated')))} min)")
            else:
                warn(f"open-meteo: {str(e)[:200]}")

    if not metars:
        print("Aucun METAR récupéré — fichier non publié.", out["errors"], file=sys.stderr)
        sys.exit(1)

    # écriture atomique + relecture de contrôle : le fichier publié est toujours un JSON complet
    os.makedirs("data", exist_ok=True)
    tmp = "data/meteo.json.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    with open(tmp, encoding="utf-8") as f:
        json.load(f)
    os.replace(tmp, "data/meteo.json")
    print(f"OK : {len(metars)} METAR, {len(tafs)} TAF, {len(out['errors'])} avertissement(s)")


if __name__ == "__main__":
    main()
