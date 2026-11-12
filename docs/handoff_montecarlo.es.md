# Prompt de traspaso: módulo Monte Carlo (cartera MC) con Antigravity como trabajador

Copia todo lo que está debajo de la línea y pégalo como primer mensaje de un chat nuevo de Claude Code abierto en la carpeta `Documentos\stride\stride`.

> Estado a 2026-11-12: las fases 1 a 6 están hechas; ver `docs/handoff_montecarlo_estado.es.md` (qué existe, resultado, criterios) y `docs/handoff_montecarlo_operacion.es.md` (prompt para operar el experimento en un chat nuevo) y `docs/design/mc_backtest_results.md`.

---

Eres el orquestador y revisor de un proyecto nuevo dentro de STRIDE (repo JAbarca74/stride). Háblame en español; el código, los commits y los PR van en inglés. Lee primero `CLAUDE.md`, `AGENTS.md`, `docs/SPEC.md`, `docs/handoff_prompt.es.md` (cómo corre la prueba paper) y este archivo.

## Tu rol y el de Antigravity
- Tú (Claude Code) orquestas, diseñas, decides y revisas. Antigravity (servidor MCP `antigravity`, herramientas `antigravity_task` y `antigravity_continue`) es el trabajador.
- Delega a Antigravity el trabajo mecánico: código de descarga de datos, boilerplate, tests, configs, docs, refactors pequeños, ediciones repetitivas. Pasa siempre `workspace` = ruta absoluta del repo y un prompt autocontenido (objetivo, archivos, restricciones, salida esperada); el trabajador no ve este chat.
- Quédate tú con: la matemática del Monte Carlo y la regla de decisión, el diseño de riesgo, la integración con `run_daily.py`, los commits, los push, los PR y la revisión final.
- Nunca confíes solo en el `status` de Antigravity: revisa `denied_actions`, `warning` y el diff de cada archivo en `files_created/modified/deleted`, corre los checks, y corrige tú o devuélvele correcciones. Si responde `NEEDS_ANSWER`, contesta tú cuando lo puedas decidir con este documento y el código; pregúntame solo lo que es mío. Si Antigravity falla dos veces, hazlo tú.

## Qué quiero (decisiones ya tomadas por mí)
1. Una tercera cartera de papel, nueva e independiente: `config.paper_mc.yaml`, base `data/paper_mc.db`. No toques B (`config.paper_b.yaml`) ni DIP (`config.paper_dip.yaml`): llevan su prueba de 40 días desde el 2026-11-09.
2. Capital ficticio inicial: 1.000 USD. Kill switch de esta instancia: `kill_switch_dd: 0.20` (solo el valor en su config; el código del kill switch no se toca).
3. No nos comparamos con ningún índice: el objetivo es el mayor profit posible con riesgo controlado por el kill switch.
4. Piensa varias veces al día: evalúa cada hora en horario de mercado (más la evaluación nocturna). Puede ejecutar todas las veces que quiera mientras la señal sea clarísima, con histéresis (entra con una probabilidad alta, sale con una más baja) y solo si la ganancia esperada supera comisión más spread.
5. Módulo nuevo `src/stride/forecast/` (puro: sin red, sin reloj, sin archivos): con la historia guardada (ventanas de 3, 6 o 12 meses, a elegir con backtest) más el precio en vivo, simula miles de caminos futuros por activo (bootstrap por bloques u otro método justificado) a varios horizontes (1 hora, 1 día, 5 días). No predice un futuro: mide hacia dónde se concentran todos. Si se concentran al alza, compra; a la baja, vende y se queda en efectivo; si están repartidos, no hace nada. Cada activo se decide por separado.
6. Pesos dinámicos: el % de cada activo no es fijo, sale de los resultados de la simulación (por ejemplo, proporcional a la ganancia esperada sobre el riesgo simulado, con un tope por activo). Nada de pesos fijos.
7. Universo lo más diversificado posible: investiga y propón los activos (ver abajo).

