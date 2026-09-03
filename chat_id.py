"""
Script de un solo uso para obtener el chat_id de tu canal de Telegram.

Pasos:
1. Agrega tu bot al canal como ADMINISTRADOR (no basta con miembro),
   con el permiso "Publicar mensajes" habilitado.
2. Publica cualquier mensaje en el canal (ej. "hola").
3. Corre este script. Va a imprimir el chat_id del canal.
4. Guarda ese número en tu .env como TELEGRAM_CHAT_ID (será negativo, empieza con -100 en canales).
"""

import os

import requests
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

if not TOKEN:
    raise SystemExit("Falta TELEGRAM_BOT_TOKEN en tu .env")

respuesta = requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates", timeout=10)
datos = respuesta.json()

if not datos.get("result"):
    print("No se encontraron actualizaciones recientes.")
    print("Asegúrate de: 1) haber agregado el bot como ADMINISTRADOR del canal, 2) haber publicado un mensaje reciente en el canal.")
else:
    vistos = set()
    for update in datos["result"]:
        # Los posts de canal llegan en 'channel_post', no en 'message'
        # (ese es el que se usa en chats/grupos normales).
        chat = update.get("channel_post", update.get("message", {})).get("chat", {})
        if chat.get("id") and chat["id"] not in vistos:
            vistos.add(chat["id"])
            print(f"Canal/Chat: {chat.get('title', chat.get('type'))}  →  chat_id: {chat['id']}")