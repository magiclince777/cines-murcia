# Cines de Murcia 🎬

Aplicación web que muestra la programación de Neocine Myrtea, Centrofama y la Filmoteca Regional de Murcia, **organizada por sala**, por película o por horario.

Los datos se actualizan automáticamente cada día gracias a una GitHub Action.

## Cómo funciona

- `scraper.py` obtiene los horarios scrapeando neocine.es y consultando la API GraphQL de entradas.com (de donde sale el nombre exacto de cada sala).
- El resultado se guarda en `data/schedule.json`.
- `index.html` lee ese JSON y lo renderiza en tres vistas: por sala, por película, por horario.

## Despliegue (5 minutos, gratis para siempre)

### 1. Crear un repositorio nuevo en GitHub
- Ve a https://github.com/new
- Nombre: lo que quieras (ej. `cines-murcia`)
- Visibilidad: **Public** (necesario para que GitHub Pages sea gratis)
- **Sin** README, sin .gitignore, sin licencia (los pondremos manualmente)

### 2. Subir estos archivos
Desde la terminal, en la carpeta `cines-murcia`:

```bash
git init
git add .
git commit -m "init"
git branch -M main
git remote add origin https://github.com/TU_USUARIO/cines-murcia.git
git push -u origin main
```

(Si no manejas terminal: arrastra los archivos a la web de GitHub usando "uploading an existing file".)

### 3. Activar GitHub Pages
- Ve a Settings → Pages
- Source: **Deploy from a branch**
- Branch: **main** / **/ (root)**
- Guardar
- A los 30 segundos tu web está en `https://TU_USUARIO.github.io/cines-murcia/`

### 4. Activar la actualización automática
- Ve a Settings → Actions → General → Workflow permissions
- Marca **Read and write permissions**
- Guardar
- Ve a la pestaña **Actions**, abre "Scrape cines", pulsa **Run workflow** una vez para probar
- A partir de aquí se ejecutará todos los días automáticamente a las 8:30 hora de Murcia

### 5. Añadir a la pantalla de inicio del móvil (iOS)
- Abre la URL en Safari
- Botón compartir → "Añadir a pantalla de inicio"
- Listo, ya tienes "app" sin pasar por la App Store

## Probar el scraper en local (opcional)

```bash
pip install -r requirements.txt
python scraper.py > data/schedule.json
open index.html  # o doble clic
```

## Estructura

```
cines-murcia/
├── README.md
├── requirements.txt
├── scraper.py              ← scraper Python (Neocine HTML + entradas.com GraphQL)
├── index.html              ← web app (vanilla JS, sin dependencias)
├── data/
│   └── schedule.json       ← lo regenera la Action diariamente
└── .github/
    └── workflows/
        └── scrape.yml      ← cron diario
```

## Personalizar

- **Cambiar cines**: edita la lista `CINEMAS_NEOCINE` en `scraper.py`. Cualquier cine del circuito Neocine vale.
- **Cambiar la hora del cron**: edita `cron: '30 6 * * *'` en `.github/workflows/scrape.yml`. Está en UTC.
- **Cambiar colores/diseño**: todo el CSS está al principio de `index.html`.

## Notas

- La Filmoteca tiene una estructura HTML antigua y publica con poco margen, así que su parser es menos fiable. Si quieres mejorarlo, retoca `scrape_filmoteca()` en `scraper.py`.
- Si la API de entradas.com cambiara y dejara de devolver el campo `auditorium`, el scraper seguiría funcionando pero pondría `?` en la sala.
