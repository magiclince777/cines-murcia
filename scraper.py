"""
Scraper de programación de cines de Murcia.

Estrategia:
  1. Scrapea las páginas HTML de neocine.es para obtener:
     título de película, duración, calificación, e ID de cada sesión.
  2. Para cada ID de sesión, llama al GraphQL de entradas.com
     (https://entradas-next-live.kinoheld.de/graphql) para obtener
     la SALA exacta y la fecha/hora oficial.
  3. Scrapea filmotecamurcia.carm.es (sala única, sin GraphQL).
  4. Genera data/schedule.json con todo el contenido.

Uso:
    pip install -r requirements.txt
    python scraper.py > data/schedule.json
"""
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

# ============================================================
# CONFIGURACIÓN
# ============================================================
CINEMAS_NEOCINE = [
    {
        "id": "myrtea",
        "name": "Myrtea Premium",
        "area": "El Tiro, Murcia",
        "color": "#7c3aed",
        "neocine_url": "https://www.neocine.es/cine/5/hd-digital-myrtea--el-tiro---murcia-/lang/es",
    },
    {
        "id": "centrofama",
        "name": "Centrofama",
        "area": "Murcia centro",
        "color": "#0891b2",
        "neocine_url": "https://www.neocine.es/cine/1/centrofama--murcia-/lang/es",
    },
]

GRAPHQL_URL = "https://entradas-next-live.kinoheld.de/graphql"
GRAPHQL_QUERY = """
query GetShow($id: ID!) {
  show(id: $id) {
    id
    beginning
    auditorium { id name }
    cinema { id name }
  }
}
"""

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

DAYS_AHEAD = 7  # cuántos días futuros guardamos (hoy = 0)


def log(msg):
    print(msg, file=sys.stderr)


def detect_rating(img_src):
    if not img_src:
        return "?"
    s = img_src.lower()
    if "para_todos_los_publicos" in s: return "TP"
    if "no_recomendada_menores_7" in s: return "+7"
    if "no_recomendada_menores_12" in s: return "+12"
    if "no_recomendada_menores_16" in s: return "+16"
    if "no_recomendada_menores_18" in s: return "+18"
    return "?"


# ============================================================
# SCRAPER NEOCINE
# ============================================================
def scrape_neocine_cinema(cinema):
    log(f"  Scrapeando {cinema['name']}…")
    r = requests.get(
        cinema["neocine_url"],
        headers={"User-Agent": USER_AGENT, "Accept-Language": "es"},
        timeout=30,
    )
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    shows = []
    seen_ids = set()
    pattern = re.compile(r"cine\.entradas\.com.*?/evento/(\d+)")

    for link in soup.find_all("a", href=pattern):
        href = link.get("href", "")
        time_text = link.get_text(strip=True)
        if not re.match(r"^\d{1,2}:\d{2}$", time_text):
            continue

        m = pattern.search(href)
        if not m:
            continue
        show_id = m.group(1)
        if show_id in seen_ids:
            continue
        seen_ids.add(show_id)

        # Título: h3 más cercano hacia atrás
        h3 = link.find_previous("h3")
        movie_title = "?"
        if h3:
            inner = h3.find("a")
            movie_title = (inner or h3).get_text(strip=True)

        # Duración
        duration = None
        dur_text = link.find_next(string=re.compile(r"Duración"))
        if dur_text:
            m2 = re.search(r"Duración:\s*(\d+)", str(dur_text))
            if m2:
                duration = int(m2.group(1))

        # Calificación
        rating = "?"
        for img in link.find_all_next("img", limit=10):
            if "calificacion" in img.get("src", ""):
                rating = detect_rating(img.get("src", ""))
                break

        shows.append({
            "movie": movie_title,
            "show_id": show_id,
            "duration": duration,
            "rating": rating,
            "neocine_url": href.split("?")[0],
        })

    log(f"    {len(shows)} sesiones únicas encontradas")
    return shows


