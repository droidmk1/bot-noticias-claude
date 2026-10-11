"""Bot de noticias de Perú: junta las noticias de la última hora y envía un resumen por Telegram.

Uso:
    python bot.py            # bucle: un resumen cada INTERVALO_MINUTOS
    python bot.py --once     # un solo resumen y termina
    python bot.py --dry-run  # imprime el resumen en consola sin enviarlo
"""

import argparse
import calendar
import html
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic
import feedparser
import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "state.json"
LIMA = timezone(timedelta(hours=-5))

FUENTES = {
    "RPP": "https://rpp.pe/feed",
    "El Comercio": "https://elcomercio.pe/arcio/rss/",
    "Gestión": "https://gestion.pe/arcio/rss/",
    "Infobae Perú": "https://www.infobae.com/arc/outboundfeeds/rss/category/peru/",
    "Andina": "https://andina.pe/agencia/rss.aspx",
    "La República (política)": "https://larepublica.pe/rss/politica.xml",
}

# Secciones de relleno (contenido extranjero tipo "dónde ver", loterías) que no son noticias de Perú
RUTAS_EXCLUIDAS = ("/mix/", "/mag/", "/respuestas/")

MODELO = "claude-opus-5-5"
TELEGRAM_MAX = 4096

log = logging.getLogger("bot-noticias")


# ---------- estado ----------

def cargar_estado() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"enviados": [], "chat_id": None}


