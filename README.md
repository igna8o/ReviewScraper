# ReviewScraper

API autoalojada para sacar reseñas de Google Maps, con la misma interfaz que la
API de Outscraper (`/maps/reviews-v3`, `/requests/{id}`, `X-API-KEY`, modo
asíncrono y webhooks). Se puede usar de tres formas: como servidor HTTP, como
librería de Python o desde la línea de comandos.

> **Estado (4‑nov‑2026):**
> - **Reseñas: probado contra Google de verdad.** Dos páginas seguidas, 20
>   reseñas distintas en el orden pedido, con todos los campos de abajo.
> - **Places API: probada con una clave real.** `/places/autocomplete` y
>   `/places/details` con el Empire State Building; el CID de `googleMapsUri`
>   convertido a `0x0:0x...` devuelve reseñas reales (`pytest -m live`).
> - **Búsqueda por nombre con scraping: sin probar.** Desde servidores en la nube Google
>   responde con un CAPTCHA (HTTP 429). Desde una conexión de casa debería
>   funcionar, pero no está comprobado. Mientras tanto, pasa el enlace de Google
>   Maps o el feature ID (`0x...:0x...`) en lugar del nombre.

## Cómo funciona

Google no tiene una API pública que devuelva todas las reseñas (la Places API
oficial da 5). Este proyecto hace lo mismo que la web de Google, sin cookies:

1. **Buscar el sitio**: `GET https://www.google.com/search?tbm=map&q=...&pb=...`
   devuelve hasta 20 lugares con su *feature ID* (`0x...:0x...`).
2. **Leer reseñas**: `GET /httpservice/web/PrivateLocalSearchUiDataService/GetLocalBoqProxy`
   con un parámetro `reqpld` que lleva el feature ID, el orden y un token de
   página. Cada página trae 10 reseñas y el token de la siguiente.

Desde 2026 la ruta antigua que usaban la mayoría de scrapers
(`/maps/rpc/listugcposts`) devuelve 403 sin sesión. Las rutas de arriba vienen
de las mediciones publicadas por
[HazelnutParadise/insyra](https://github.com/HazelnutParadise/insyra) (MIT,
13‑sep‑2026). Google puede cambiarlas sin aviso. Si la respuesta deja de tener
la forma esperada, el código lanza `FormatChanged` en lugar de devolver una
lista vacía.

Usar estos endpoints va contra los términos de servicio de Google. El código
espera entre 1 y 3 segundos entre páginas. Si haces muchas peticiones, Google
puede bloquear tu IP.

## Integrarlo en un dashboard

Ver [`docs/INTEGRACION_DASHBOARD.md`](docs/INTEGRACION_DASHBOARD.md): el cliente
busca su negocio por nombre, ve sus reseñas al instante y, si quiere
responderlas, conecta su cuenta de Google.

## Instalación

```bash
pip install git+https://github.com/igna8o/ReviewScraper
# o con Docker:
docker build -t reviewscraper . && docker run -p 8000:8000 -e REVIEWSCRAPER_API_KEYS=... reviewscraper
# o, para desarrollar:
git clone https://github.com/igna8o/ReviewScraper && cd ReviewScraper
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
```

## 1. Como servidor (igual que Outscraper)

```bash
export REVIEWSCRAPER_API_KEYS=mi-clave-secreta
reviewscraper serve --host 0.0.0.0 --port 8000
```

```bash
# síncrono
curl -H "X-API-KEY: mi-clave-secreta" \
  "http://localhost:8000/maps/reviews-v3?query=Din+Tai+Fung+Taipei&reviewsLimit=50&sort=newest&async=false"

# asíncrono: devuelve un id y luego se consulta
curl -H "X-API-KEY: mi-clave-secreta" \
  "http://localhost:8000/maps/reviews-v3?query=Din+Tai+Fung+Taipei&reviewsLimit=0&webhook=https://mi.app/hook"
curl -H "X-API-KEY: mi-clave-secreta" http://localhost:8000/requests/<id>
```

Documentación interactiva en `http://localhost:8000/docs`.

### Endpoints

| Endpoint | Qué hace |
|---|---|
| `GET /maps/reviews-v3` | Lugares con sus reseñas en `reviews_data` |
| `GET /maps/search-v3` | Lugares para una búsqueda de texto (con scraping; desde la nube Google pide CAPTCHA) |
| `GET /places/autocomplete` | Sugerencias de negocios mientras se escribe (API oficial de Places) |
| `GET /places/details` | Un `place_id` convertido en lugar con su `google_id`, dirección y nota |
| `GET /requests/{id}` | Resultado de una petición asíncrona (`Pending`, `Success` o `Error`) |
| `GET /requests?type=running\|finished` | Historial |
| `GET /health` | Comprobación de vida |