# ============================================================
# ENRIQUECIMIENTO VIA GRAPHQL — la SALA viene de aquí
# ============================================================
def enrich_with_graphql(show_ids):
    results = {}
    total = len(show_ids)
    for i, sid in enumerate(show_ids):
        if i and i % 20 == 0:
            log(f"    GraphQL: {i}/{total}…")
        try:
            r = requests.post(
                GRAPHQL_URL,
                json={"query": GRAPHQL_QUERY, "variables": {"id": str(sid)}},
                headers={
                    "Content-Type": "application/json",
                    "Accept-Language": "es",
                    "User-Agent": USER_AGENT,
                },
                timeout=15,
            )
            data = r.json()
            show = (data.get("data") or {}).get("show")
            if show:
                aud = show.get("auditorium") or {}
                results[sid] = {
                    "auditorium": aud.get("name"),
                    "beginning": show.get("beginning"),
                }
            time.sleep(0.05)
        except Exception as e:
            log(f"    Fallo en show {sid}: {e}")
    log(f"    GraphQL completo: {len(results)}/{total} respondidos")
    return results


# ============================================================
# FILMOTECA REGIONAL
# ============================================================
def scrape_filmoteca():
    log("  Scrapeando Filmoteca Regional…")
    url = "https://filmotecamurcia.carm.es/servlet/s.Sl?METHOD=ENLACEMENUS&sit=c,884,m,3623,a,0"
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
        r.raise_for_status()
        r.encoding = r.apparent_encoding or "iso-8859-1"
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        log(f"    Error: {e}")
        return []

    sessions = []
    meses = {
        "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
        "julio": 7, "agosto": 8, "septiembre": 9, "octubre": 10,
        "noviembre": 11, "diciembre": 12,
    }
    year = datetime.now().year

    for a in soup.find_all("a"):
        text = a.get_text("\n", strip=True)
        m = re.search(
            r"(\d{1,2})\s+(\w+)\s*(\d{1,2}):(\d{2})\s*H?\.?\s*(.+)",
            text, re.IGNORECASE | re.DOTALL,
        )
        if not m:
            continue
        day, mes_str, hh, mm, rest = m.groups()
        mes = meses.get(mes_str.lower())
        if not mes:
            continue
        try:
            dt = datetime(year, mes, int(day), int(hh), int(mm))
        except ValueError:
            continue
        title = rest.strip().split("\n")[0].strip()
        if len(title) < 3:
            continue
        sessions.append({
            "movie": title,
            "date": dt.date().isoformat(),
            "time": dt.strftime("%H:%M"),
            "duration": None,
            "rating": "?",
            "sala": "Sala única",
            "url": a.get("href", url),
        })

    log(f"    {len(sessions)} eventos encontrados")
    return sessions


# ============================================================
# MAIN
# ============================================================
def main():
    today = datetime.now().date()
    max_date = today + timedelta(days=DAYS_AHEAD)

    output = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "fromDate": today.isoformat(),
        "toDate": max_date.isoformat(),
        "cinemas": [],
    }

    for cfg in CINEMAS_NEOCINE:
        try:
            raw = scrape_neocine_cinema(cfg)
            enriched = enrich_with_graphql([s["show_id"] for s in raw])
        except Exception as e:
            log(f"  ERROR scrapeando {cfg['name']}: {e}")
            continue

        sessions = []
        salas = set()
        for s in raw:
            extra = enriched.get(s["show_id"])
            if not extra or not extra.get("beginning"):
                continue
            try:
                dt = datetime.fromisoformat(extra["beginning"].replace("Z", "+00:00"))
                local = dt.astimezone()
                d = local.date()
            except Exception:
                continue
            if not (today <= d <= max_date):
                continue
            sala = extra.get("auditorium") or "?"
            if sala != "?":
                salas.add(sala)
            sessions.append({
                "movie": s["movie"],
                "date": d.isoformat(),
                "time": local.strftime("%H:%M"),
                "duration": s["duration"],
                "rating": s["rating"],
                "sala": sala,
                "url": s["neocine_url"],
            })

        output["cinemas"].append({
            "id": cfg["id"],
            "name": cfg["name"],
            "area": cfg["area"],
            "color": cfg["color"],
            "salas": sorted(salas),
            "sessions": sessions,
        })

    filmo = [
        s for s in scrape_filmoteca()
        if today.isoformat() <= s["date"] <= max_date.isoformat()
    ]
    output["cinemas"].append({
        "id": "filmoteca",
        "name": "Filmoteca Regional",
        "area": "Murcia",
        "color": "#dc2626",
        "salas": ["Sala única"],
        "sessions": filmo,
    })

    json.dump(output, sys.stdout, ensure_ascii=False, indent=2)
    log("✓ JSON generado")


if __name__ == "__main__":
    main()
