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
            # Algunos medios reactualizan notas viejas: si la URL trae fecha (/2026/08/21/), manda esa
            fecha_url = re.search(r"/(20\d\d)/(\d\d)/(\d\d)/", enlace)
            if fecha_url and datetime(*map(int, fecha_url.groups()), tzinfo=LIMA) < desde - timedelta(days=1):
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


# ---------- top de la hora ----------

STOPWORDS = set("""a al ante con contra de del desde el en entre es la las lo los para por que se sin
su sus tras un una uno y o e ni como mas más pero sobre este esta estos estas ese esa fue son ser hoy
tras vivo en-vivo dónde donde qué cuándo cómo así sus perú peru peruano peruanos lima""".split())

# Secciones con más peso: noticias nacionales de interés general
SECCIONES_PRIORITARIAS = ("/politica", "/economia", "/peru", "/lima", "/judiciales", "/actualidad", "/sociedad")


def _palabras(titulo: str) -> set[str]:
    return {w for w in re.findall(r"\w+", titulo.lower()) if len(w) > 3 and w not in STOPWORDS}


def seleccionar_top(noticias: list[dict], n: int) -> list[dict]:
    """Agrupa titulares que hablan de lo mismo y prioriza lo que cubren más medios."""
    grupos: list[list[dict]] = []
    for noticia in noticias:
        p = _palabras(noticia["titulo"])
        for grupo in grupos:
            q = set().union(*(_palabras(x["titulo"]) for x in grupo))
            comunes = len(p & q)
            # Misma noticia si comparten buena parte de las palabras o al menos 3 clave
            if comunes >= 3 or (p and q and comunes / len(p | q) >= 0.3):
                grupo.append(noticia)
                break
        else:
            grupos.append([noticia])

    def puntuar(grupo: list[dict]) -> float:
        medios = {x["medio"] for x in grupo}
        principal = grupo[0]
        puntos = 10 * len(medios)
        if any(s in principal["enlace"] for s in SECCIONES_PRIORITARIAS):
            puntos += 3
        # Desempate por lo más reciente
        return puntos + principal["fecha"].timestamp() / 1e10

    grupos.sort(key=puntuar, reverse=True)
    top, por_medio = [], {}
    for grupo in grupos:
        principal = grupo[0]
        # Máximo 2 por medio para que no domine uno solo
        if por_medio.get(principal["medio"], 0) >= 2:
            continue
        por_medio[principal["medio"]] = por_medio.get(principal["medio"], 0) + 1
        principal["otros_medios"] = sorted({x["medio"] for x in grupo} - {principal["medio"]})
        top.append(principal)
        if len(top) == n:
            break
    return top


PROMPT_SISTEMA = """Eres un editor de noticias peruano. Recibes titulares de medios de Perú publicados en la última hora.
Elige las {n} noticias más importantes para un lector en Perú (prioriza política, economía, seguridad y sociedad \
nacionales; las repetidas en varios medios suelen ser las más relevantes) y escribe una lista numerada.

Reglas:
- Español neutro, tono informativo, sin opinión.
- Cada punto: el número, un titular propio en <b>negrita</b>, una frase de contexto y el enlace \
<a href="URL">Medio</a> a una de las fuentes.
- Formato: solo HTML de Telegram (<b>, <i>, <a href>). Nada de Markdown, nada de otras etiquetas.
- Solo la lista, sin introducción ni cierre."""


def top_con_claude(noticias: list[dict], n: int) -> str:
    lista = "\n".join(
        f"- [{x['medio']}] {x['titulo']} — {x['resumen']} ({x['enlace']})" for x in noticias
    )
    client = anthropic.Anthropic()
    response = client.beta.messages.create(
        model=MODELO,
        max_tokens=16000,
        output_config={"effort": "low"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=PROMPT_SISTEMA.format(n=n),
        messages=[{"role": "user", "content": f"Noticias de la última hora:\n{lista}"}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude rechazó generar el resumen")
    return "".join(b.text for b in response.content if b.type == "text").strip()


def top_simple(noticias: list[dict], n: int) -> str:
    """Top sin IA: titulares más repetidos entre medios."""
    lineas = []
    for i, x in enumerate(seleccionar_top(noticias, n), 1):
        enlace = html.escape(x["enlace"], quote=True)
        extra = f" (+{len(x['otros_medios'])} medios)" if x["otros_medios"] else ""
        lineas.append(f'{i}. <b>{html.escape(x["titulo"])}</b>\n    <a href="{enlace}">{html.escape(x["medio"])}</a>{extra}')
    return "\n\n".join(lineas)


def construir_mensaje(noticias: list[dict], n: int) -> str:
    ahora = datetime.now(LIMA).strftime("%d/%m %H:%M")
    cabecera = f"🇵🇪 <b>Top {n} de Perú</b> — última hora ({ahora})\n\n"

    cuerpo = None
    if os.getenv("ANTHROPIC_API_KEY"):
        try:
            cuerpo = top_con_claude(noticias, n)
        except anthropic.APIError as e:
            log.error("Error de la API de Claude, uso top simple: %s", e)
        except RuntimeError as e:
            log.error("%s, uso top simple", e)
    if not cuerpo:
        cuerpo = top_simple(noticias, n)
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

    mensaje = construir_mensaje(noticias, int(os.getenv("TOP_N", "5")))
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