def guardar_estado(estado: dict) -> None:
    # Solo guardamos los últimos enlaces para que el archivo no crezca sin límite
    estado["enviados"] = estado["enviados"][-2000:]
    STATE_FILE.write_text(json.dumps(estado, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------- noticias ----------

def obtener_noticias(ventana_min: int, ya_enviados: set) -> list[dict]:
    desde = datetime.now(timezone.utc) - timedelta(minutes=ventana_min)
    noticias = []
    for medio, url in FUENTES.items():
        try:
            resp = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0 bot-noticias-claude"})
            resp.raise_for_status()
        except requests.RequestException as e:
            log.warning("No se pudo leer %s: %s", medio, e)
            continue

        for entrada in feedparser.parse(resp.content).entries:
            fecha_struct = entrada.get("published_parsed") or entrada.get("updated_parsed")
            if not fecha_struct:
                continue
            fecha = datetime.fromtimestamp(calendar.timegm(fecha_struct), tz=timezone.utc)
            enlace = entrada.get("link", "")
            if fecha < desde or enlace in ya_enviados or any(r in enlace for r in RUTAS_EXCLUIDAS):
                continue
            noticias.append({
                "medio": medio,
                "titulo": entrada.get("title", "").strip(),
                "resumen": html.unescape(_sin_html(entrada.get("summary", "")))[:400],
                "enlace": enlace,
                "fecha": fecha,
            })
    noticias.sort(key=lambda n: n["fecha"], reverse=True)
    log.info("%d noticias nuevas en los últimos %d min", len(noticias), ventana_min)
    return noticias


def _sin_html(texto: str) -> str:
    return re.sub(r"<[^>]+>", " ", texto).strip()


# ---------- resumen ----------

PROMPT_SISTEMA = """Eres un editor de noticias peruano. Recibes titulares de medios de Perú publicados en la última hora \
y escribes un resumen breve para leer en Telegram.

Reglas:
- Escribe en español neutro, tono informativo, sin opinión.
- Agrupa por tema (política, economía, sociedad, deportes, internacional, etc.); omite temas sin noticias.
- Une las noticias repetidas entre medios en un solo punto.
- Cada punto: una o dos frases y el enlace a una fuente con <a href="URL">Medio</a>.
- Formato: solo HTML de Telegram (<b>, <i>, <a href>). Nada de Markdown, nada de otras etiquetas.
- Empieza con una línea de 1-2 frases con lo más importante de la hora.
- Máximo unas 2500 caracteres."""


def resumir_con_claude(noticias: list[dict]) -> str:
    lista = "\n".join(
        f"- [{n['medio']}] {n['titulo']} — {n['resumen']} ({n['enlace']})" for n in noticias
    )
    client = anthropic.Anthropic()
    response = client.beta.messages.create(
        model=MODELO,
        max_tokens=16000,
        output_config={"effort": "low"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=PROMPT_SISTEMA,
        messages=[{"role": "user", "content": f"Noticias de la última hora:\n{lista}"}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude rechazó generar el resumen")
    return "".join(b.text for b in response.content if b.type == "text").strip()


def resumen_simple(noticias: list[dict]) -> str:
    """Resumen sin IA: titulares agrupados por medio."""
    partes = []
    por_medio: dict[str, list[dict]] = {}
    for n in noticias:
        por_medio.setdefault(n["medio"], []).append(n)
    for medio, items in por_medio.items():
        partes.append(f"\n<b>{html.escape(medio)}</b>")
        for n in items[:8]:
            partes.append(f'• <a href="{html.escape(n["enlace"], quote=True)}">{html.escape(n["titulo"])}</a>')
    return "\n".join(partes).strip()


def construir_mensaje(noticias: list[dict], ventana_min: int) -> str:
    ahora = datetime.now(LIMA).strftime("%d/%m %H:%M")
    cabecera = f"🇵🇪 <b>Noticias de Perú</b> — última hora ({ahora})\n\n"

    cuerpo = None
    if os.getenv("ANTHROPIC_API_KEY"):
        try:
            cuerpo = resumir_con_claude(noticias)
        except anthropic.APIError as e:
            log.error("Error de la API de Claude, uso resumen simple: %s", e)
        except RuntimeError as e:
            log.error("%s, uso resumen simple", e)
    if not cuerpo:
        cuerpo = resumen_simple(noticias)
    return cabecera + cuerpo


# ---------- telegram ----------

def api_telegram(metodo: str, **params):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    resp = requests.post(f"https://api.telegram.org/bot{token}/{metodo}", json=params, timeout=30)
    datos = resp.json()
    if not datos.get("ok"):
        raise RuntimeError(f"Telegram {metodo}: {datos.get('description')}")
    return datos["result"]


def resolver_chat_id(estado: dict) -> str | None:
    chat_id = os.getenv("TELEGRAM_CHAT_ID") or estado.get("chat_id")
    if chat_id:
        return str(chat_id)
    # Sin chat configurado: tomamos el último chat que escribió al bot
    for update in reversed(api_telegram("getUpdates")):
        mensaje = update.get("message") or update.get("channel_post")
        if mensaje:
            estado["chat_id"] = str(mensaje["chat"]["id"])
            log.info("Chat detectado: %s", estado["chat_id"])
            return estado["chat_id"]
    return None


def partir(texto: str, limite: int = TELEGRAM_MAX) -> list[str]:
    """Divide por líneas para no cortar etiquetas HTML a la mitad."""
    trozos, actual = [], ""
    for linea in texto.split("\n"):
        if len(actual) + len(linea) + 1 > limite and actual:
            trozos.append(actual)
            actual = ""
        actual += linea + "\n"
    if actual.strip():
        trozos.append(actual)
    return trozos


def enviar(chat_id: str, texto: str) -> None:
    for trozo in partir(texto):
        try:
            api_telegram("sendMessage", chat_id=chat_id, text=trozo,
                         parse_mode="HTML", disable_web_page_preview=True)
        except RuntimeError as e:
            # Si el HTML viene mal formado, lo mandamos como texto plano
            log.warning("%s — reenvío sin formato", e)
            api_telegram("sendMessage", chat_id=chat_id, text=_sin_html(trozo),
                         disable_web_page_preview=True)


# ---------- ciclo ----------

def ejecutar_ciclo(ventana_min: int, dry_run: bool) -> None:
    estado = cargar_estado()
    noticias = obtener_noticias(ventana_min, set(estado["enviados"]))
    if not noticias:
        log.info("Sin noticias nuevas; no envío nada")
        return

    mensaje = construir_mensaje(noticias, ventana_min)
    if dry_run:
        print(mensaje)
        return

    chat_id = resolver_chat_id(estado)
    if not chat_id:
        log.error("No hay chat de destino: escríbele /start al bot en Telegram y vuelve a ejecutar")
        return
    enviar(chat_id, mensaje)
    estado["enviados"].extend(n["enlace"] for n in noticias)
    guardar_estado(estado)
    log.info("Resumen enviado (%d noticias)", len(noticias))


def main() -> None:
    load_dotenv(BASE_DIR / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--once", action="store_true", help="ejecutar un solo ciclo")
    parser.add_argument("--dry-run", action="store_true", help="imprimir sin enviar a Telegram")
    args = parser.parse_args()

    ventana = int(os.getenv("VENTANA_MINUTOS", "60"))
    intervalo = int(os.getenv("INTERVALO_MINUTOS", "60"))

    while True:
        try:
            ejecutar_ciclo(ventana, args.dry_run)
        except Exception:
            log.exception("Fallo en el ciclo; reintento en el siguiente")
        if args.once or args.dry_run:
            break
        time.sleep(intervalo * 60)


if __name__ == "__main__":
    main()
