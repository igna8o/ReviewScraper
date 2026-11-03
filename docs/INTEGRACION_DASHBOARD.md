# Integrar ReviewScraper en el dashboard de reseñas

Guía para conectar este servicio a un dashboard que ya existe en otro proyecto.
Está escrita para que la siga una persona o para pasársela a Claude en el
repositorio del dashboard ("implementa esto siguiendo `INTEGRACION_DASHBOARD.md`").

## Objetivo

El cliente conecta su negocio **sin hacer trabajo**:

1. **Al registrarse (opción A, sin login):** escribe el nombre de su negocio,
   lo elige de una lista y ve sus reseñas de Google al instante.
2. **Después (opción B, oficial):** si quiere **responder reseñas** desde el
   dashboard, pulsa "Conectar con Google". A partir de ahí sus reseñas llegan por
   la API oficial de Google Business Profile y el scraper deja de usarse para él.

## Qué está probado y qué no (3‑nov‑2026)

| Pieza | Estado |
|---|---|
| Leer reseñas de Google por feature ID o por CID (`0x0:0x<cid>`) | **Probado en vivo** |
| Enlaces `maps.google.com/?cid=...` como entrada | **Probado en vivo** |
| Autocompletado y detalles con la API oficial de Places | Probado solo con simulación; falta probarlo con una clave real |
| Búsqueda por nombre con scraping (`/maps/search-v3`) | **No usar.** Desde servidores en la nube Google responde con CAPTCHA |
| Google Business Profile API (opción B) | No implementado aquí; se hace en el dashboard (sección 5) |

## Arquitectura

```
┌────────────── Dashboard (tu proyecto) ──────────────┐
│ Frontend: buscador de negocio, lista de reseñas,    │
│           botón "Conectar con Google"               │
│ Backend:  tablas businesses / reviews, cron de sync,│
│           OAuth de Google, respuestas a reseñas     │
└───────┬───────────────────────────────┬─────────────┘
        │ HTTP + X-API-KEY              │ OAuth (opción B)
        ▼                               ▼
┌─ ReviewScraper (este repo) ─┐   ┌─ Google Business Profile API ─┐
│ /places/autocomplete        │   │ cuentas, ubicaciones, reseñas │
│ /places/details             │   │ y respuestas                  │
│ /maps/reviews-v3            │   └───────────────────────────────┘
│ /requests/{id}              │
└──────┬───────────────┬──────┘
       ▼               ▼
  Places API      Reseñas de Google
  (oficial)       (scraping)
```

ReviewScraper corre como **servicio aparte** (Docker). El dashboard le habla
por HTTP, así que da igual en qué lenguaje esté hecho el dashboard. **El
frontend nunca llama a ReviewScraper directamente**, siempre a través del
backend del dashboard, para no exponer la `X-API-KEY`.

---

## 1. Desplegar ReviewScraper

```bash
docker build -t reviewscraper .
docker run -d --name reviewscraper -p 8000:8000 -v reviewscraper-data:/data \
  -e REVIEWSCRAPER_API_KEYS=<clave-larga-aleatoria> \
  -e GOOGLE_PLACES_API_KEY=<clave de Google Cloud> \
  reviewscraper
```

- `GOOGLE_PLACES_API_KEY`: en Google Cloud Console, crea un proyecto, activa
  **Places API (New)** y crea una clave restringida a esa API. Tiene cuota
  gratuita mensual; consulta los precios actuales antes de lanzar.
- Ponlo en la misma red privada que el backend del dashboard. No hace falta
  exponerlo a internet.
- Comprobación: `curl http://<host>:8000/health` → `{"status":"ok"}`.

En el backend del dashboard guarda dos variables: `REVIEWSCRAPER_URL` y
`REVIEWSCRAPER_API_KEY`.

## 2. Modelo de datos (en el dashboard)

Adáptalo a tu ORM o base de datos.

**`businesses`**

