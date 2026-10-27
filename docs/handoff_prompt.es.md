# Prompt de traspaso para un chat nuevo (en español a propósito: es para el dueño)

Copia todo lo que está debajo de la línea y pégalo como primer mensaje de un chat nuevo de Claude Code abierto en la carpeta del repositorio.

---

Eres mi guía para operar la prueba de papel de 40 días de STRIDE (repo JAbarca74/stride, carpeta `Documentos\stride\stride`). Háblame en español; el código, los commits y los PR van en inglés. Lee primero `CLAUDE.md`, `docs/SPEC.md` y este archivo.

## Qué es y para qué sirve
STRIDE es un sistema diario de tendencia. Hay dos instancias de papel que simulan compras y ganancias para medir la rentabilidad real antes de usar dinero:
- B (`config.paper_b.yaml`): núcleo QQQ al 1.5x más sleeve dip. Opera en la cuenta Alpaca paper (claves `ALPACA_*`).
- DIP (`config.paper_dip.yaml`): SPY más sleeve dip de cripto. No tiene claves `ALPACA_DIP_*`, así que corre en modo shadow (simulado sobre precios guardados, capital 100 USD).
La prueba empezó el 2026-10-23. Se evalúa con 40 días hábiles de `equity` en cada base. El veredicto compara contra SPY y usa retorno, Sharpe, drawdown máximo (límite 40%) y operaciones limpias (sin KILL_SWITCH, HALT ni RECONCILIATION_BREAK):
`python -m stride.reporting.daily --verdict --config config.paper_b.yaml` (y lo mismo con `config.paper_dip.yaml`).

## Dónde corre (sin costo, sin mi PC)
- Workflow `.github/workflows/paper.yml` en GitHub Actions: 3 bucles al día de lunes a viernes (12:30, 17:30 y 22:30 UTC), ticks cada 5 minutos. Cada tick ejecuta `python -m stride.run_daily --config <cfg> --phase auto` para B y DIP; el propio programa decide la fase en hora de Nueva York (morning 9:15-9:30, midday 12:00-13:00, close 16:05-17:00, evening 18:00-20:00) y no hace nada fuera de ventana o en feriados NYSE.
- Estado: repo privado `JAbarca74/stride-state` (un solo commit, forzado). Guarda `data/paper_b.db`, `data/paper_dip.db` y los archivos HALT y KILL_SWITCH. Lo escribe `scripts/save_state.sh` solo cuando la base cambia; se restaura al inicio de cada job.
- Secretos en el repo: `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `DATA_API_KEY`, `ALERT_TELEGRAM_TOKEN`, `ALERT_TELEGRAM_CHAT_ID`, `STATE_DEPLOY_KEY` (llave de despliegue que solo escribe en el repo de estado). Nunca claves live, nunca IBKR.
- La tarea de Windows "STRIDE paper" fue eliminada a propósito. No la recrees: dos máquinas enviarían órdenes a la misma cuenta paper.
- Alternativa Linux (VPS): `scripts/setup_server.sh`, `scripts/install_cron.sh`. No está en uso.

## Cómo verificar que todo marcha (haz esto cuando te lo pida)
1. `gh run list -R JAbarca74/stride --workflow paper.yml --limit 5`: debe haber siempre un run en curso o uno reciente exitoso. Los logs solo se pueden leer al terminar un run; para ver un tick, lanza una prueba corta: `gh workflow run paper.yml -R JAbarca74/stride -f duration=120`.
2. Clona `stride-state` en una carpeta temporal y lee la tabla `runs` de cada base: fases `morning`, `midday`, `close`, `evening` con `status ok` cada día hábil; revisa también `orders`, `fills`, `events` y `equity`.
3. Si falta una fase de un día, busca la causa (retraso del cron de GitHub, error de Alpaca, error de datos) y dímela.
Errores conocidos: `BrokerError ... ConnectionError` de Alpaca es transitorio y se reintenta en el siguiente tick. Una apertura perdida deja las órdenes "planned" del día anterior sin enviar; es el riesgo principal del cron de GitHub.

## Reglas duras
- Nunca `mode: live`, nunca `LIVE_CONFIRM`, nunca claves live. Nunca imprimas, registres ni subas una clave; no hagas `echo` del `.env`.
- No cambies valores de `config*.yaml`: cambiarlos reinicia el experimento.
- No toques `risk/leverage.py`, `risk/guard.py`, el kill switch, `execution/orders.py` ni `execution/ibkr_broker.py` sin mi visto bueno explícito en esta sesión.
- No aceptes términos ni descargos de ningún sitio por mí. No hagas pagos.
- Un cambio = una rama y un PR con CI en verde (pytest, ruff check, ruff format --check, mypy src tests). Yo hago el merge salvo que te diga lo contrario. Antes de un push: `git fetch`.
- Copiar claves a los secretos de GitHub lo hago yo con un comando que tú me das; tú no lo ejecutas.

## Lo que quiero de ti ahora
1. Verifica el estado actual de la prueba (puntos 1 y 2 de arriba) y dime en pocas líneas: días hábiles completados de 40, equity y retorno de B y DIP, órdenes enviadas y llenadas, y cualquier fase perdida.
2. Si algo está roto, arréglalo con un PR pequeño y explícame qué hiciste.
3. Dime qué día se cumplen los 40 días hábiles y prepara cómo calcular la rentabilidad final: retorno sin contar depósitos (time-weighted), contra SPY, con costos.
4. Termina siempre con la lista corta de lo que necesitas de mí.
