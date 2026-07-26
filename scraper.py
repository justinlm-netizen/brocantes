#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Collecteur brocabrac -> data.json
==================================
Parcourt brocabrac.fr pour une liste de departements francais et les prochains
mois, geocode les communes via l'API officielle geo.api.gouv.fr, et ecrit un
fichier data.json consommable par l'application (index.html).

Concu pour tourner tout seul sur GitHub Actions (voir .github/workflows/update.yml).

Utilisation :
    python scraper.py                      # tous les departements, 4 mois
    python scraper.py --months 3           # horizon de 3 mois
    python scraper.py --depts 22,35,56,29  # sous-ensemble (test)
    python scraper.py --out data.json      # fichier de sortie

Remarques importantes :
- brocabrac n'expose pas de donnees structurees (JSON-LD). On analyse donc le
  HTML. Le reperage repose sur le motif d'URL des fiches evenement
  (/<dept>/<ville>/<id>-<nom>) et sur les en-tetes de date : c'est volontairement
  robuste aux changements de classes CSS. Si brocabrac modifie profondement sa
  structure, adaptez la fonction parse_listing().
- Securite : si une execution ne collecte AUCUN evenement (parsing casse ou site
  injoignable), le script n'ecrase PAS le data.json existant et sort en erreur.
"""

import argparse
import json
import re
import sys
import time
import unicodedata
from datetime import date

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

BASE = "https://brocabrac.fr"
GEO = "https://geo.api.gouv.fr"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; BrocanteurBot/1.0; agregateur personnel de brocantes)",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

MONTH_SLUGS = ["janvier", "fevrier", "mars", "avril", "mai", "juin",
               "juillet", "aout", "septembre", "octobre", "novembre", "decembre"]

# noms de mois tels qu'ils apparaissent dans les en-tetes (accentues ou non)
MONTH_NAMES = {
    "janvier": 1, "fevrier": 2, "février": 2, "mars": 3, "avril": 4, "mai": 5,
    "juin": 6, "juillet": 7, "aout": 8, "août": 8, "septembre": 9,
    "octobre": 10, "novembre": 11, "decembre": 12, "décembre": 12,
}

DATE_RE = re.compile(
    r"(?:lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)?\s*"
    r"(\d{1,2})\s+"
    r"(janvier|f[ée]vrier|mars|avril|mai|juin|juillet|ao[ûu]t|septembre|octobre|novembre|d[ée]cembre)"
    r"\s+(\d{4})",
    re.IGNORECASE,
)

session = requests.Session()
session.headers.update(HEADERS)


# ----------------------------------------------------------------------------
# Utilitaires
# ----------------------------------------------------------------------------
def norm(s):
    """Normalise un nom de commune pour la comparaison (sans accents, minuscules)."""
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def deslug(slug):
    """Transforme un slug d'URL en libelle lisible."""
    return re.sub(r"\s+", " ", slug.replace("-", " ")).strip().capitalize()


def get(url, tries=3):
    for i in range(tries):
        try:
            r = session.get(url, timeout=25)
            if r.status_code == 200:
                return r.text
            if r.status_code == 404:
                return None
        except requests.RequestException as e:
            print(f"  ! erreur reseau {url} : {e}", file=sys.stderr)
        time.sleep(1.2 * (i + 1))
    return None


def month_horizon(n):
    """Renvoie les slugs de mois a parcourir a partir du mois courant."""
    d = date.today()
    out = []
    for i in range(n):
        out.append(MONTH_SLUGS[(d.month - 1 + i) % 12])
    # dedup en gardant l'ordre
    seen = set()
    res = []
    for m in out:
        if m not in seen:
            seen.add(m)
            res.append(m)
    return res


