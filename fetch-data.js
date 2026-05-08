/**
 * fetch-data.js
 * Scraper cliente: neocine.es + filmotecamurcia.carm.es + TMDB
 * Usa api.allorigins.win para páginas HTML y corsproxy.io para GraphQL POST.
 * Si el scraping falla o devuelve 0 sesiones, lanza un error para que
 * index.html caiga al JSON estático (data/schedule.json).
 */

const TMDB_KEY    = 'a95b0d598e9119d6ec87906127a1fa1c';
const TMDB_SEARCH = 'https://api.themoviedb.org/3/search/movie';
const TMDB_GENRES = 'https://api.themoviedb.org/3/genre/movie/list';
const TMDB_IMG    = 'https://image.tmdb.org/t/p/w342';

const GQL_URL   = 'https://entradas-next-live.kinoheld.de/graphql';
// GraphQL necesita POST — usamos corsproxy.io que sí reenvía el body
const GQL_PROXY = `https://corsproxy.io/?${encodeURIComponent(GQL_URL)}`;
// Consulta masiva por cine: una sola petición devuelve todos los pases con urlSlug + sala
// movie{name} añadido para obtener título directamente de kinoheld (cubre salas que la web no muestra)
const GQL_SHOWS_QUERY = 'query S($cid:ID!){shows(cinemaId:$cid){data{urlSlug beginning auditorium{name} movie{name}}}}';

// Bump la versión cuando el formato de caché cambie para limpiar datos viejos
const CACHE_KEY = 'cines-murcia-live-v6';
const CACHE_TTL = 3 * 60 * 60 * 1000; // 3 horas
const DAYS_AHEAD = 14;

const CINEMAS_CFG = [
  { id:'myrtea',     name:'Myrtea Premium',  area:'El Tiro, Murcia', color:'#7c3aed',
    url:'https://www.neocine.es/cine/5/hd-digital-myrtea--el-tiro---murcia-/lang/es',
    cid:'MzY1NjYwNA',
    entradaSlug:'neocine-myrtea-HD-digital' },
  { id:'centrofama', name:'Centrofama',       area:'Murcia centro',   color:'#0891b2',
    url:'https://www.neocine.es/cine/1/centrofama--murcia-/lang/es',
    cid:'2943',
    entradaSlug:'neocine-centrofama-HD-digital' },
];

const FILMOTECA_BASE = 'https://filmotecamurcia.carm.es';
const FILMOTECA_P0   = FILMOTECA_BASE + '/servlet/s.Sl?METHOD=ENLACEMENUS&sit=c,884,m,3623,a,0';
const FILMOTECA_PN   = ofs => FILMOTECA_BASE + `/servlet/s.Sl?sit=c,884,m,3623,i,1,a,0,ofs,${ofs}`;

const MESES = {
  enero:1,febrero:2,marzo:3,abril:4,mayo:5,junio:6,
  julio:7,agosto:8,septiembre:9,octubre:10,noviembre:11,diciembre:12
};
const SKIP_WORDS = ['inicio','contacto','filmoteca','facebook','twitter','instagram',
                    'región','copyright','aviso','politica','cookies','compra','entradas'];

// ── Caché ─────────────────────────────────────────────────────────────────

function getCache() {
  try {
    const raw = localStorage.getItem(CACHE_KEY);
    if (!raw) return null;
    const { ts, data } = JSON.parse(raw);
    if (Date.now() - ts < CACHE_TTL) return data;
  } catch {}
  return null;
}

function setCache(data) {
  try { localStorage.setItem(CACHE_KEY, JSON.stringify({ ts: Date.now(), data })); } catch {}
}

// ── CORS GET (páginas HTML) ───────────────────────────────────────────────

async function corsGet(url) {
  const r = await fetch(`https://api.allorigins.win/get?url=${encodeURIComponent(url)}`);
  if (!r.ok) throw new Error(`proxy HTTP ${r.status}`);
  const json = await r.json();
  if (!json.contents) throw new Error('allorigins: contenido vacío');
  return json.contents;
}

