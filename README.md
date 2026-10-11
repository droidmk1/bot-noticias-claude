# bot-noticias-claude

Bot que junta las noticias de Perú de la última hora y envía un resumen por Telegram (`@noticias_claude_bot`), en bucle.

## Cómo funciona

1. Lee los RSS de RPP, El Comercio, Gestión, Infobae Perú, Andina y La República (política).
2. Se queda con lo publicado en los últimos `VENTANA_MINUTOS` que no se haya enviado antes (`state.json`).
3. Si hay `ANTHROPIC_API_KEY`, Claude redacta un resumen agrupado por tema; si no, envía los titulares agrupados por medio.
4. Lo manda al chat de Telegram y espera `INTERVALO_MINUTOS` para repetir.

## Puesta en marcha (Windows)

```bash
py -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
cp .env.example .env   # y rellena TELEGRAM_BOT_TOKEN
```

Escríbele `/start` al bot en Telegram (así detecta a qué chat enviar) y luego:

```bash
.venv/Scripts/python bot.py --dry-run   # ver el resumen en consola
.venv/Scripts/python bot.py --once      # enviar un resumen
.venv/Scripts/python bot.py             # bucle continuo
```

## Ramas

- `main`: producción.
- `dev`: desarrollo; se fusiona en `main` cuando está probado.

`.env` y `state.json` no se suben al repo.
