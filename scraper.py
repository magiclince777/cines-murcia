"""
Scraper de programación de cines de Murcia.

Estrategia:
  1. Scrapea las páginas HTML de neocine.es para obtener
     título, duración, calificación e ID de cada sesión.
  2. Para cada ID, llama al GraphQL de entradas.com para obtener
     sala (detectando VOSE/3D/IMAX/4DX), fecha y hora exactas.
  3. Pagina filmotecamurcia.carm.es; para cada evento visita su
     página de detalle para obtener Sala A/B, carátula y sinopsis.
  4. Enriquece con TMDB (poster, sinopsis, puntuación, géneros, año).
  5. Genera data/schedule.json.

Uso:
    pip install -r requirements.txt
    python scraper.py > data/schedule.json      # genera siempre
    python scraper.py --check > data/schedule.json  # omite si <2h
"""
import json
import os
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

DAYS_AHEAD = 14

# ── TMDB ────────────────────────────────────────────────────────────────────
# Regístrate gratis en https://www.themoviedb.org/settings/api y pon tu clave:
#export TMDB_API_KEY="a95b0d598e9119d6ec87906127a1fa1c"
TMDB_API_KEY  = os.environ.get("TMDB_API_KEY", "a95b0d598e9119d6ec87906127a1fa1c")
TMDB_SEARCH   = "https://api.themoviedb.org/3/search/movie"
TMDB_GENRES   = "https://api.themoviedb.org/3/genre/movie/list"
TMDB_IMG_BASE = "https://image.tmdb.org/t/p/w342"

_tmdb_genre_map: dict = {}


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


# ============================================================
# TMDB
# ============================================================
def _get_tmdb_genre_map() -> dict:
    global _tmdb_genre_map
    if _tmdb_genre_map or not TMDB_API_KEY:
        return _tmdb_genre_map
    try:
        r = requests.get(TMDB_GENRES, params={"api_key": TMDB_API_KEY, "language": "es"}, timeout=10)
        _tmdb_genre_map = {g["id"]: g["name"] for g in r.json().get("genres", [])}
    except Exception as e:
        log(f"    TMDB géneros error: {e}")
    return _tmdb_genre_map


def fetch_tmdb_movie(title: str) -> dict | None:
    """Devuelve {poster, overview, vote_average, genres, year} o None."""
    if not TMDB_API_KEY:
        return None
    genre_map = _get_tmdb_genre_map()
    for lang in ("es", "en"):
        try:
            r = requests.get(TMDB_SEARCH, params={"api_key": TMDB_API_KEY, "query": title, "language": lang}, timeout=10)
            results = r.json().get("results", [])
            if results:
                m = results[0]
                genres = [genre_map.get(gid) for gid in m.get("genre_ids", []) if genre_map.get(gid)][:4]
                year_str = m.get("release_date", "")
                return {
                    "poster":       TMDB_IMG_BASE + m["poster_path"] if m.get("poster_path") else None,
                    "overview":     m.get("overview") or None,
                    "vote_average": round(m["vote_average"], 1) if m.get("vote_average") else None,
                    "genres":       genres,
                    "year":         int(year_str[:4]) if year_str else None,
                }
        except Exception as e:
            log(f"    TMDB error '{title}' [{lang}]: {e}")
    return None


# ============================================================
# UTILIDADES
# ============================================================
def detect_rating(img_src: str) -> str:
    if not img_src:
        return "?"
    s = img_src.lower()
    if "para_todos_los_publicos" in s: return "TP"
    if "no_recomendada_menores_7"  in s: return "+7"
    if "no_recomendada_menores_12" in s: return "+12"
    if "no_recomendada_menores_16" in s: return "+16"
    if "no_recomendada_menores_18" in s: return "+18"
    return "?"


_FORMAT_RE = re.compile(r'\b(v\.o\.s?\.e?\.?|vose|v\.o\.|3d|imax|4dx|hfr|atmos)\b', re.I)