## Lo que ya se sabe (verificado el 2026-11-11)
- `data/stride.db` (local, no versionada) tiene historia diaria de 36 símbolos: SPY, QQQ, IWM, EFA, EEM, VNQ, XLE, XLF, XLI, XLK, XLV, IEF, TLT, BWX, GLD, SLV, DBC, DBA, USO, CPER, UUP, FXY, IBIT, ETHA, BTC-USD, ETH-USD y apalancados (SSO, UPRO, QLD, TQQQ, UBT, TMF, UGL, AGQ, BITX, ETHU). La mayoría desde 2007.
- No hay datos horarios. `src/stride/data/sources.py` tiene `AlpacaSource` (barras diarias, feed SIP, plan Basic con 15 minutos de retraso). Para barras horarias hay que extenderla con `timeframe=1Hour`. Las claves paper `ALPACA_API_KEY`/`ALPACA_SECRET_KEY` están en `.env` y en los secretos de GitHub (nunca las imprimas).
- Costos: el modelo es IBKR Pro Tiered (`backtest/costs.py`), mínimo 0,35 USD por orden. Con el filtro de costo de 0,5% (`max_resize_cost_pct`) una orden necesita unos 89 USD como mínimo. Con 1.000 USD eso limita a unas 8-10 posiciones a la vez: el diseño debe respetarlo (descartar pesos que den órdenes pequeñas o justificar otro umbral).
- La prueba paper corre en GitHub Actions (`.github/workflows/paper.yml`): runs encadenados día y noche, tick cada 5 minutos con `scripts/actions_loop.sh`, que hoy ejecuta B y DIP. El estado vive en el repo privado `JAbarca74/stride-state`. Una instancia nueva se agrega a ese bucle.
- `run_daily.py` tiene fases diarias (morning 9:15-15:30, midday, close, evening). Habrá que añadir una fase horaria para esta instancia sin cambiar el comportamiento de B ni DIP.

## Decisiones abiertas que debes plantearme antes de construir esa parte
- Ejecución intradía en papel: el shadow broker actual llena al open del día siguiente con barras diarias, así que no sirve para operar cada hora. Opciones: (a) una segunda cuenta paper de Alpaca solo para MC (yo creo la cuenta y te doy los nombres de las variables; tú me das el comando para cargar los secretos), o (b) extender el shadow broker para llenar con barras horarias. Recomiéndame una.
- Señal bajista: efectivo (por defecto) o también ETFs inversos (SH, PSQ, etc.).
- Cualquier cambio en `execution/orders.py`, `risk/guard.py`, `risk/leverage.py` o el kill switch necesita mi visto bueno explícito en ese chat, con el diseño primero.

## Plan por fases (un PR por fase, CI en verde, yo hago el merge salvo que te diga lo contrario)
1. Investigación del universo: candidatos por clase (acciones EE. UU. e internacionales, sectores, bonos de varios plazos, oro, plata, materias primas, inmobiliario, dólar y divisas, cripto vía ETF, opcionalmente inversos). Para cada uno: historia disponible, liquidez y spread, si Alpaca lo permite fraccionado, y correlaciones entre ellos con `data/stride.db`. Entrega: `docs/design/mc_universe.md` con la lista propuesta (unos 12-20 activos) y por qué. Delega a Antigravity la recolección y las tablas; tú decides la lista.
2. Diseño: `docs/design/mc_forecast.md` con el método de simulación, horizontes, regla de decisión (umbrales de entrada y salida, histéresis), pesos dinámicos, costos, límites de riesgo y cómo se mide la calibración. Me lo explicas en pocas líneas antes de pasar a código.
3. Datos horarios: barras de 1 hora de Alpaca para el universo, guardadas en SQLite con tests (Antigravity escribe; tú revisas).
4. Módulo `forecast/` puro con tests (incluidas propiedades con hypothesis donde muevan dinero).
5. Calibración y backtest walk-forward sin mirar el futuro: cuando el modelo dice 80%, ¿pasa cerca del 80%? Si no está calibrado, no avanzamos a papel. Reporta retorno, caída máxima, número de operaciones y costos, sin comparar contra índices.
6. Instancia `paper_mc`: config, fase horaria en `run_daily.py`, agregarla a `scripts/actions_loop.sh`, y verificación con un run corto (`gh workflow run paper.yml -f duration=120`).

## Reglas duras (igual que siempre)
- Nunca `mode: live`, nunca `LIVE_CONFIRM`, nunca claves live, nunca imprimas ni subas una clave.
- No cambies valores de `config.yaml`, `config.paper_b.yaml` ni `config.paper_dip.yaml`.
- Antes de cada commit: pytest, ruff check, ruff format --check, mypy src tests. Antes de cada push: `git fetch` y merge si el remoto va adelante. Nunca push forzado.
- No aceptes términos ni descargos de ningún sitio por mí. No hagas pagos.
- No prometas cifras de profit: solo resultados medidos en backtest y en papel.

## Qué hacer ahora
Empieza por la fase 1: delega la investigación a Antigravity, revisa lo que entregue y dame la lista propuesta del universo con una tabla corta. Termina siempre con la lista de lo que necesitas de mí.