| Columna | Tipo | Notas |
|---|---|---|
| `id` | pk | |
| `client_id` | fk | Dueño en tu sistema |
| `name`, `address` | text | De `/places/details` |
| `place_id` | text, único | `ChIJ...` (oficial) |
| `google_id` | text | `0x0:0x...`, lo que usa el scraper |
| `rating`, `reviews_count` | num | De Places; se actualizan en cada sync |
| `source` | enum | `scraper` o `gbp` (cuando conecta Google) |
| `gbp_account`, `gbp_location` | text, null | Solo en la opción B |
| `last_review_at` | timestamp, null | La reseña más nueva guardada |
| `last_sync_at`, `sync_error` | timestamp, text | Para monitorizar |

**`reviews`**

| Columna | Tipo | Notas |
|---|---|---|
| `id` | pk | |
| `business_id` | fk | |
| `source` | enum | `scraper` o `gbp` |
| `external_id` | text | `review_id` del scraper o `reviewId` de GBP |
| `author_name`, `author_image`, `author_link` | text | |
| `rating` | int 1–5 | |
| `text` | text | |
| `language` | text | |
| `photos` | json | `review_img_urls` |
| `published_at` | timestamp | De `review_timestamp` (segundos Unix) |
| `reply_text`, `reply_at` | text, timestamp | Solo con GBP |
| `raw` | json | Respuesta original, por si hace falta reprocesar |

Índice único en **`(business_id, source, external_id)`**. Así cada sync puede
hacer *upsert* sin duplicar reseñas.

## 3. Onboarding sin fricción (opción A)

### 3.1 Buscador en el frontend

Un campo de texto con sugerencias:

1. Cuando el cliente abre el buscador, genera un `sessionToken` (un UUID).
2. Mientras escribe (con *debounce* de 250–300 ms y un mínimo de 3 letras),
   el frontend llama a tu backend y tu backend llama a:

   ```
   GET {REVIEWSCRAPER_URL}/places/autocomplete?input=café%20central&language=es&region=ES&sessionToken=<uuid>
   X-API-KEY: <clave>
   ```

   Respuesta:

   ```json
   {"data": [{"place_id": "ChIJ...", "name": "Café Central",
              "address": "Plaza del Ángel, Madrid", "description": "..."}]}
   ```

3. Muestra `name` y `address` en la lista.

`region` es opcional. Si sabes el país del cliente, pásalo para que las
sugerencias sean más precisas.

### 3.2 El cliente elige su negocio

El backend llama a:

```
GET {REVIEWSCRAPER_URL}/places/details?placeId=ChIJ...&language=es&sessionToken=<el mismo uuid>
```

```json
{"data": {"name": "Café Central", "place_id": "ChIJ...", "cid": "1234...",
          "google_id": "0x0:0x...", "full_address": "...", "rating": 4.5, "reviews": 812}}
```

Usa el **mismo `sessionToken`** que en el autocompletado. Así Google cobra la
búsqueda completa como una sola sesión. Guarda el negocio en `businesses`.

### 3.3 Primera carga de reseñas

Haz dos peticiones:

1. **Para que vea algo al instante:** las 20 más recientes, en síncrono (tarda
   unos segundos).

   ```
   GET /maps/reviews-v3?query=<google_id>&reviewsLimit=20&sort=newest&async=false&language=es
   ```

2. **El historial completo, en segundo plano:**

   ```
   GET /maps/reviews-v3?query=<google_id>&reviewsLimit=0&sort=newest&language=es&webhook=https://<tu-backend>/webhooks/reviewscraper
   ```

   Responde al momento con `{"id": "...", "status": "Pending"}`. Cuando
   termina, ReviewScraper hace un `POST` a tu webhook con
   `{"id", "status", "results_location", "quantity"}`. Tu backend pide entonces
   `GET /requests/{id}` (con la `X-API-KEY`) y guarda las reseñas. Los resultados
   se borran 2 horas después de terminar.

   Si no puedes recibir webhooks, consulta `GET /requests/{id}` cada pocos
   segundos hasta que `status` deje de ser `Pending`.

