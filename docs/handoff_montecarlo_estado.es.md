# Estado del módulo Monte Carlo (`paper_mc`) a 2026-11-12

Documento para el dueño y para quien analice el proyecto después (por ejemplo Fable): qué se construyó, qué resultó, qué está pendiente de decidir y qué probar para ganar más. El código, los commits y los PR están en inglés; este documento y el prompt de traspaso (`docs/handoff_montecarlo.es.md`) están en español.

## 1. En una página

- Se construyeron las 6 fases pedidas: universo de 20 activos, diseño, datos de 30 minutos, módulo de simulación, evaluación walk-forward y la instancia `paper_mc` con su trabajo horario.
- **La puerta de calibración que el dueño aprobó (sin calibración no hay papel) no se cumplió.** En la validación (2023 a noviembre de 2026) ningún pronosticador supera al modelo "sin dirección" (el nulo) en ningún horizonte; la única ganancia estadísticamente clara fue mínima, a 1 hora y en 2017-2022, y no se repitió. Lo que sí funciona es el modelo de riesgo: el intervalo simulado del 90% contiene el resultado entre el 85% y el 91% de las veces.
- Por eso **la instancia está construida, probada y desactivada** (`enabled: false` en `config.paper_mc.yaml`). Con `false` cada tick del bucle no hace nada y no puede afectar a B ni a DIP. Activarla es una decisión tuya (sección 5).
- Qué se puede hacer con lo aprendido para ganar más: sección 6.

Detalle de los números: `docs/design/mc_backtest_results.md`. Diseño y "tal como se construyó": `docs/design/mc_forecast.md`. Universo: `docs/design/mc_universe.md`.

## 2. Qué hay y dónde

| Pieza | Dónde |
| --- | --- |
| Universo de 20 activos (aprobado 2026-11-11) | `docs/design/mc_universe.md` |
| Diseño del método y de la regla de decisión | `docs/design/mc_forecast.md` |
| Resultados de la evaluación | `docs/design/mc_backtest_results.md` y los datos en `docs/results/mc/` |
| Barras de 30 minutos de Alpaca | `src/stride/data/intraday.py`, `AlpacaSource.fetch_intraday` |
| Módulo de simulación (puro) | `src/stride/forecast/` |
| Cuenta simulada, la misma en backtest y en papel | clase `Trader` en `src/stride/forecast/walkforward.py` |
| Trabajo horario | `src/stride/run_mc.py` |
| Configuración de la instancia | `config.paper_mc.yaml` (cada parámetro comentado) |
| Scripts de investigación | `scripts/research/mc_data.py`, `mc_evaluate.py`, `mc_diagnostics.py` |
| Pruebas | `tests/test_forecast.py`, `test_walkforward.py`, `test_intraday.py`, `test_run_mc.py` |

## 3. Cómo funciona, en pocas líneas

1. Cada hora, cuando termina una "franja" de la sesión (9:30-10:00 y luego una por hora hasta el cierre) y sus barras están publicadas (retraso de 15 minutos del plan gratuito de Alpaca), el trabajo descarga lo nuevo y arma un panel de retornos por franja de los 20 activos.
2. Para cada activo simula 2.000 futuros: toma sus últimos 252 días, quita la volatilidad del momento y la hora del día, y arma trayectorias copiando rachas reales de esa historia (bootstrap por bloques), con una parte de la deriva reciente. De los futuros saca: probabilidad de subir, probabilidad de ganar después de costos, ganancia esperada y riesgo.
3. Regla por activo: entra si la probabilidad de ganar después de costos a 20 días llega al 65% y el horizonte de 1 día no apunta a la baja; sale a efectivo si la probabilidad de ganar baja al 55% o la ganancia esperada se acaba. En medio no hace nada (histéresis). Nunca vende en corto.
4. Pesos dinámicos: un cuarto de Kelly sobre la ganancia esperada y la varianza simuladas, con topes (25% por activo; 40% acciones de EE. UU., 35% metales, 30% cripto), sin apalancamiento, una pérdida simulada a 5 días limitada al 8% y órdenes mínimas de 150 USD.
5. Las órdenes se llenan, de forma simulada, en la apertura de la franja que empieza después de la decisión, con el spread y la comisión de IBKR Pro Tiered (los mismos de B y DIP). Interruptor de seguridad: caída del 20% desde el máximo al cierre de la sesión, vende todo y se detiene para siempre.
6. Todo queda en `data/paper_mc.db` (cuenta, operaciones, equity por franja y una auditoría de los pronósticos), que `save_state.sh` copia al repositorio de estado junto con B y DIP.

## 4. Lo que dicen los números (resumen)

