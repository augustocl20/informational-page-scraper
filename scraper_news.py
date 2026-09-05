"""
Scraper de noticias basado en feeds RSS.

Lee las fuentes RSS configuradas, guarda las noticias nuevas en Supabase
(evitando duplicados por url) y las publica en un canal de Telegram.
Pensado para correr periódicamente vía GitHub Actions (ver
.github/workflows/scraper.yml), aunque también funciona corriéndolo
manualmente en local.
"""

import html
import os
import re
from datetime import datetime, timezone
from time import mktime, sleep

import feedparser
import requests
from dotenv import load_dotenv
from supabase import Client, create_client

# Carga las variables de entorno desde un archivo .env local
# (ese archivo NUNCA debe subirse a GitHub — agrégalo a tu .gitignore).
load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Fuentes RSS. Agregar una fuente nueva es tan simple como añadir
# una línea aquí (nombre visible -> URL del feed).
FUENTES_RSS = {
    "La República": "https://larepublica.pe/rss/home.xml",
    "ATV": "https://www.atv.pe/rss/",
    "Gestión": "https://gestion.pe/rss/",
    "Canal N": "https://canaln.pe/feed",
    "Willax": "https://willax.pe/feed",
    # "RPP": "https://rpp.pe/rss",
}


def limpiar_resumen(texto_html: str) -> str:
    """
    Deja el resumen en texto plano y legible. Algunos feeds (sobre todo
    los basados en WordPress, como el de ATV) traen el resumen con
    etiquetas HTML crudas (<p>, <a>...) y un texto automático del tipo
    'The post X appeared first on Y.' que WordPress agrega por defecto.
    Ninguna de las dos cosas nos sirve para el mensaje de Telegram.
    """
    if not texto_html:
        return ""

    # Quita el bloque "The post ... appeared first on ...." si existe.
    texto_html = re.sub(
        r"The post .*? appeared first on .*?\.", "", texto_html, flags=re.DOTALL
    )

    # Quita cualquier etiqueta HTML restante (<p>, <a>, <strong>, etc.)
    sin_tags = re.sub(r"<[^>]+>", "", texto_html)

    # Convierte entidades HTML (&amp;, &quot;, etc.) y limpia espacios extra
    limpio = html.unescape(sin_tags)
    return re.sub(r"\s+", " ", limpio).strip()


def extraer_imagen(entrada) -> str | None:
    """
    Busca una imagen para la noticia probando, en orden, los distintos
    formatos que usan los feeds RSS reales (no todos los medios exponen
    la imagen de la misma forma):

    1. media_content — extensión Media RSS que usa La República.
    2. media_thumbnail — variante de Media RSS que usan otros medios.
    3. enclosure de tipo imagen — patrón común en feeds de WordPress.
    4. primera <img> dentro del HTML crudo del resumen — último recurso.

    Devuelve None si no encuentra nada; en ese caso la noticia se envía
    sin foto (mejor eso que fallar el envío completo).
    """
    if hasattr(entrada, "media_content") and entrada.media_content:
        return entrada.media_content[0].get("url")

    if hasattr(entrada, "media_thumbnail") and entrada.media_thumbnail:
        return entrada.media_thumbnail[0].get("url")

    for link in entrada.get("links", []):
        if link.get("rel") == "enclosure" and link.get("type", "").startswith("image"):
            return link.get("href")

    html_crudo = entrada.get("summary", "")
    if not html_crudo and entrada.get("content"):
        html_crudo = entrada["content"][0].get("value", "")

    match = re.search(r'<img[^>]+src="([^"]+)"', html_crudo)
    if match:
        return match.group(1)

    return None


def extraer_imagen_de_pagina(url_articulo: str) -> str | None:
    """
    Último recurso cuando el feed no trae ninguna imagen: visita la
    página del artículo y busca la meta tag og:image, el estándar que
    usan los sitios para la vista previa en redes sociales (Facebook,
    WhatsApp, etc.) — casi todo medio de noticias moderno la tiene.

    Cuesta una petición HTTP extra por noticia sin imagen, pero como
    solo se llama para noticias NUEVAS (no para todo el feed en cada
    corrida), el costo real es bajo.
    """
    try:
        respuesta = requests.get(url_articulo, timeout=10)
        respuesta.raise_for_status()
    except requests.exceptions.RequestException:
        return None

    match = re.search(
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        respuesta.text,
    )
    if match:
        return match.group(1)

    # Algunos sitios escriben los atributos en el orden inverso.
    match = re.search(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
        respuesta.text,
    )
    return match.group(1) if match else None


