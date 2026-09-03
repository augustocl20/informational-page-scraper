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
            # bozo=True indica que el feed no se parseó del todo limpio
            # (puede ser un detalle menor de formato, no siempre es fatal).
            print(f"⚠️  Aviso al leer {nombre_fuente}: {feed.bozo_exception}")

        for entrada in feed.entries:
            # published_parsed viene como time.struct_time en UTC;
            # lo convertimos a ISO 8601 para que Postgres lo entienda.
            fecha_iso = None
            if getattr(entrada, "published_parsed", None):
                fecha_iso = datetime.fromtimestamp(
                    mktime(entrada.published_parsed), tz=timezone.utc
                ).isoformat()

            noticias.append({
                "fuente": nombre_fuente,
                "titulo": entrada.get("title", "").strip(),
                "url": entrada.get("link", ""),
                "resumen": limpiar_resumen(entrada.get("summary", "")),
                "categoria": entrada.get("category", ""),
                "autor": entrada.get("author", ""),
                "fecha_publicacion": fecha_iso,
                "imagen_url": (
                    entrada.media_content[0]["url"]
                    if hasattr(entrada, "media_content") and entrada.media_content
                    else None
                ),
            })
    return noticias


def guardar_noticias(supabase: Client, noticias: list[dict]) -> None:
    """
    Inserta las noticias nuevas en Supabase. Gracias al 'unique' en la
    columna url, on_conflict + ignore_duplicates hace que las que ya
    existen simplemente se salteen, sin lanzar error ni duplicarse.

    Nota: NO usamos lo que devuelve upsert() para saber qué se insertó
    de verdad — con ignore_duplicates=True esa respuesta no siempre es
    confiable. En vez de eso, obtener_pendientes() pregunta directamente
    a la tabla qué falta enviar.
    """
    if not noticias:
        return

    try:
        supabase.table("noticias").upsert(
            noticias, on_conflict="url", ignore_duplicates=True
        ).execute()
    except Exception as error:
        print(f"❌ Error guardando en Supabase: {error}")


def obtener_pendientes(supabase: Client) -> list[dict]:
    """
    Devuelve las noticias que todavía no se enviaron a Telegram,
    consultando directamente la columna enviado_telegram. Esto es lo
    que realmente decide qué se envía — no importa si vienen de esta
    corrida o de una anterior que se quedó a medias.
    """
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
    """
    Envía una noticia al canal.

    Si hay imagen: primero la foto con el título como caption, y en un
    segundo mensaje el resumen + link. Si no hay imagen, todo va en un
    solo mensaje de texto.
    """
    titulo = html.escape(noticia["titulo"])
    resumen = html.escape(noticia["resumen"])
    cuerpo = f"{resumen}\n\n<a href=\"{noticia['url']}\">Leer más</a>"

    if noticia.get("imagen_url"):
        # Mensaje 1: foto + título como caption
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

        sleep(1)  # pequeño respiro entre los dos mensajes de la misma noticia

        # Mensaje 2: resumen + link
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
        # Sin imagen: todo junto en un solo mensaje
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
            # Ya se envió a Telegram pero no se pudo marcar en Supabase.
            # Riesgo conocido: si esto pasa, esa noticia podría reenviarse
            # en la próxima corrida (mejor eso que perderla silenciosamente).
            print(f"⚠️  Se envió '{n['titulo']}' pero no se pudo marcar en Supabase: {error}")

        sleep(2)  # evita saturar el rate limit de Telegram en el canal