Usa `query=<google_id>` y no el `place_id`. Así te ahorras una llamada de pago a
Places en cada sincronización.

Un negocio con miles de reseñas tarda minutos: son 10 reseñas por página, con
1 a 3 segundos de espera entre páginas. En la interfaz, muestra "Importando
reseñas…" mientras tanto.

### 3.4 Conversión de campos

Cada elemento de `data` es un lugar con `reviews_data`. Por cada reseña:

| ReviewScraper | `reviews` |
|---|---|
| `review_id` | `external_id` |
| `author_title` | `author_name` |
| `author_image` | `author_image` |
| `author_link` | `author_link` |
| `review_rating` | `rating` |
| `review_text` | `text` (puede venir vacío: hay reseñas solo con estrellas) |
| `review_language` | `language` |
| `review_img_urls` | `photos` |
| `review_timestamp` | `published_at` (segundos Unix, UTC) |

## 4. Sincronización incremental

Un cron (por ejemplo cada 6 horas) recorre los negocios con `source = scraper`:

```
GET /maps/reviews-v3?query=<google_id>&reviewsLimit=0&sort=newest&cutoff=<last_review_at - 86400>&async=false
```

- `cutoff` hace que se detenga al llegar a reseñas más antiguas. Normalmente es
  **una sola petición a Google** por negocio. Restar un día cubre reseñas
  editadas y relojes desfasados; el índice único evita duplicados.
- Haz *upsert* de las reseñas y actualiza `last_review_at` y `last_sync_at`.
- Escalona los negocios a lo largo de la ventana; no los lances todos a la vez.
  El servidor ya limita la concurrencia (`REVIEWSCRAPER_CONCURRENCY`, 4 por
  defecto).
- Para refrescar `rating` y `reviews_count`, llama a `/places/details` una vez
  al día como mucho, porque cada llamada se cobra.

### Errores que hay que vigilar

| Señal | Qué significa | Qué hacer |
|---|---|---|
| `errorMessage` contiene `FormatChanged` | Google cambió el formato de su respuesta | Alerta interna. Hay que actualizar el parser; los datos ya guardados no se pierden |
| `HTTP 429` o `HTTP 403` | Google limita la IP del servidor | Reintenta más tarde con espera creciente. Si se repite, configura `REVIEWSCRAPER_PROXY` |
| `Places API HTTP 4xx` | Clave mal configurada o cuota agotada | Revisa la clave y la cuota en Google Cloud |

Guarda el error en `businesses.sync_error`. Al cliente muéstrale un mensaje
genérico como "No pudimos actualizar tus reseñas; lo reintentaremos".

## 5. "Conectar con Google" (opción B, oficial)

Esta parte vive **en el backend del dashboard**, no en ReviewScraper, porque
maneja los tokens de cada cliente.

> Los nombres de endpoints y campos de esta sección son de la documentación de
> Google Business Profile que conozco. **Compruébalos en la documentación
> oficial antes de implementar**; Google renombra y mueve estas APIs a menudo.

### 5.1 Requisitos (una sola vez, los haces tú)

1. En el proyecto de Google Cloud, configura la **pantalla de consentimiento
   OAuth** y crea credenciales OAuth de tipo "aplicación web" con la URL de
   retorno de tu backend.
2. **Pide acceso a las APIs de Business Profile** con el formulario de Google.
   Hasta que lo aprueban, la cuota es 0. Puede tardar días o semanas: pídelo
   cuanto antes.
3. Activa: My Business Account Management API, My Business Business
   Information API y la API de reseñas de Business Profile (v4).
4. Scope OAuth: `https://www.googleapis.com/auth/business.manage`.
   Probablemente Google exigirá verificar tu app antes de que la usen clientes
   reales.

### 5.2 Flujo para el cliente