def parse_sala_and_format(raw_name: str | None) -> tuple[str, bool, str]:
    """Devuelve (sala_limpia, vose, format_tag)."""
    if not raw_name:
        return "?", False, ""
    name = raw_name.strip()
    vose   = bool(re.search(r'\bv\.?o\.?s?\.?e?\.?\b', name, re.I))
    fmt    = ""
    if re.search(r'\bimax\b', name, re.I):    fmt = "IMAX"
    elif re.search(r'\b4dx\b', name, re.I):   fmt = "4DX"
    elif re.search(r'\b3d\b', name, re.I):    fmt = "3D"
    clean  = _FORMAT_RE.sub('', name).strip(' -·|').strip()
    return clean or name, vose, fmt


# ============================================================
# SCRAPER NEOCINE
# ============================================================
def scrape_neocine_cinema(cinema: dict) -> list[dict]:
    log(f"  Scrapeando {cinema['name']}…")
    r = requests.get(cinema["neocine_url"], headers={"User-Agent": USER_AGENT, "Accept-Language": "es"}, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    shows, seen_ids = [], set()
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
            "movie":      movie_title,
            "show_id":    show_id,
            "duration":   duration,
            "rating":     rating,
            "neocine_url": href.split("?")[0],
        })

    log(f"    {len(shows)} sesiones únicas encontradas")
    return shows


# ============================================================
# ENRIQUECIMIENTO VIA GRAPHQL
# ============================================================
def enrich_with_graphql(show_ids: list[str]) -> dict:
    results, total = {}, len(show_ids)
    for i, sid in enumerate(show_ids):
        if i and i % 20 == 0:
            log(f"    GraphQL: {i}/{total}…")
        try:
            r = requests.post(
                GRAPHQL_URL,
                json={"query": GRAPHQL_QUERY, "variables": {"id": str(sid)}},
                headers={"Content-Type": "application/json", "Accept-Language": "es", "User-Agent": USER_AGENT},
                timeout=15,
            )
            data = r.json()
            show = (data.get("data") or {}).get("show")
            if show:
                aud = show.get("auditorium") or {}
                results[sid] = {"auditorium": aud.get("name"), "beginning": show.get("beginning")}
            time.sleep(0.05)
        except Exception as e:
            log(f"    Fallo en show {sid}: {e}")
    log(f"    GraphQL completo: {len(results)}/{total} respondidos")
    return results


# ============================================================
# FILMOTECA REGIONAL
# ============================================================
MESES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}

EVENT_PATTERN = re.compile(
    r"(\d{1,2})\s+(\w+)\s+(\d{1,2}):(\d{2})\s*H?\.?\s+(.+?)(?:\s+icono\s+visualización)?$",
    re.IGNORECASE | re.DOTALL,
)

_SKIP_WORDS = {"inicio", "contacto", "filmoteca", "facebook", "twitter", "instagram",
               "región", "copyright", "aviso", "politica", "cookies", "compra", "entradas"}


def fetch_filmoteca_page(offset: int) -> BeautifulSoup:
    url = ("https://filmotecamurcia.carm.es/servlet/s.Sl?METHOD=ENLACEMENUS&sit=c,884,m,3623,a,0"
           if offset == 0 else
           f"https://filmotecamurcia.carm.es/servlet/s.Sl?sit=c,884,m,3623,i,1,a,0,ofs,{offset}")
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    r.raise_for_status()
    r.encoding = "iso-8859-1"
    return BeautifulSoup(r.text, "html.parser")


def parse_filmoteca_page(soup: BeautifulSoup, today) -> list[dict]:
    sessions = []
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
        if "(cartagena)" in title.lower():
            continue
        title = re.sub(r"\s*icono\s+visualización.*$", "", title, flags=re.IGNORECASE).strip()
        if len(title) < 3:
            continue

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
            "movie":    title,
            "date":     dt.date().isoformat(),
            "time":     dt.strftime("%H:%M"),
            "duration": None,
            "rating":   "?",
            "sala":     None,
            "url":      url,
            "_dt":      dt,
        })
    return sessions


