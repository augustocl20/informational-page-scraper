"""
Scraper de noticias basado en feeds RSS.

Etapa 2: lee las fuentes RSS y guarda las noticias nuevas en Supabase
(evitando duplicados por url). Todavía falta conectar el envío a
Telegram — eso viene en el siguiente paso.
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
load_dotenv()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

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
    texto_html = re.sub(
        r"The post .*? appeared first on .*?\.", "", texto_html, flags=re.DOTALL
    )

        # Quita cualquier etiqueta HTML restante (<p>, <a>, <strong>, etc.)
    sin_tags = re.sub(r"<[^>]+>", "", texto_html)

    # Convierte entidades HTML (&amp;, &quot;, etc.) y limpia espacios extra
    limpio = html.unescape(sin_tags)
    return re.sub(r"\s+", " ", limpio).strip()


def obtener_noticias(fuentes: dict) -> list[dict]:
    """Lee cada feed RSS y devuelve una lista de noticias normalizadas."""
    noticias = []
    for nombre_fuente, url in fuentes.items():
        feed = feedparser.parse(url)

        if feed.bozo:
            print(f"⚠️  Aviso al leer {nombre_fuente}: {feed.bozo_exception}")

        for entrada in feed.entries:
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


def guardar_noticias(supabase: Client, noticias: list[dict]) -> list[dict]:
    """
    Inserta las noticias nuevas en Supabase. Gracias al 'unique' en la
    columna url, on_conflict + ignore_duplicates hace que las que ya
    existen simplemente se salteen, sin lanzar error ni duplicarse.

    Devuelve solo las noticias que SÍ eran nuevas (para poder
    enviarlas a Telegram en el siguiente paso).
    """
    if not noticias:
        return []

    resultado = (
        supabase.table("noticias")
        .upsert(noticias, on_conflict="url", ignore_duplicates=True)
        .execute()
    )
    return resultado.data


def enviar_a_telegram(token: str, chat_id: str, noticia: dict) -> None:
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

        sleep(1)

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

    noticias_nuevas = guardar_noticias(supabase, noticias)
    print(f"Se guardaron {len(noticias_nuevas)} noticias nuevas en Supabase.\n")

    for n in noticias_nuevas:
        try:
            enviar_a_telegram(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, n)
            marcar_como_enviada(supabase, n["id"])
            print(f"✅ Enviada: {n['titulo']}")
        except Exception as error:
            print(f"❌ Error enviando '{n['titulo']}': {error}")
        sleep(2)  # evita saturar el rate limit de Telegram en el grupo