1. Pulsa "Conectar con Google" y se le redirige a Google con
   `access_type=offline` y `prompt=consent` para obtener un *refresh token*.
2. Inicia sesión con la cuenta que **administra la ficha** del negocio.
3. Tu backend recibe el código, lo cambia por tokens y **guarda el refresh
   token cifrado**.
4. Lista cuentas y ubicaciones:
   - `GET https://mybusinessaccountmanagement.googleapis.com/v1/accounts`
   - `GET https://mybusinessbusinessinformation.googleapis.com/v1/{account}/locations?readMask=name,title,storefrontAddress,metadata`
5. **Empareja** la ubicación con el negocio que ya tenías, comparando
   `metadata.placeId` con `businesses.place_id`. Si coincide, no le preguntes
   nada al cliente. Si tiene varias ubicaciones, que elija.
6. Guarda `gbp_account` y `gbp_location` y cambia `source` a `gbp`.

### 5.3 Reseñas y respuestas por la vía oficial

- Leer: `GET https://mybusiness.googleapis.com/v4/{account}/{location}/reviews`
  (paginado). Campos: `reviewId`, `reviewer.displayName`,
  `reviewer.profilePhotoUrl`, `starRating` (`ONE`…`FIVE`), `comment`,
  `createTime`, `updateTime`, `reviewReply`.
- Responder: `PUT .../reviews/{reviewId}/reply` con `{"comment": "..."}`.
  Borrar la respuesta: `DELETE` en la misma ruta.
- Avisos de reseñas nuevas (opcional): Business Profile puede publicar eventos
  `NEW_REVIEW` y `UPDATED_REVIEW` en Google Cloud Pub/Sub, lo que evita el cron.

### 5.4 Pasar del scraper a la vía oficial

Los IDs del scraper y los de la API oficial **no coinciden**. Al conectar:

1. Importa todas las reseñas desde GBP con `source = gbp`.
2. Empareja cada reseña del scraper con una de GBP por nombre del autor,
   estrellas y fecha (a menos de 1 día de diferencia). Copia lo que quieras
   conservar, como etiquetas o notas internas, y borra o archiva las del scraper.
3. Desde ese momento el cron usa GBP para ese negocio. El scraper solo vuelve a
   usarse si el cliente desconecta Google o si el token deja de funcionar.

Si el refresh token caduca o se revoca, vuelve a `source = scraper` y muestra
"Vuelve a conectar Google para responder reseñas".

## 6. Lista de tareas para el proyecto del dashboard

- [ ] Desplegar ReviewScraper (sección 1) y guardar `REVIEWSCRAPER_URL` y `REVIEWSCRAPER_API_KEY`
- [ ] Crear `businesses` y `reviews` con el índice único (sección 2)
- [ ] Endpoints del backend que llaman a `/places/autocomplete` y `/places/details`
- [ ] Componente de buscador con *debounce* y `sessionToken`
- [ ] Primera carga: 20 en síncrono y el resto en asíncrono con webhook (sección 3.3)
- [ ] Endpoint `/webhooks/reviewscraper` que pide `/requests/{id}` y guarda las reseñas
- [ ] Cron de sincronización incremental con `cutoff` (sección 4)
- [ ] Registro de errores y alerta cuando aparezca `FormatChanged`
- [ ] Pedir a Google el acceso a la API de Business Profile (sección 5.1). **Hazlo ya; tarda**
- [ ] OAuth "Conectar con Google", emparejado por `placeId` y responder reseñas (5.2–5.3)
- [ ] Paso de `scraper` a `gbp` (5.4)

## 7. Aviso legal

Leer reseñas con el scraper va contra los términos de servicio de Google. Por
eso la opción B es la vía principal para los clientes que quieren responder, y
el scraper conviene usarlo con poco volumen: una petición por negocio en cada
sincronización. La API de Places y la de Business Profile son oficiales. Esto
no es asesoramiento legal.