def fetch_filmoteca_detail(url: str) -> tuple[str, str | None, str | None]:
    """Devuelve (sala, poster_url, overview)."""
    sala, poster_url, overview = "Sala única", None, None
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
        r.raise_for_status()
        r.encoding = "iso-8859-1"
        soup = BeautifulSoup(r.text, "html.parser")

        # Sala
        m = re.search(r'\bSala\s+([AB])\b', r.text, re.IGNORECASE)
        if m:
            sala = f"Sala {m.group(1).upper()}"

        # Carátula
        for img in soup.find_all("img"):
            src = img.get("src", "")
            if "integra.servlets.Imagenes" in src:
                poster_url = ("https://filmotecamurcia.carm.es" + src
                              if not src.startswith("http") else src)
                break

        # Sinopsis — buscamos el párrafo de texto más largo que no sea navegación
        candidates = []
        for tag in soup.find_all(["p", "td", "div"]):
            if tag.find(["p", "div", "table"]):  # no containers anidados
                continue
            t = tag.get_text(" ", strip=True)
            if 80 < len(t) < 900 and not any(w in t.lower() for w in _SKIP_WORDS):
                candidates.append(t)
        if candidates:
            overview = max(candidates, key=len)

    except Exception as e:
        log(f"    Fallo al obtener detalle de {url}: {e}")
    return sala, poster_url, overview


def scrape_filmoteca() -> list[dict]:
    log("  Scrapeando Filmoteca Regional…")
    today = datetime.now().date()
    max_date = today + timedelta(days=DAYS_AHEAD)

    all_sessions, seen_urls, pages_without_future = [], set(), 0

    for offset in range(0, 200, 12):
        try:
            soup = fetch_filmoteca_page(offset)
            page_sessions = parse_filmoteca_page(soup, today)
        except Exception as e:
            log(f"    Error en offset {offset}: {e}")
            break

        if not page_sessions:
            log(f"    Página {offset//12+1}: sin eventos, paramos")
            break

        future_in_page = 0
        for s in page_sessions:
            if s["url"] in seen_urls:
                continue
            seen_urls.add(s["url"])
            if s["_dt"].date() < today or s["_dt"].date() > max_date:
                continue
            future_in_page += 1
            all_sessions.append(s)

        if future_in_page == 0:
            pages_without_future += 1
            if pages_without_future >= 3:
                log(f"    Sin más eventos en rango tras offset {offset}, paramos")
                break
        else:
            pages_without_future = 0

        time.sleep(0.2)

    all_sessions.sort(key=lambda s: s["_dt"])
    for s in all_sessions:
        del s["_dt"]

    log(f"    Obteniendo detalle para {len(all_sessions)} eventos…")
    for s in all_sessions:
        s["sala"], s["_poster"], s["_overview"] = fetch_filmoteca_detail(s["url"])
        time.sleep(0.2)

    log(f"    {len(all_sessions)} eventos futuros encontrados")
    return all_sessions