// ── Helpers ───────────────────────────────────────────────────────────────

function localDateStr(dt) {
  return `${dt.getFullYear()}-${String(dt.getMonth()+1).padStart(2,'0')}-${String(dt.getDate()).padStart(2,'0')}`;
}
function localTimeStr(dt) {
  return `${String(dt.getHours()).padStart(2,'0')}:${String(dt.getMinutes()).padStart(2,'0')}`;
}
function detectRating(src) {
  const s = (src||'').toLowerCase();
  if (s.includes('para_todos') || s.includes('_tp'))  return 'TP';
  if (s.includes('menores_7')  || s.includes('_7_'))  return '+7';
  if (s.includes('menores_12') || s.includes('_12'))  return '+12';
  if (s.includes('menores_16') || s.includes('_16'))  return '+16';
  if (s.includes('menores_18') || s.includes('_18'))  return '+18';
  return '?';
}
function parseSalaAndFormat(rawName) {
  if (!rawName) return { sala:'?', vose:false, format:'' };
  const name = rawName.trim();
  const vose = /v\.?o\.?s?\.?e?\.?/i.test(name);
  let format = '';
  if (/\bimax\b/i.test(name))     format = 'IMAX';
  else if (/\b4dx\b/i.test(name)) format = '4DX';
  else if (/\b3d\b/i.test(name))  format = '3D';
  const clean = name
    .replace(/\b(v\.?o\.?s?\.?e?\.?|vose|3d|imax|4dx|hfr|atmos)\b/gi, '')
    .replace(/[\s\-·|]+$/,'').replace(/^[\s\-·|]+/,'').trim();
  return { sala: clean||name, vose, format };
}

// Encuentra el h3 más cercano que precede a 'el' en orden documento
function findPrecedingH3(el) {
  const h3s = Array.from(el.ownerDocument.querySelectorAll('h3'));
  const before = h3s.filter(h3 =>
    el.compareDocumentPosition(h3) & Node.DOCUMENT_POSITION_PRECEDING
  );
  return before[before.length-1] || null;
}

async function batchMap(items, fn, size=6) {
  const out = [];
  for (let i=0; i<items.length; i+=size)
    out.push(...await Promise.all(items.slice(i,i+size).map(fn)));
  return out;
}

// ── Neocine ───────────────────────────────────────────────────────────────

function parseNeocinePage(html) {
  const doc = new DOMParser().parseFromString(html, 'text/html');
  const shows = [], seen = new Set();

  doc.querySelectorAll('a[href*="entradas.com"][href*="/evento/"]').forEach(link => {
    // Texto del enlace: debe ser sólo HH:MM (toleramos espacios extra)
    const timeText = link.textContent.replace(/\s+/g,'');
    if (!/^\d{1,2}:\d{2}$/.test(timeText)) return;

    const href = link.getAttribute('href')||'';
    const idm  = href.match(/\/evento\/(\d+)/);
    if (!idm) return;
    const showId = idm[1];
    if (seen.has(showId)) return;
    seen.add(showId);

    const h3 = findPrecedingH3(link);
    const movieTitle = h3 ? (h3.querySelector('a')||h3).textContent.trim() : '?';

    let duration=null, el=link.parentElement;
    for (let i=0;i<8&&el&&!duration;i++,el=el.parentElement){
      const m=el.textContent.match(/Duraci[oó]n[:\s]+(\d+)/i);
      if(m) duration=parseInt(m[1]);
    }
    let rating='?';
    el=link.parentElement;
    for (let i=0;i<8&&el&&rating==='?';i++,el=el.parentElement){
      const img=el.querySelector('img[src*="calificacion"]');
      if(img) rating=detectRating(img.getAttribute('src')||'');
    }

    shows.push({ movie:movieTitle, showId, duration, rating, url:href.split('?')[0] });
  });
  return shows;
}

async function scrapeNeocine(cinema) {
  try {
    const html = await corsGet(cinema.url);
    const shows = parseNeocinePage(html);
    console.log(`[neocine] ${cinema.id}: ${shows.length} sesiones encontradas`);
    return shows;
  } catch(e) {
    console.warn('[neocine]', cinema.id, e.message);
    return [];
  }
}