Parámetros de `/maps/reviews-v3` (mismos nombres que Outscraper):

| Parámetro | Por defecto | Notas |
|---|---|---|
| `query` | — | Feature ID `0x..:0x..`, URL de Google Maps, enlace `?cid=`, `cid:<número>`, `place_id` (`ChIJ...`, necesita `GOOGLE_PLACES_API_KEY`) o un nombre. Se puede repetir |
| `reviewsLimit` | 10 | `0` = todas |
| `limit` | 1 | Lugares por búsqueda |
| `sort` | `most_relevant` | `newest`, `highest_rating`, `lowest_rating` |
| `start`, `cutoff` | — | Timestamps Unix (más nueva / más antigua). Fuerzan `newest` |
| `cutoffRating` | — | Con `highest_rating` o `lowest_rating` |
| `ignoreEmpty` | false | Quita reseñas sin texto |
| `reviewsQuery` | — | Filtra por texto (se aplica localmente, no en Google) |
| `lastPaginationId` | — | `review_pagination_id` de una reseña, para seguir desde la página siguiente |
| `language`, `region` | `en`, — | `hl` y `gl` de Google |
| `fields` | — | Campos a devolver, separados por comas |
| `async` | true | `false` espera el resultado |
| `webhook` | — | URL que recibe un POST `{id, status, results_location, quantity}` al terminar |

Campos de cada reseña: `review_id`, `review_link`, `author_title`, `author_id`,
`author_link`, `author_image`, `author_reviews_count`, `author_photos_count`,
`review_text`, `review_rating`, `review_timestamp`, `review_datetime_utc`,
`review_relative_time`, `review_language`, `review_img_urls`,
`review_pagination_id`.

Los resultados se guardan en SQLite y se borran 2 horas después de terminar.

### Configuración (variables de entorno)

| Variable | Por defecto |
|---|---|
| `REVIEWSCRAPER_API_KEYS` | vacía = sin autenticación (solo para uso local) |
| `REVIEWSCRAPER_DB` | `reviewscraper.db` |
| `REVIEWSCRAPER_RESULT_TTL` | `7200` segundos |
| `REVIEWSCRAPER_CONCURRENCY` | `4` peticiones a la vez |
| `REVIEWSCRAPER_MIN_DELAY` / `MAX_DELAY` | `1` / `3` segundos entre páginas |
| `REVIEWSCRAPER_PROXY` | sin proxy (p. ej. `http://user:pass@host:port`) |
| `REVIEWSCRAPER_GOOGLE_BASE_URL` | `https://www.google.com` |
| `GOOGLE_PLACES_API_KEY` | sin clave = `/places/*` y los `place_id` no funcionan |

### Cliente de Python para el servidor

Tiene los mismos métodos que el SDK de Outscraper:

```python
from reviewscraper.client import ApiClient

client = ApiClient(api_key="mi-clave-secreta", base_url="http://localhost:8000")
places = client.google_maps_reviews("Din Tai Fung Taipei", reviews_limit=50, sort="newest")
for review in places[0]["reviews_data"]:
    print(review["review_rating"], review["review_text"])
```

## 2. Como librería (sin servidor)

```python
import asyncio
from reviewscraper import GoogleMaps

async def main():
    maps = GoogleMaps()
    try:
        [place] = await maps.search("Din Tai Fung Taipei", limit=1)
        reviews = await maps.reviews(place.google_id, reviews_limit=100, sort="newest")
    finally:
        await maps.aclose()
    print(place.name, len(reviews))

asyncio.run(main())
```

## 3. Desde la terminal

```bash
reviewscraper reviews "Din Tai Fung Taipei" --limit 100 --sort newest -o reviews.csv
reviewscraper search "pizza en Madrid" --language es --region ES
```

## Pruebas

```bash
pytest            # contra un Google simulado
pytest -m live    # contra Google de verdad (necesita internet)
```

## Qué falta respecto a Outscraper

- Solo Google Maps. Outscraper también cubre Yelp, Tripadvisor, Trustpilot,
  Google Play, App Store, Amazon y otros. Cada fuente sería un módulo nuevo en
  `reviewscraper/providers/`.
- Todavía no se lee la respuesta del dueño: la reseña de prueba no tenía
  ninguna, así que no se sabe en qué posición viene. Tampoco los "me gusta".
- Los lugares solo traen nombre y feature ID: todavía no se leen la dirección,
  la valoración media ni el teléfono de la respuesta de búsqueda.
- No hay reintentos con rotación de proxies ni panel web.
