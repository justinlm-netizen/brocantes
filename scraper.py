#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Collecteur brocabrac -> data.json  (version 2 : JSON-LD)
========================================================
brocabrac.fr embarque, pour chaque événement, un bloc JSON-LD schema.org/Event
extrêmement complet (nom, date/heure, adresse exacte, coordonnées GPS précises,
statut annulé). La taille est donnée par les "points" (span.dots[title="De 100 à 200"]).

Ce collecteur parcourt brocabrac pour tous les départements et les prochains mois,
puis produit data.json — sans dépendre d'un géocodage externe (les coordonnées
viennent directement du JSON-LD).

Structure HTML exploitée :
  div.block.ev-list
    div.ev-section
      div.section-title[data-date="YYYY-MM-DD"]   <- date de l'occurrence (gère le multi-jours)
      div.ev[data-event-id]
        script[type=application/ld+json]           <- Event complet
        span.dots[title="De 100 à 200"]  •••       <- taille (nb de points + fourchette)
        span.cat[title="Vide-Grenier"]             <- type
        p.info > .zipcode / .cat / .address

Utilisation :
    python scraper.py                      # tous les départements, 4 mois
    python scraper.py --months 6
    python scraper.py --depts 22,35,56,29
    python scraper.py --out data.json

Sécurité : si une exécution ne collecte AUCUN événement, data.json n'est pas écrasé.
"""

import argparse
import json
import re
import sys
import time
import unicodedata
from datetime import date

import requests
from bs4 import BeautifulSoup

BASE = "https://brocabrac.fr"
GEO = "https://geo.api.gouv.fr"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; BrocanteurBot/2.0; agregateur personnel de brocantes)",
    "Accept-Language": "fr-FR,fr;q=0.9",
}
MONTH_SLUGS = ["janvier", "fevrier", "mars", "avril", "mai", "juin",
               "juillet", "aout", "septembre", "octobre", "novembre", "decembre"]

session = requests.Session()
session.headers.update(HEADERS)


# ---------------------------------------------------------------------------
def norm(s):
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def get(url, tries=3):
    for i in range(tries):
        try:
            r = session.get(url, timeout=25)
            if r.status_code == 200:
                return r.text
            if r.status_code == 404:
                return None
        except requests.RequestException as e:
            print(f"  ! réseau {url} : {e}", file=sys.stderr)
        time.sleep(1.2 * (i + 1))
    return None


def month_horizon(n):
    d = date.today()
    out, seen = [], set()
    for i in range(n):
        m = MONTH_SLUGS[(d.month - 1 + i) % 12]
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


# ---------------------------------------------------------------------------
def category(text):
    t = norm(text)
    if "vide maison" in t or "vide garage" in t:
        return "Vide-maison"
    if "braderie" in t:
        return "Braderie"
    if "brocante" in t and "vide grenier" not in t:
        return "Brocante"
    if "bourse" in t or "marche aux livres" in t:
        return "Bourse / marché"
    if "vide grenier" in t or "vide greniers" in t or "puces" in t:
        return "Vide-grenier"
    if "brocante" in t:
        return "Brocante"
    return "Autre"


def size_from(dotcount, exhibitors, cat, blob):
    """Taille S/M/L : d'abord le nombre de points, sinon la fourchette, sinon le type."""
    if dotcount >= 3:
        return "L"
    if dotcount == 2:
        return "M"
    if dotcount == 1:
        return "S"
    if exhibitors:
        nums = [int(x) for x in re.findall(r"\d+", exhibitors)]
        if nums:
            up = max(nums)
            return "S" if up <= 50 else ("M" if up <= 150 else "L")
    if cat == "Vide-maison":
        return "S"
    if cat in ("Braderie", "Bourse / marché"):
        return "S" if any(w in blob for w in ("livres", "vinyles", "vetements", "puericulture")) else "M"
    return "M"