# ----------------------------------------------------------------------------
# Categorie / taille (identique a la logique de l'app)
# ----------------------------------------------------------------------------
def category(text):
    t = text.lower()
    if "vide-maison" in t or "vide maison" in t or "vide garage" in t or "vide-garage" in t:
        return "Vide-maison"
    if "braderie" in t:
        return "Braderie"
    if "brocante" in t:
        return "Brocante"
    if "bourse" in t or "marche aux livres" in t or "marché aux livres" in t:
        return "Bourse / marché"
    if "vide-grenier" in t or "vide grenier" in t or "vide-greniers" in t or "puces" in t or "foire aux puces" in t:
        return "Vide-grenier"
    return "Autre"


BIG_KW = ["foire a la brocante", "foire à la brocante", "foire aux puces", "grand vide grenier",
          "grand vide-grenier", "grande braderie", "braderie d automne", "puces d ete",
          "foire antiquites", "foire aux antiquites", "foire antiquités"]


def size(cat, text):
    blob = norm(text)
    if any(norm(k) in blob for k in BIG_KW):
        return "L"
    if cat == "Vide-maison":
        return "S"
    if cat in ("Braderie", "Bourse / marché"):
        if any(w in blob for w in ("livres", "vinyles", "vetements", "puericulture")):
            return "S"
        return "M"
    return "M"


# ----------------------------------------------------------------------------
# Geocodage par departement (communes -> centre)
# ----------------------------------------------------------------------------
def load_departements():
    """Renvoie [{code, nom}] pour tous les departements."""
    txt = get(f"{GEO}/departements?fields=nom,code&format=json")
    if not txt:
        raise SystemExit("Impossible de recuperer la liste des departements (geo.api.gouv.fr).")
    return json.loads(txt)


def load_communes(dept_code):
    """Renvoie {nom_normalise: (lat, lng)} pour un departement."""
    url = f"{GEO}/communes?codeDepartement={dept_code}&fields=nom,centre&format=json&geometry=centre"
    txt = get(url)
    out = {}
    if not txt:
        return out
    for c in json.loads(txt):
        centre = c.get("centre")
        if centre and centre.get("coordinates"):
            lng, lat = centre["coordinates"][0], centre["coordinates"][1]
            out[norm(c["nom"])] = (round(lat, 5), round(lng, 5), c["nom"])
    return out


# ----------------------------------------------------------------------------
# Analyse d'une page de listing
# ----------------------------------------------------------------------------
def parse_listing(html, dept_code):
    """
    Extrait les evenements d'une page brocabrac (departement + mois).
    Strategie robuste :
      - parcours du document dans l'ordre ;
      - toute chaine de texte qui ressemble a une date => date courante ;
      - tout lien <a> vers une fiche /<dept>/<ville>/<id>-<nom> => evenement,
        rattache a la date courante.
    Renvoie une liste de dict bruts.
    """
    soup = BeautifulSoup(html, "lxml")
    # motif de lien fiche pour CE departement (le code peut etre 22, 2a, 974...)
    link_re = re.compile(rf"/{re.escape(dept_code.lower())}/([^/]+)/(\d+)-([^/?#]+)", re.IGNORECASE)

    current = None  # (year, month, day)
    events = []
    seen_ids = set()

    for node in soup.descendants:
        if isinstance(node, NavigableString):
            txt = str(node).strip()
            if not txt or len(txt) > 60:
                continue
            m = DATE_RE.search(txt)
            if m:
                day = int(m.group(1))
                mon = MONTH_NAMES.get(m.group(2).lower())
                yr = int(m.group(3))
                if mon:
                    current = (yr, mon, day)
        elif isinstance(node, Tag) and node.name == "a" and node.get("href"):
            href = node.get("href")
            lm = link_re.search(href)
            if not lm:
                continue
            ville_slug, eid, name_slug = lm.group(1), lm.group(2), lm.group(3)
            if not current:
                continue  # pas de date connue -> on ignore (evite les liens hors-liste)
            key = (eid, current)
            if key in seen_ids:
                continue
            seen_ids.add(key)
            name = deslug(name_slug)
            # detection "annule" dans le voisinage immediat
            around = ""
            parent = node.parent
            if parent:
                around = norm(parent.get_text(" ", strip=True))
            cancelled = "annule" in around
            events.append({
                "y": current[0], "m": current[1], "d": current[2],
                "ville_slug": ville_slug, "id": eid,
                "name": name, "cancelled": cancelled,
            })
    return events


