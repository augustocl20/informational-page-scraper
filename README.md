# Scraper de Noticias → Telegram

![Estado del scraper](https://github.com/augustocl20/informational-page-scraper/actions/workflows/scraper.yml/badge.svg)

Bot que extrae noticias desde feeds RSS de medios peruanos y las publica automáticamente en un canal de Telegram, sin duplicados y sin intervención manual. Corre 24/7 gracias a GitHub Actions.

## Capturas

<!--
Agrega aquí 2-3 capturas reales del bot funcionando. Sugerencia de qué mostrar:
1. El canal de Telegram con un par de noticias publicadas (foto + título, luego resumen + link).
2. La pestaña Actions de GitHub con una corrida exitosa (círculo verde).
3. La tabla `noticias` en Supabase con datos reales.

Cómo insertarlas una vez que las tengas:
![Bot publicando en Telegram](ruta/a/tu/captura1.png)
![Workflow corriendo en GitHub Actions](ruta/a/tu/captura2.png)
-->


## Cómo funciona

1. Lee las fuentes RSS configuradas en `FUENTES_RSS`.
2. Limpia y normaliza cada noticia (quita HTML crudo y texto automático que agregan algunos feeds de WordPress, como "The post... appeared first on...").
3. Guarda las noticias nuevas en Supabase, usando la URL de cada noticia como clave única para evitar duplicados.
4. Publica cada noticia nueva en el canal de Telegram:
   - Si tiene imagen: primero la foto con el título, luego un mensaje con el resumen y el link ("Leer más").
   - Si no tiene imagen: un solo mensaje con todo.
5. Marca la noticia como enviada, para no repetirla en la próxima corrida.

## Stack

- **Python 3.12**
- **feedparser** — lectura y parseo de feeds RSS
- **Supabase (PostgreSQL)** — persistencia y control de duplicados
- **Telegram Bot API** (vía `requests`) — publicación en el canal
- **GitHub Actions** — automatización (corre cada 30 min, sin depender de un servidor propio)

## Fuentes actuales

| Fuente | Feed |
|---|---|
| La República | `https://larepublica.pe/rss/home.xml` (incluye todas las secciones: deportes, economía, espectáculos, etc.) |
| ATV | `https://www.atv.pe/rss/` |
| Gestión | `https://gestion.pe/rss/` |
| Canal N | `https://canaln.pe/feed` |
| Willax | `https://willax.pe/feed` |

Agregar una fuente nueva (si tiene RSS) es tan simple como añadir una línea al diccionario `FUENTES_RSS`.

## Estructura del proyecto

```
├── scraper_news.py               # script principal
├── chat_id.py                    # script auxiliar de un solo uso (obtener el chat_id del canal)
├── crear_tabla_noticias.sql      # script SQL para crear la tabla en Supabase
├── requirements.txt              # dependencias
├── .env.example                  # plantilla de variables de entorno
└── .github/workflows/scraper.yml # automatización con GitHub Actions
```

## Configuración local

```bash
git clone <tu-repo>
cd <tu-repo>
python -m venv venv
source venv/bin/activate  # en Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env  # y completa tus credenciales
python scraper_news.py
```

## Variables de entorno

| Variable | Descripción |
|---|---|
| `SUPABASE_URL` | URL del proyecto de Supabase |
| `SUPABASE_SERVICE_KEY` | Service role key de Supabase (ignora RLS, úsala solo server-side) |
| `TELEGRAM_BOT_TOKEN` | Token del bot, obtenido desde BotFather |
| `TELEGRAM_CHAT_ID` | ID del canal de Telegram (empieza con `-100`) |

## Base de datos

Tabla `noticias` en Supabase, con Row Level Security habilitado (solo accesible mediante la `service_role` key):

- `url` (único) — evita duplicados vía `upsert ... on_conflict`
- `enviado_telegram` — separa "guardado" de "publicado", para no reenviar el historial al conectar una fuente nueva
- `fecha_publicacion`, `categoria`, `autor`, `imagen_url` — metadata de cada noticia

## Automatización

El workflow en `.github/workflows/scraper.yml` corre el script cada 30 minutos usando GitHub Secrets para las credenciales, así el bot funciona 24/7 sin depender de una máquina propia encendida. También puede dispararse manualmente desde la pestaña *Actions* del repositorio.

## Decisiones de diseño

- **HTML en vez de Markdown** para el formato de los mensajes de Telegram: solo hay que escapar `&`, `<` y `>`, evitando errores de parseo con los caracteres que suelen traer los títulos reales (`*`, `_`, `[`, `]`, etc.).
- **Vista previa de link desactivada** (`link_preview_options`) para que el mensaje se vea limpio, sin la tarjeta automática de Telegram.
- **`limpiar_resumen()`** normaliza resúmenes que llegan con HTML crudo o el boilerplate típico de WordPress — útil para cualquier fuente nueva que use ese CMS, no solo ATV.

## Próximos pasos

- [ ] Evaluar más fuentes con RSS disponible
- [ ] Agregar tests básicos
- [ ] Revisar comportamiento cuando un feed falla o está caído