def obtener_noticias(fuentes: dict) -> list[dict]:
    """
    Lee cada feed RSS y devuelve una lista de noticias normalizadas.

    Si una fuente falla (caída, timeout, feed inválido), se omite y se
    sigue con las demás — una fuente rota no debe tumbar toda la corrida,
    sobre todo corriendo desatendido cada 30 min en GitHub Actions.
    """
    noticias = []
    for nombre_fuente, url in fuentes.items():
        try:
            respuesta = requests.get(url, timeout=10)
            respuesta.raise_for_status()
            feed = feedparser.parse(respuesta.content)
        except requests.exceptions.RequestException as error:
            print(f"⚠️  No se pudo leer {nombre_fuente}, se omite esta corrida: {error}")
            continue

        if feed.bozo:
            print(f"⚠️  Aviso al leer {nombre_fuente}: {feed.bozo_exception}")

        for entrada in feed.entries:
            fecha_iso = None
            if getattr(entrada, "published_parsed", None):
                fecha_iso = datetime.fromtimestamp(
                    mktime(entrada.published_parsed), tz=timezone.utc
                ).isoformat()

            url_noticia = entrada.get("link", "")
            imagen = extraer_imagen(entrada)
            if not imagen and url_noticia:
                imagen = extraer_imagen_de_pagina(url_noticia)

            noticias.append({
                "fuente": nombre_fuente,
                "titulo": entrada.get("title", "").strip(),
                "url": url_noticia,
                "resumen": limpiar_resumen(entrada.get("summary", "")),
                "categoria": entrada.get("category", ""),
                "autor": entrada.get("author", ""),
                "fecha_publicacion": fecha_iso,
                "imagen_url": imagen,
            })
    return noticias


def guardar_noticias(supabase: Client, noticias: list[dict]) -> None:
    if not noticias:
        return

    try:
        supabase.table("noticias").upsert(
            noticias, on_conflict="url", ignore_duplicates=True
        ).execute()
    except Exception as error:
        print(f"❌ Error guardando en Supabase: {error}")


def obtener_pendientes(supabase: Client) -> list[dict]:
    try:
        resultado = (
            supabase.table("noticias")
            .select("*")
            .eq("enviado_telegram", False)
            .order("fecha_publicacion", desc=False)
            .execute()
        )
        return resultado.data
    except Exception as error:
        print(f"❌ Error consultando noticias pendientes: {error}")
        return []


def enviar_a_telegram(token: str, chat_id: str, noticia: dict) -> None:
    titulo = html.escape(noticia["titulo"])
    resumen = html.escape(noticia["resumen"])
    cuerpo = f"{resumen}\n\n<a href=\"{noticia['url']}\">Leer más</a>"

    if noticia.get("imagen_url"):
        respuesta = requests.post(
            f"https://api.telegram.org/bot{token}/sendPhoto",
            json={
                "chat_id": chat_id,
                "photo": noticia["imagen_url"],
                "caption": f"<b>{titulo}</b>",
                "parse_mode": "HTML",
            },
            timeout=10,
        )
        respuesta.raise_for_status()

        sleep(1)

        respuesta = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": cuerpo,
                "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": True},
            },
            timeout=10,
        )
        respuesta.raise_for_status()
    else:
        texto = f"<b>{titulo}</b>\n\n{cuerpo}"
        respuesta = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": texto,
                "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": True},
            },
            timeout=10,
        )
        respuesta.raise_for_status()


def marcar_como_enviada(supabase: Client, id_noticia: int) -> None:
    supabase.table("noticias").update({"enviado_telegram": True}).eq("id", id_noticia).execute()


if __name__ == "__main__":
    faltantes = [
        nombre for nombre, valor in {
            "SUPABASE_URL": SUPABASE_URL,
            "SUPABASE_SERVICE_KEY": SUPABASE_KEY,
            "TELEGRAM_BOT_TOKEN": TELEGRAM_TOKEN,
            "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
        }.items() if not valor
    ]
    if faltantes:
        raise SystemExit(f"Faltan variables en tu .env: {', '.join(faltantes)}")

    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

    noticias = obtener_noticias(FUENTES_RSS)
    print(f"Se encontraron {len(noticias)} noticias en los feeds.")

    guardar_noticias(supabase, noticias)

    pendientes = obtener_pendientes(supabase)
    print(f"Hay {len(pendientes)} noticias pendientes de enviar a Telegram.\n")

    for n in pendientes:
        try:
            enviar_a_telegram(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, n)
        except Exception as error:
            print(f"❌ Error enviando '{n['titulo']}': {error}")
            sleep(2)
            continue

        try:
            marcar_como_enviada(supabase, n["id"])
            print(f"✅ Enviada: {n['titulo']}")
        except Exception as error:
            print(f"⚠️  Se envió '{n['titulo']}' pero no se pudo marcar en Supabase: {error}")

        sleep(2)