// ── GraphQL (via corsproxy.io para evitar CORS) ───────────────────────────
// Una sola petición por cine devuelve todos los pases con urlSlug, beginning y auditorium.
// urlSlug == el ID numérico de la URL de entradas.com (ej. "422182").

async function fetchCinemaShows(cinema) {
  try {
    const r = await fetch(GQL_PROXY, {
      method: 'POST',
      headers: { 'Content-Type':'application/json', 'Accept-Language':'es' },
      body: JSON.stringify({ query:GQL_SHOWS_QUERY, variables:{ cid:cinema.cid } }),
    });
    const j = await r.json();
    const items = j?.data?.shows?.data || [];
    // Construir mapa urlSlug → {beginning, auditorium}
    const map = {};
    items.forEach(s => {
      if(s.urlSlug) map[s.urlSlug] = {
        beginning:  s.beginning,
        auditorium: s.auditorium?.name,
        movieName:  s.movie?.name || null,
      };
    });
    console.log(`[graphql] ${cinema.id}: ${items.length} pases recibidos`);
    return map;
  } catch(e) {
    console.warn('[graphql]', cinema.id, e.message);
    return {};
  }
}

// ── Filmoteca ─────────────────────────────────────────────────────────────

const FILMOTECA_RE = /^(\d{1,2})\s+(\w+)\s+(\d{1,2}):(\d{2})\s*H?\.?\s+(.+?)(?:\s+icono\s+visualizaci[oó]n.*)?$/i;

function parseFilmotecaPage(html, todayStr, maxDateStr) {
  const doc=new DOMParser().parseFromString(html,'text/html');
  const now=new Date(), events=[];
  doc.querySelectorAll('a[href*="DETALLE_EVENTO"]').forEach(a=>{
    const text=a.textContent.replace(/\s+/g,' ').trim();
    const m=FILMOTECA_RE.exec(text);
    if(!m) return;
    const [,day,mesStr,hh,mm,rawTitle]=m;
    const mes=MESES[mesStr.toLowerCase()];
    if(!mes) return;
    let title=rawTitle.trim().replace(/\s*icono\s+visualizaci[oó]n.*/i,'').trim();
    if(title.toLowerCase().includes('(cartagena)')||title.length<3) return;
    const year=mes<now.getMonth()+1?now.getFullYear()+1:now.getFullYear();
    const date=`${year}-${String(mes).padStart(2,'0')}-${String(parseInt(day)).padStart(2,'0')}`;
    if(date<todayStr||date>maxDateStr) return;
    let href=a.getAttribute('href')||'';
    if(!href.startsWith('http')) href=FILMOTECA_BASE+(href.startsWith('/')?href:'/'+href);
    events.push({ movie:title, date,
      time:`${String(parseInt(hh)).padStart(2,'0')}:${mm}`,
      duration:null, rating:'?', sala:null, url:href });
  });
  return events;
}

async function scrapeFilmotecaList(todayStr, maxDateStr) {
  const all=[], seen=new Set();
  let empty=0;
  for (let ofs=0;ofs<200;ofs+=12) {
    try {
      const url=ofs===0?FILMOTECA_P0:FILMOTECA_PN(ofs);
      const html=await corsGet(url);
      const page=parseFilmotecaPage(html,todayStr,maxDateStr);
      if(!page.length){ if(++empty>=3) break; continue; }
      let added=0;
      page.forEach(e=>{ if(!seen.has(e.url)){ seen.add(e.url); all.push(e); added++; } });
      empty=added?0:(++empty>=3?Infinity:empty);
      if(empty===Infinity) break;
    } catch { break; }
  }
  console.log(`[filmoteca] ${all.length} eventos en rango`);
  return all;
}