# ----------------------------------------------------------------------------
# Collecte d'un departement
# ----------------------------------------------------------------------------
def scrape_departement(code, nom, months, communes, sleep=0.3):
    results = {}  # (id, date) -> event
    for mslug in months:
        seen_ids_month = set()
        page = 1
        while True:
            url = f"{BASE}/{code.lower()}/{mslug}/" + (f"?p={page}" if page > 1 else "")
            html = get(url)
            if not html:
                break
            evs = parse_listing(html, code)
            new_ids = {e["id"] for e in evs} - seen_ids_month
            if not new_ids:
                break  # page hors-limite (brocabrac renvoie la page 1) ou plus rien
            seen_ids_month |= {e["id"] for e in evs}
            for e in evs:
                date_str = f"{e['y']:04d}-{e['m']:02d}-{e['d']:02d}"
                key = (e["id"], date_str)
                if key in results:
                    continue
                cat = category(e["name"])
                geo = communes.get(norm(deslug(e["ville_slug"])))
                city = geo[2] if geo else deslug(e["ville_slug"])
                results[key] = {
                    "date": date_str,
                    "city": city,
                    "type": cat,               # type deduit du nom (pas de type explicite fiable)
                    "name": e["name"],
                    "address": None,
                    "cancelled": e["cancelled"],
                    "cat": cat,
                    "size": size(cat, e["name"]),
                    "dep": code,
                    "depname": nom,
                    "lat": geo[0] if geo else None,
                    "lng": geo[1] if geo else None,
                }
            page += 1
            if page > 12:  # garde-fou
                break
            time.sleep(sleep)
    return list(results.values())


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Collecteur brocabrac -> data.json")
    ap.add_argument("--months", type=int, default=4, help="nombre de mois a parcourir (defaut 4)")
    ap.add_argument("--depts", type=str, default="", help="codes de departements separes par des virgules (defaut : tous)")
    ap.add_argument("--out", type=str, default="data.json", help="fichier de sortie")
    ap.add_argument("--sleep", type=float, default=0.3, help="pause entre requetes (s)")
    args = ap.parse_args()

    months = month_horizon(args.months)
    print(f"Mois parcourus : {', '.join(months)}")

    all_deps = load_departements()
    if args.depts:
        wanted = {d.strip().lower() for d in args.depts.split(",") if d.strip()}
        all_deps = [d for d in all_deps if d["code"].lower() in wanted]
    print(f"{len(all_deps)} departement(s) a collecter.")

    events = []
    for i, dep in enumerate(all_deps, 1):
        code, nom = dep["code"], dep["nom"]
        communes = load_communes(code)
        evs = scrape_departement(code, nom, months, communes, sleep=args.sleep)
        events.extend(evs)
        print(f"[{i}/{len(all_deps)}] {code} {nom} : {len(evs)} evenements (communes geocodees: {len(communes)})")
        time.sleep(args.sleep)

    # Securite : ne jamais ecraser par du vide
    if not events:
        print("ERREUR : aucun evenement collecte. Le fichier existant n'est pas modifie.", file=sys.stderr)
        sys.exit(1)

    events.sort(key=lambda e: (e["date"], e["dep"], e["city"]))
    payload = {
        "generated": date.today().isoformat(),
        "source": "brocabrac.fr",
        "count": len(events),
        "events": events,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    geoloc = sum(1 for e in events if e["lat"] is not None)
    print(f"OK : {len(events)} evenements ecrits dans {args.out} ({geoloc} geolocalises).")


if __name__ == "__main__":
    main()
