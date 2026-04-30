"""
Scraper de programación de cines de Murcia.

Estrategia:
  1. Scrapea las páginas HTML de neocine.es para obtener:
     título de película, duración, calificación, e ID de cada sesión.
  2. Para cada ID de sesión, llama al GraphQL de entradas.com
     (https://entradas-next-live.kinoheld.de/graphql) para obtener
     la SALA exacta y la fecha/hora oficial.
  3. Pagina filmotecamurcia.carm.es y filtra eventos futuros de Murcia.
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

DAYS_AHEAD = 14  # cuántos días futuros guardamos


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

        h3 = link.find_previous("h3")
        movie_title = "?"
        if h3:
            inner = h3.find("a")
            movie_title = (inner or h3).get_text(strip=True)

        duration = None
        dur_text = link.find_next(string=re.compile(r"Duración"))
        if dur_text:
            m2 = re.search(r"Duración:\s*(\d+)", str(dur_text))
            if m2:
                duration = int(m2.group(1))

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
# ENRIQUECIMIENTO VIA GRAPHQL
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
# FILMOTECA REGIONAL — versión paginada
# ============================================================
MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}

# Patrón: "DD mes HH:MM H Título" (todo en el texto del enlace)
EVENT_PATTERN = re.compile(
    r"(\d{1,2})\s+(\w+)\s+(\d{1,2}):(\d{2})\s*H?\.?\s+(.+?)(?:\s+icono\s+visualización)?$",
    re.IGNORECASE | re.DOTALL,
)


def fetch_filmoteca_page(offset):
    """Descarga una página de la programación con paginación."""
    if offset == 0:
        url = "https://filmotecamurcia.carm.es/servlet/s.Sl?METHOD=ENLACEMENUS&sit=c,884,m,3623,a,0"
    else:
        url = f"https://filmotecamurcia.carm.es/servlet/s.Sl?sit=c,884,m,3623,i,1,a,0,ofs,{offset}"
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    r.raise_for_status()
    r.encoding = "iso-8859-1"  # forzamos la codificación correcta
    return BeautifulSoup(r.text, "html.parser")


def parse_filmoteca_page(soup, today):
    """Extrae eventos de una página. Devuelve lista de dicts."""
    sessions = []
    # Cada evento es un <a> que enlaza a "...DETALLE_EVENTO" y cuyo texto
    # contiene "DD mes HH:MM H Título"
    for a in soup.find_all("a", href=re.compile(r"DETALLE_EVENTO")):
        text = a.get_text(" ", strip=True)
        m = EVENT_PATTERN.match(text)
        if not m:
            continue
        day, mes_str, hh, mm, title = m.groups()
        mes = MESES.get(mes_str.lower())
        if not mes:
            continue
        title = title.strip()
        # Descartamos eventos de Cartagena
        if "(cartagena)" in title.lower():
            continue
        # Limpiar el título (quitar el "icono visualización" si quedara colgado)
        title = re.sub(r"\s*icono\s+visualización.*$", "", title, flags=re.IGNORECASE).strip()
        if len(title) < 3:
            continue

        # Calcular el año: si el mes es ≥ mes actual, asumimos año actual;
        # si es menor, asumimos año siguiente (porque la lista mira hacia adelante)
        year = today.year
        if mes < today.month:
            year = today.year + 1
        try:
            dt = datetime(year, mes, int(day), int(hh), int(mm))
        except ValueError:
            continue

        url = a.get("href", "")
        if not url.startswith("http"):
            url = "https://filmotecamurcia.carm.es" + (url if url.startswith("/") else "/" + url)

        sessions.append({
            "movie": title,
            "date": dt.date().isoformat(),
            "time": dt.strftime("%H:%M"),
            "duration": None,
            "rating": "?",
            "sala": None,  # se obtiene desde la página de detalle
            "url": url,
            "_dt": dt,  # para ordenar/filtrar internamente
        })
    return sessions


def fetch_filmoteca_sala(url):
    """Obtiene la sala (Sala A / Sala B) desde la página de detalle del evento."""
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
        r.raise_for_status()
        r.encoding = "iso-8859-1"
        m = re.search(r'\bSala\s+([AB])\b', r.text, re.IGNORECASE)
        if m:
            return f"Sala {m.group(1).upper()}"
    except Exception as e:
        log(f"    Fallo al obtener sala de {url}: {e}")
    return "Sala única"


def scrape_filmoteca():
    """Pagina hasta cubrir los próximos DAYS_AHEAD días."""
    log("  Scrapeando Filmoteca Regional…")
    today = datetime.now().date()
    max_date = today + timedelta(days=DAYS_AHEAD)

    all_sessions = []
    seen_urls = set()
    pages_without_future = 0

    for offset in range(0, 200, 12):  # tope de seguridad: 200 eventos
        try:
            soup = fetch_filmoteca_page(offset)
            page_sessions = parse_filmoteca_page(soup, today)
        except Exception as e:
            log(f"    Error en offset {offset}: {e}")
            break

        if not page_sessions:
            log(f"    Página {offset // 12 + 1}: sin eventos, paramos")
            break

        future_in_page = 0
        for s in page_sessions:
            if s["url"] in seen_urls:
                continue
            seen_urls.add(s["url"])
            if s["_dt"].date() < today:
                continue  # pasado, ignorar
            if s["_dt"].date() > max_date:
                continue  # demasiado lejos, ignorar (pero seguimos paginando por si hay otros antes)
            future_in_page += 1
            all_sessions.append(s)

        # Si llevamos 3 páginas seguidas sin nada futuro útil, paramos
        if future_in_page == 0:
            pages_without_future += 1
            if pages_without_future >= 3:
                log(f"    Sin más eventos en rango tras offset {offset}, paramos")
                break
        else:
            pages_without_future = 0

        time.sleep(0.2)

    # Quitar el campo interno _dt y ordenar
    all_sessions.sort(key=lambda s: s["_dt"])
    for s in all_sessions:
        del s["_dt"]

    # Obtener sala (A/B) desde cada página de detalle
    log(f"    Obteniendo sala para {len(all_sessions)} eventos…")
    for s in all_sessions:
        s["sala"] = fetch_filmoteca_sala(s["url"])
        time.sleep(0.2)

    log(f"    {len(all_sessions)} eventos futuros (próximos {DAYS_AHEAD} días) encontrados")
    return all_sessions


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

    filmo_sessions = scrape_filmoteca()
    filmo_salas = sorted({s["sala"] for s in filmo_sessions if s.get("sala")}) or ["Sala A", "Sala B"]
    output["cinemas"].append({
        "id": "filmoteca",
        "name": "Filmoteca Regional",
        "area": "Murcia",
        "color": "#dc2626",
        "salas": filmo_salas,
        "sessions": filmo_sessions,
    })

    json.dump(output, sys.stdout, ensure_ascii=False, indent=2)
    log("✓ JSON generado")


if __name__ == "__main__":
    main()