async function fetchFilmotecaDetail(url) {
  try {
    const html=await corsGet(url);
    const doc=new DOMParser().parseFromString(html,'text/html');
    let sala='Sala única';
    const sm=html.match(/\bSala\s+([AB])\b/i);
    if(sm) sala=`Sala ${sm[1].toUpperCase()}`;
    let poster=null;
    for (const img of doc.querySelectorAll('img')) {
      const src=img.getAttribute('src')||'';
      if(src.includes('integra.servlets.Imagenes')){
        poster=src.startsWith('http')?src:FILMOTECA_BASE+src; break;
      }
    }
    const candidates=[];
    doc.querySelectorAll('p,td').forEach(tag=>{
      if(tag.querySelector('p,div,table')) return;
      const t=tag.textContent.replace(/\s+/g,' ').trim();
      if(t.length>80&&t.length<900&&!SKIP_WORDS.some(w=>t.toLowerCase().includes(w)))
        candidates.push(t);
    });
    const overview=candidates.length?candidates.reduce((a,b)=>a.length>=b.length?a:b):null;
    return { sala, poster, overview };
  } catch { return { sala:'Sala única', poster:null, overview:null }; }
}

// ── TMDB ──────────────────────────────────────────────────────────────────

async function fetchTMDBGenres() {
  const r=await fetch(`${TMDB_GENRES}?api_key=${TMDB_KEY}&language=es`);
  const d=await r.json();
  return Object.fromEntries((d.genres||[]).map(g=>[g.id,g.name]));
}

async function fetchTMDBMovie(title, genreMap) {
  for (const lang of ['es','en']) {
    try {
      const r=await fetch(`${TMDB_SEARCH}?api_key=${TMDB_KEY}&query=${encodeURIComponent(title)}&language=${lang}`);
      const d=await r.json();
      const m=(d.results||[])[0];
      if(m) return {
        poster:     m.poster_path?`${TMDB_IMG}${m.poster_path}`:null,
        overview:   m.overview||null,
        vote_average: m.vote_average?Math.round(m.vote_average*10)/10:null,
        genres:     (m.genre_ids||[]).map(id=>genreMap[id]).filter(Boolean).slice(0,4),
        year:       m.release_date?parseInt(m.release_date):null,
      };
    } catch {}
  }
  return null;
}

async function enrichWithTMDB(titles, movies, genreMap, onProgress) {
  const BATCH=6;
  for (let i=0;i<titles.length;i+=BATCH) {
    await Promise.all(titles.slice(i,i+BATCH).map(async title=>{
      const entry=movies[title]||{};
      if(entry.poster&&entry.overview&&entry.vote_average) return;
      const tmdb=await fetchTMDBMovie(title,genreMap);
      movies[title]=tmdb?{
        poster:       entry.poster      ||tmdb.poster,
        overview:     entry.overview    ||tmdb.overview,
        vote_average: entry.vote_average||tmdb.vote_average,
        genres:       (entry.genres?.length?entry.genres:tmdb.genres)||[],
        year:         entry.year        ||tmdb.year,
      }:(movies[title]||{});
    }));
    onProgress(
      `Buscando carátulas… ${Math.min(i+BATCH,titles.length)}/${titles.length}`,
      65+(Math.min(i+BATCH,titles.length)/titles.length)*30
    );
  }
}

// ── Orquestador ───────────────────────────────────────────────────────────