# ============================================================
# MAIN
# ============================================================
def main() -> None:
    # ── Actualización incremental ───────────────────────────────
    if "--check" in sys.argv:
        target = "data/schedule.json"
        if os.path.exists(target):
            age_min = (time.time() - os.path.getmtime(target)) / 60
            if age_min < 120:
                log(f"  schedule.json generado hace {age_min:.0f} min — omitiendo (sin --check para forzar)")
                with open(target, encoding="utf-8") as f:
                    sys.stdout.write(f.read())
                return

    today = datetime.now().date()
    max_date = today + timedelta(days=DAYS_AHEAD)

    output: dict = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "fromDate":    today.isoformat(),
        "toDate":      max_date.isoformat(),
        "cinemas":     [],
    }

    # ── Cines comerciales (Neocine) ─────────────────────────────
    for cfg in CINEMAS_NEOCINE:
        try:
            raw      = scrape_neocine_cinema(cfg)
            enriched = enrich_with_graphql([s["show_id"] for s in raw])
        except Exception as e:
            log(f"  ERROR scrapeando {cfg['name']}: {e}")
            continue

        sessions, salas = [], set()
        for s in raw:
            extra = enriched.get(s["show_id"])
            if not extra or not extra.get("beginning"):
                continue
            try:
                dt    = datetime.fromisoformat(extra["beginning"].replace("Z", "+00:00"))
                local = dt.astimezone()
                d     = local.date()
            except Exception:
                continue
            if not (today <= d <= max_date):
                continue

            sala_raw = extra.get("auditorium")
            sala, vose, fmt = parse_sala_and_format(sala_raw)
            if sala and sala != "?":
                salas.add(sala)

            sessions.append({
                "movie":    s["movie"],
                "date":     d.isoformat(),
                "time":     local.strftime("%H:%M"),
                "duration": s["duration"],
                "rating":   s["rating"],
                "sala":     sala,
                "vose":     vose,
                "format":   fmt or None,
                "url":      s["neocine_url"],
            })

        output["cinemas"].append({
            "id":       cfg["id"],
            "name":     cfg["name"],
            "area":     cfg["area"],
            "color":    cfg["color"],
            "salas":    sorted(salas),
            "sessions": sessions,
        })

    # ── Filmoteca ────────────────────────────────────────────────
    filmo_sessions = scrape_filmoteca()
    filmo_salas    = sorted({s["sala"] for s in filmo_sessions if s.get("sala")}) or ["Sala A", "Sala B"]
    output["cinemas"].append({
        "id":       "filmoteca",
        "name":     "Filmoteca Regional",
        "area":     "Murcia",
        "color":    "#dc2626",
        "salas":    filmo_salas,
        "sessions": filmo_sessions,
    })

    # ── Construir dict movies ────────────────────────────────────
    movies: dict = {}

    # 1) Datos propios de la Filmoteca (poster + sinopsis)
    for s in filmo_sessions:
        title    = s["movie"]
        poster   = s.pop("_poster",   None)
        overview = s.pop("_overview", None)
        if title not in movies:
            movies[title] = {"poster": None, "overview": None, "vote_average": None, "genres": [], "year": None}
        if poster   and not movies[title]["poster"]:   movies[title]["poster"]   = poster
        if overview and not movies[title]["overview"]: movies[title]["overview"] = overview

    # 2) TMDB para todos los títulos (completa lo que falta)
    if TMDB_API_KEY:
        _get_tmdb_genre_map()   # pre-carga el mapa de géneros
        all_titles = {s["movie"] for c in output["cinemas"] for s in c["sessions"]}
        log(f"  Buscando metadata TMDB para {len(all_titles)} títulos…")
        for title in sorted(all_titles):
            entry = movies.setdefault(title, {"poster": None, "overview": None, "vote_average": None, "genres": [], "year": None})
            if all(entry.get(k) for k in ("poster", "overview", "vote_average")):
                continue  # ya completo
            tmdb = fetch_tmdb_movie(title)
            if tmdb:
                if not entry["poster"]:        entry["poster"]        = tmdb["poster"]
                if not entry["overview"]:      entry["overview"]      = tmdb["overview"]
                if not entry["vote_average"]:  entry["vote_average"]  = tmdb["vote_average"]
                if not entry["genres"]:        entry["genres"]        = tmdb["genres"]
                if not entry["year"]:          entry["year"]          = tmdb["year"]
            time.sleep(0.12)
        log(f"  TMDB completado: {sum(1 for m in movies.values() if m.get('poster'))} con carátula")
    else:
        log("  TMDB_API_KEY no configurada — sin enriquecimiento externo")
        log("  (Regístrate gratis en themoviedb.org/settings/api y exporta TMDB_API_KEY)")
        # Asegurar que todos los títulos aparecen en el dict aunque estén vacíos
        for c in output["cinemas"]:
            for s in c["sessions"]:
                movies.setdefault(s["movie"], {"poster": None, "overview": None, "vote_average": None, "genres": [], "year": None})

    output["movies"] = movies

    json.dump(output, sys.stdout, ensure_ascii=False, indent=2)
    log(f"✓ JSON generado — {sum(len(c['sessions']) for c in output['cinemas'])} sesiones, {len(movies)} películas")


if __name__ == "__main__":
    main()
