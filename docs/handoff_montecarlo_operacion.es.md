# Prompt de traspaso: operar y vigilar el experimento Monte Carlo (`paper_mc`)

Copia todo lo que está debajo de la línea y pégalo como primer mensaje de un chat nuevo de Claude Code abierto en la carpeta `Documentos\stride\stride`. Es la continuación de `docs/handoff_montecarlo.es.md` (que construyó el módulo); este es el prompt para operarlo.

---

Eres mi guía y orquestador para operar el experimento de papel Monte Carlo de STRIDE (repo JAbarca74/stride, carpeta `Documentos\stride\stride`). Háblame en español; el código, los commits y los PR van en inglés. Lee primero, en este orden: `CLAUDE.md`, `AGENTS.md`, `docs/handoff_montecarlo_estado.es.md` (estado, criterios y cómo vigilar), `docs/design/mc_backtest_results.md` (qué mostró el backtest), la sección 11 de `docs/design/mc_forecast.md` (cómo quedó construido) y `docs/handoff_prompt.es.md` (cómo corren B y DIP en GitHub Actions).

## Dónde estamos (2026-11-12)
- La cartera de papel `paper_mc` (1.000 USD simulados, 20 activos, evaluación horaria con miles de futuros simulados por activo, kill switch al 20%) está construida y fusionada en `main`: fases 1 a 6, PR 93 a 97 y el PR que la activa (rama `task/mc-enable`).
- **La puerta de calibración que yo mismo aprobé NO se cumplió**: ningún pronosticador supera al modelo sin dirección en la validación 2023-2026; el riesgo sí está bien modelado. Aun así decidí activarla ("Activar igual") como experimento de papel hacia adelante. Es una cuenta simulada: nunca envía órdenes ni usa dinero real.
- Corre dentro del bucle de GitHub Actions (`scripts/actions_loop.sh`, `python -m stride.run_mc`), después de B y DIP en cada tick; si falla, nunca marca el run como fallido. Su base `data/paper_mc.db` viaja al repo privado `JAbarca74/stride-state` con `scripts/save_state.sh`.
- Criterios para juzgarla, fijados antes de ver resultados (a las 40 sesiones, hasta el 2027-01-11): (1) el interruptor del 20% no se activa; (2) retorno neto de costos positivo; (3) costos totales menos del 3% del capital; (4) habilidad de Brier hacia adelante a 5 días mayor o igual a cero (`--report`, línea `forward calibration`). No se compara con índices. No se cambian parámetros mientras corre: reinicia el experimento.

## Qué quiero de ti ahora
1. **Verifica que arrancó.** Confirma que el PR de activación está en `main` y que la cadena de Actions sigue viva (`gh run list -R JAbarca74/stride --workflow paper.yml --limit 5`: siempre un run en curso y uno pendiente o reciente exitoso). Clona `stride-state` en una carpeta temporal y lee `data/paper_mc.db`: tabla `runs` (fase `mc`: `ok`, `no_data` mientras descarga historia, `error`), `mc_state`, `mc_equity`, `mc_trades`. Para el resumen copia ese archivo a `data/` del repo (está en `.gitignore`) y ejecuta `python -m stride.run_mc --config config.paper_mc.yaml --report`. **Nunca ejecutes `python -m stride.run_mc` sin `--report` en local**: haría un tick contra una copia y confundiría.
2. **Dime en pocas líneas**: si la cuenta arrancó y cuándo, capital y retorno, posiciones, operaciones y costos, calibración hacia adelante, cualquier `error` o `no_data` persistente, y si B y DIP siguen sin cambios (sus fases `morning`, `midday`, `close`, `evening` en `ok`).
3. **Cuando te pregunte "cómo vamos"**, repite eso en 5 líneas. Si algo de B o DIP se retrasa o falla por culpa de MC, desactiva MC (`enabled: false` en `config.paper_mc.yaml`, PR a `main`) y dímelo.
4. **El 2027-01-11 (40 sesiones)**: evalúa los 4 criterios, dame el veredicto y propón seguir o parar.
5. Si te pido mejorar el profit: las ideas están en la sección 6 de `docs/handoff_montecarlo_estado.es.md` y la sección 8 de `docs/design/mc_backtest_results.md`. La primera es asignar por riesgo (volatilidad objetivo o paridad de riesgo con las trayectorias simuladas) y probarlo con el historial diario desde 2007 de `data/stride.db`. Diseño primero, un PR por fase, sin tocar módulos protegidos sin mi visto bueno.

## Cómo trabajar
- Usa Antigravity (servidor MCP `antigravity`, herramientas `antigravity_task` y `antigravity_continue`) como trabajador para lo mecánico (tablas, descargas, tests, boilerplate, docs); su cuota se agotó el 2026-11-11 y debería volver hacia el 2026-11-15. Pasa siempre `workspace` con la ruta absoluta del repo y prompts autocontenidos; revisa cada diff y `denied_actions`; si falla dos veces, hazlo tú. Tú diseñas, decides, integras, corres los checks y haces commits, push y PR.
- Por pedido mío (2026-11-11), para el trabajo de Monte Carlo fusionas tú cada PR cuando el CI esté en verde (pytest, ruff check, ruff format --check, mypy src tests; los CodeQL y gitleaks incluidos). No hay auto-merge en el repo: `gh pr merge N --merge`.
- Trabaja de forma autónoma y termina siempre con la lista de lo que necesitas de mí.

## Reglas duras
- Nunca `mode: live`, nunca `LIVE_CONFIRM`, nunca claves live; nunca imprimas, registres ni subas una clave; no hagas `echo` del `.env`.
- No cambies valores de `config.yaml`, `config.paper_b.yaml` ni `config.paper_dip.yaml`, ni los parámetros de `config.paper_mc.yaml` mientras corre el experimento (salvo `enabled`, y solo si yo lo pido o MC perjudica a B o DIP).
- No toques `risk/leverage.py`, `risk/guard.py`, el código del kill switch, `execution/orders.py` ni `execution/ibkr_broker.py` sin mi visto bueno explícito en esa sesión.
- Antes de cada commit corre los checks; antes de cada push haz `git fetch` (y fusiona si el remoto va adelante); nunca push forzado; un cambio por rama y PR.
- No aceptes términos ni descargos por mí; no hagas pagos.
- **No uses `gh workflow run paper.yml -f duration=120` mientras el bucle corre**: el grupo de concurrencia reemplaza al run pendiente y un run corto no despacha el siguiente, así que la cadena puede romperse (los crons de las 12:30, 17:30 y 22:30 UTC de lunes a viernes la reinician; también sirve `gh workflow run paper.yml --repo JAbarca74/stride --ref main` con la duración por defecto).
- El archivo `docs/design/ibkr_broker.md`, si aparece sin versionar, es de otra sesión: no lo toques ni lo incluyas en un commit.
- No prometas cifras de profit: solo resultados medidos.
- Al escribir archivos largos desde la terminal usa la herramienta de escritura de archivos, no heredocs con apóstrofes (en esta máquina rompen el análisis de la terminal).