# ---------------------------------------------------------------------------
def parse_page(html, dep_code, dep_name):
    """Extrait tous les événements d'une page (dept + mois)."""
    soup = BeautifulSoup(html, "lxml")
    out = []
    for sec in soup.select("div.ev-section"):
        st = sec.select_one(".section-title[data-date]")
        if not st:
            continue
        day = st.get("data-date")  # YYYY-MM-DD
        for ev in sec.select(".ev[data-event-id]"):
            eid = ev.get("data-event-id")
            s = ev.find("script", attrs={"type": "application/ld+json"})
            if not s:
                continue
            try:
                j = json.loads(s.string or s.get_text())
            except Exception:
                continue
            if isinstance(j, list):
                j = next((x for x in j if x.get("@type") == "Event"), j[0] if j else {})
            if not j:
                continue

            loc = j.get("location") or {}
            geo = loc.get("geo") or {}
            addr = loc.get("address") or {}
            start = j.get("startDate") or ""
            end = j.get("endDate") or ""
            hhmm = start[11:16] if len(start) >= 16 else None
            end_day = end[:10] if len(end) >= 10 else None

            # type depuis span.cat[title]
            cat_span = ev.select_one(".cat[title]")
            typ = (cat_span.get("title").strip() if cat_span and cat_span.get("title")
                   else (ev.select_one("p.info .cat").get_text(strip=True) if ev.select_one("p.info .cat") else ""))

            # taille depuis span.dots
            dots = ev.select_one(".dots")
            exhibitors = dots.get("title").strip() if dots and dots.get("title") else None
            dotcount = dots.get_text().count("•") if dots else 0

            status = (j.get("eventStatus") or "")
            cancelled = "cancel" in status.lower()

            city = addr.get("addressLocality") or ""
            street = addr.get("streetAddress")
            zipc = addr.get("postalCode")
            lat = geo.get("latitude")
            lng = geo.get("longitude")
            try:
                lat = round(float(lat), 6) if lat is not None else None
                lng = round(float(lng), 6) if lng is not None else None
            except (TypeError, ValueError):
                lat = lng = None

            cat = category(typ or j.get("name", ""))
            blob = norm((j.get("name") or "") + " " + (typ or ""))
            out.append({
                "id": eid,
                "date": day,
                "time": hhmm,
                "enddate": end_day if end_day and end_day != day else None,
                "city": city,
                "type": typ or cat,
                "name": j.get("name"),
                "address": street,
                "zip": zipc,
                "exhibitors": exhibitors,
                "cancelled": cancelled,
                "cat": cat,
                "size": size_from(dotcount, exhibitors, cat, blob),
                "lat": lat,
                "lng": lng,
                "dep": dep_code,
                "depname": dep_name,
                "url": j.get("url") or j.get("@id"),
            })
    return out


def scrape_departement(code, nom, months, sleep=0.3):
    results = {}
    for mslug in months:
        seen_month = set()
        page = 1
        while True:
            url = f"{BASE}/{code.lower()}/{mslug}/" + (f"?p={page}" if page > 1 else "")
            html = get(url)
            if not html:
                break
            evs = parse_page(html, code, nom)
            page_keys = {(e["id"], e["date"]) for e in evs}
            new_keys = page_keys - seen_month
            if not new_keys:
                break  # page hors-limite (brocabrac renvoie la page 1) ou plus rien
            seen_month |= page_keys
            for e in evs:
                results[(e["id"], e["date"])] = e
            page += 1
            if page > 15:
                break
            time.sleep(sleep)
    # nettoyer la clé technique 'id'
    for e in results.values():
        e.pop("id", None)
    return list(results.values())


# ---------------------------------------------------------------------------
def load_departements():
    txt = get(f"{GEO}/departements?fields=nom,code&format=json")
    if not txt:
        raise SystemExit("Impossible de récupérer la liste des départements.")
    return json.loads(txt)


def main():
    ap = argparse.ArgumentParser(description="Collecteur brocabrac -> data.json (JSON-LD)")
    ap.add_argument("--months", type=int, default=4)
    ap.add_argument("--depts", type=str, default="")
    ap.add_argument("--out", type=str, default="data.json")
    ap.add_argument("--sleep", type=float, default=0.3)
    args = ap.parse_args()

    months = month_horizon(args.months)
    print(f"Mois : {', '.join(months)}")

    deps = load_departements()
    if args.depts:
        wanted = {d.strip().lower() for d in args.depts.split(",") if d.strip()}
        deps = [d for d in deps if d["code"].lower() in wanted]
    print(f"{len(deps)} département(s).")

    events = []
    for i, dep in enumerate(deps, 1):
        evs = scrape_departement(dep["code"], dep["nom"], months, sleep=args.sleep)
        events.extend(evs)
        geoloc = sum(1 for e in evs if e["lat"] is not None)
        sized = sum(1 for e in evs if e["exhibitors"])
        print(f"[{i}/{len(deps)}] {dep['code']} {dep['nom']} : {len(evs)} évts "
              f"({geoloc} géoloc., {sized} avec taille)")
        time.sleep(args.sleep)

    if not events:
        print("ERREUR : aucun événement. data.json non modifié.", file=sys.stderr)
        sys.exit(1)

    events.sort(key=lambda e: (e["date"], e["dep"], e["city"] or ""))
    payload = {
        "generated": date.today().isoformat(),
        "source": "brocabrac.fr",
        "count": len(events),
        "events": events,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    geoloc = sum(1 for e in events if e["lat"] is not None)
    sized = sum(1 for e in events if e["exhibitors"])
    print(f"OK : {len(events)} événements -> {args.out} "
          f"({geoloc} géolocalisés, {sized} avec taille exacte).")


if __name__ == "__main__":
    main()