async function fetchLiveData(onProgress=()=>{}) {
  const cached=getCache();
  if(cached){ onProgress('Datos en caché ✓',100); return cached; }

  const today=new Date();
  const todayStr=localDateStr(today);
  const maxStr=localDateStr(new Date(today.getTime()+DAYS_AHEAD*86400000));

  // 1. HTML + GraphQL bulk en paralelo (2 peticiones GQL en vez de N individuales)
  onProgress('Leyendo cartelera de cines…',5);
  const [myrteaShows, centrofamaShows, filmoEvents, gqlMyrtea, gqlCentrofama] = await Promise.all([
    scrapeNeocine(CINEMAS_CFG[0]),
    scrapeNeocine(CINEMAS_CFG[1]),
    scrapeFilmotecaList(todayStr, maxStr),
    fetchCinemaShows(CINEMAS_CFG[0]),
    fetchCinemaShows(CINEMAS_CFG[1]),
  ]);
  onProgress('Horarios y salas cargados…', 40);

  // 2. Detalles Filmoteca
  onProgress(`Cargando detalles Filmoteca (${filmoEvents.length} eventos)…`,46);
  const details=await batchMap(filmoEvents,e=>fetchFilmotecaDetail(e.url),4);
  filmoEvents.forEach((e,i)=>{ e.sala=details[i].sala; e._poster=details[i].poster; e._overview=details[i].overview; });

  // 3. Construir cines comerciales
  // Estrategia: GraphQL es la fuente completa (todos los pases, todas las salas).
  // El HTML scrape aporta duración, calificación y título exacto de neocine.es.
  // Iteramos GraphQL para no perder pases que el HTML no incluya (ej: SALA 5).
  const cinemas=[];
  for (const [cfg, htmlShows, gql] of [
    [CINEMAS_CFG[0], myrteaShows,    gqlMyrtea],
    [CINEMAS_CFG[1], centrofamaShows, gqlCentrofama],
  ]) {
    // Mapa showId → datos del HTML (título, duración, calificación, URL exacta)
    const htmlMap = {};
    htmlShows.forEach(s => { htmlMap[s.showId] = s; });

    const sessions=[], salas=new Set();
    Object.entries(gql).forEach(([showId, extra]) => {
      if(!extra.beginning) return;
      const dt   = new Date(extra.beginning);
      const date = localDateStr(dt);
      if(date < todayStr || date > maxStr) return;
      const {sala, vose, format} = parseSalaAndFormat(extra.auditorium);
      if(sala && sala !== '?') salas.add(sala);
      const html = htmlMap[showId] || {};
      // Preferimos título del HTML (coincide con neocine.es); si no, usamos el de kinoheld
      const movie = html.movie || extra.movieName || '?';
      const url   = html.url  || `https://cine.entradas.com/cine/murcia/${cfg.entradaSlug}/evento/${showId}`;
      sessions.push({ movie, date, time:localTimeStr(dt),
        duration: html.duration || null,
        rating:   html.rating   || '?',
        sala, vose, format: format||null, url });
    });
    sessions.sort((a,b) => a.date.localeCompare(b.date) || a.time.localeCompare(b.time));
    cinemas.push({ id:cfg.id, name:cfg.name, area:cfg.area, color:cfg.color,
                   salas:[...salas].sort(), sessions });
  }

  // 5. Filmoteca
  const filmoSalas=[...new Set(filmoEvents.map(s=>s.sala).filter(Boolean))].sort();
  const movies={};
  filmoEvents.forEach(s=>{
    if(s._poster||s._overview)
      movies[s.movie]={ poster:s._poster||null, overview:s._overview||null,
                        vote_average:null, genres:[], year:null };
  });
  cinemas.push({
    id:'filmoteca',name:'Filmoteca Regional',area:'Murcia',color:'#dc2626',
    salas:filmoSalas.length?filmoSalas:['Sala A','Sala B'],
    sessions:filmoEvents.map(({_poster,_overview,...s})=>s),
  });

  // Validación: si no hay ninguna sesión, no cachear y dejar caer al JSON estático
  const total=cinemas.reduce((n,c)=>n+c.sessions.length,0);
  console.log(`[fetchLiveData] total sesiones: ${total}`);
  if(total===0) throw new Error('El scraper no encontró sesiones — usando JSON estático');

  // 6. TMDB
  onProgress('Buscando carátulas y sinopsis…',65);
  const allTitles=[...new Set(cinemas.flatMap(c=>c.sessions.map(s=>s.movie)))];
  let genreMap={};
  try{ genreMap=await fetchTMDBGenres(); }catch{}
  await enrichWithTMDB(allTitles,movies,genreMap,onProgress);
  allTitles.forEach(t=>{ movies[t]=movies[t]||{}; });

  const data={
    generatedAt:new Date().toISOString(),
    fromDate:todayStr, toDate:maxStr,
    movies, cinemas,
  };
  setCache(data);
  onProgress('¡Listo!',100);
  return data;
}