| Pregunta | Respuesta |
| --- | --- |
| ¿El modelo acierta la dirección mejor que no decir nada? | No. Habilidad de Brier contra el nulo en la validación: -0,002 a 5 días (intervalo del 95%: -0,006 a +0,001) y -0,011 a 20 días (-0,022 a -0,0002). A 1 hora hubo +0,011 en 2017-2022 con la deriva completa y +0,001 en 2023-2026 |
| ¿Está bien calibrado el riesgo? | Sí. Cobertura del intervalo del 90%: 0,85 a 0,91 con ventanas de 126 y 252 días |
| ¿Las probabilidades a 5 días son honestas? | Sí donde se usan: dijo 54%, pasó 54%; dijo 61%, pasó 63%. A 20 días son demasiado extremas |
| ¿Las reglas de trading son robustas? | No. En la validación van de -20% a +32% según reglas vecinas; 12 de 24 corridas activaron el interruptor del 20% |
| ¿Qué regla ganó en selección? | `H140_p65_k0.25`: +68% (2017-2022) y +32% en la validación (3,8 años), caída máxima 17,5% |
| ¿Cuánto es solo "estar invertido"? | Comprar y mantener los mismos activos con los mismos costos: +33% (2017-2022) y +54% (validación), caídas 28% y 11% |

Cifras sin promesa: son resultados medidos en datos pasados, no una expectativa.

## 5. Decisiones que quedan en tus manos

1. **¿Activar `paper_mc` de todos modos?** No lo recomiendo con este modelo: la puerta que tú aprobaste no se cumple y la evidencia no muestra ventaja sobre estar invertido. Si lo quieres como experimento de papel (no cuesta dinero; mediría hacia adelante, que es la prueba más limpia), el cambio es una línea: `enabled: true` en `config.paper_mc.yaml`, en un PR a `main`. El siguiente tick del bucle arranca la cuenta con 1.000 USD simulados (los primeros ticks solo descargan historia, unos 10-25 minutos en total). Para pararla a mano: crear un archivo `KILL_SWITCH_MC` en la carpeta del repositorio.
2. **¿Cambiar el método o el objetivo?** Las ideas de la sección 6 usan lo que sí se demostró (el riesgo se modela bien) en lugar de lo que no (la dirección).
3. **Costos de `paper_mc`.** Hoy son los de IBKR como B y DIP (decisión del 2026-11-11). Alpaca no cobra comisión; con 1.000 USD el mínimo de 0,35 USD por orden pesa más que cualquier pronóstico, así que esta decisión mueve más el resultado que mejorar el modelo.

## 6. Qué probar para ganar más (en orden de valor esperado)

Ver el detalle en `docs/design/mc_backtest_results.md`, sección 8. Resumen:

1. **Asignar por riesgo, no por dirección prevista.** El modelo de riesgo está calibrado; una cartera con pesos de volatilidad objetivo o paridad de riesgo con las trayectorias conjuntas no necesita acertar la dirección y opera poco.
2. **Probar con el historial diario desde 2007** (`data/stride.db`, 36 símbolos, incluye 2008 y 2020): para horizontes de 5 y 20 días las barras de 30 minutos no aportan y la muestra de 2016 a hoy es corta y casi toda alcista.
3. **Ranking entre activos** ("cuál de los 20 irá mejor") en lugar de "este activo sube": la prima de las acciones se cancela y es lo que la cartera necesita.
4. **Menos y más grandes posiciones**, o revisar el costo de ejecución.
5. **Dimensionar solo con cantidades calibradas** (bandas de probabilidad y cola de pérdida simulada), no con la ganancia esperada, que es el número menos fiable.
6. **Reversión a 1 día** (la idea del DIP) con el mismo marco; y **guardar la tabla de casos** para ver habilidad por clase de activo, régimen de volatilidad y franja.

## 7. Cómo usarlo

```bash
python scripts/research/mc_data.py         # baja las barras de 30 minutos (unos 15 minutos)
python scripts/research/mc_evaluate.py     # evaluación completa (unos 22 minutos); --smoke para probar en 3
python scripts/research/mc_diagnostics.py  # estadísticas por activo y comprar-y-mantener de referencia
python -m stride.run_mc --config config.paper_mc.yaml --report   # estado de la cuenta
```

Las claves de Alpaca (de papel) se leen de `.env`; el módulo nunca opera dinero real (`mode` solo admite `paper`) y nunca envía órdenes a un bróker: la cuenta es simulada.

## 8. Reglas que se respetaron

- No se tocó ningún módulo protegido (`execution/orders.py`, `risk/guard.py`, `risk/leverage.py`, el código del interruptor, `ibkr_broker.py`) ni ningún valor de `config.yaml`, `config.paper_b.yaml` o `config.paper_dip.yaml`. Las instancias B y DIP no cambian de comportamiento.
- Nunca `mode: live`, nunca `LIVE_CONFIRM`, ninguna clave impresa ni subida.
- Cada fase fue un PR con CI en verde (pytest, ruff, ruff format, mypy) y se fusionó a `main` por indicación del dueño.
- Antigravity (el trabajador) escribió el primer borrador del script de investigación del universo y después se quedó sin cuota (el mensaje decía que se restablecía unas 79 horas después del 11 de noviembre); el resto lo hizo el orquestador y todo diff fue revisado.

## 9. Limitaciones conocidas

Ver la sección 7 de `docs/design/mc_backtest_results.md`. Las principales: una sola década casi toda alcista, 12 reglas probadas (el ganador de 12 está sesgado al alza), spread constante en el modelo frente a spreads reales más altos en activos poco líquidos (UNG, CPER), y datos con 15 minutos de retraso.
