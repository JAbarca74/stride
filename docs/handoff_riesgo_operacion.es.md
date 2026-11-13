# Instancia de papel RISK (`paper_risk`): cómo operarla y vigilarla

Notas para el dueño y para quien retome el proyecto en un chat nuevo. El código, los commits y los PR están en inglés; este documento está en español. Diseño: `docs/design/mc_risk_paper.md`. De dónde sale la regla: `docs/design/mc_risk_results.md`.

## 1. Estado

- La instancia está **activada desde el 2026-11-13** (`enabled: true` en `config.paper_risk.yaml`), por decisión del dueño. Arranca con 10.000 USD simulados, todo en efectivo, en el primer tick del bucle que corra con este código después de las 18:00 de Nueva York de un día hábil: ese tick baja el historial y decide, y las primeras compras se registran en la sesión siguiente.
- Está conectada al bucle de GitHub Actions (`scripts/actions_loop.sh`), después de B, DIP y MC, y su base se guarda con las demás (`scripts/save_state.sh`). Un fallo suyo nunca marca el run como fallido.
- Con `enabled: false` cada tick es un no-op: no baja datos y no envía alertas; la base y las operaciones quedan.

Es una cuenta simulada. Nunca envía órdenes a un bróker ni usa dinero real (`mode` solo admite `paper`).

## 2. Qué hace, en pocas líneas

1. Una vez por sesión, cuando la barra diaria ya está publicada (después de las 18:00 de Nueva York), baja los precios diarios ajustados de los 20 activos desde Tiingo.
2. El primer día hábil de cada mes recalcula los pesos: inverso de la volatilidad simulada a 20 sesiones, con tope de 25% por activo y los topes por grupo. Un activo solo se mantiene si su cierre está sobre su media de 200 días; lo que no se mantiene queda en efectivo.
3. Entre un rebalanceo y el siguiente solo puede reducir (por el tope de pérdida simulada del 8% a 5 sesiones).
4. Una orden decidida al cierre se llena en la apertura de la sesión siguiente, con el spread y la comisión de IBKR Pro Tiered de `config.yaml`. Por eso las compras aparecen un día después de la decisión.
5. Interruptor: una caída del 20% desde el máximo, al cierre, vende todo en la apertura siguiente y detiene la instancia para siempre.

Capital simulado: 10.000 USD. No es el tamaño de la cuenta real; con 1.000 USD la selección fijada de antemano dio un mal resultado.

## 3. Qué esperar (y qué no)

- Opera poco: una tanda de órdenes al mes y, a veces, alguna reducción.
- En el backtest hizo 3,4% y 5,2% anual en los dos periodos, con años entre -2,7% y +13,5% y una caída máxima de 9,3%. Son resultados medidos en datos pasados con ejecuciones simuladas, no una expectativa.
- En un mercado que sube rinde menos que estar invertido: una parte de la cartera suele estar en efectivo.
- Unos meses de papel no confirman ni refutan la regla. Sirven para comprobar que la instancia hace lo mismo que el backtest y que se mantiene dentro de sus límites de riesgo.

## 4. Cómo se juzga

A las 120 sesiones desde el arranque (unos seis meses). Sigue solo si se cumplen los cuatro criterios, fijados antes de ver resultados:

1. El interruptor del 20% no se activó.
2. La caída desde el máximo nunca pasó de 12%.
3. Los costos acumulados son menores al 0,5% del capital inicial (50 USD).
4. La comprobación `--verify` pasa: repetir la cuenta desde su primera sesión da el mismo capital que lo registrado.

El signo del retorno no es un criterio: en seis meses es ruido para esta regla. Si algún criterio falla, se desactiva (`enabled: false`). No se cambian parámetros mientras corre: cambiarlos reinicia el experimento.

## 5. Cómo vigilarla

- **Telegram.** Un resumen (`INFO`, con la etiqueta `RISK:`) después de cada sesión procesada: capital, efectivo, porcentaje invertido, caída actual y peor caída, costos, factor de freno y pesos. Alertas `KILL_SWITCH`, `DATA_FAILED` y `RUN_FAILED` si algo falla.
- **Estado a mano.** Clonar `stride-state` en una carpeta temporal, copiar `data/paper_risk.db` a `data/` y ejecutar:

```bash
python -m stride.run_risk --config config.paper_risk.yaml --report
```

```bash
python -m stride.run_risk --config config.paper_risk.yaml --verify
```

- **Runs de GitHub Actions.** El paso de RISK nunca marca el run como fallido, así que un fallo se ve en Telegram y en la tabla `runs` de la base (fase `risk`, estado `error` o `no_data`).

Mensajes normales del job: `up to date` (nada nuevo que procesar), `waiting to retry` (encontró que la barra aún no estaba o falló, y espera 30 minutos antes de volver a pedir datos), `no data: the bar of ... is not published yet`.

## 6. Qué guarda `data/paper_risk.db`

| Tabla | Contenido |
| --- | --- |
| `prices` | barras diarias ajustadas de los 20 activos, desde unos 500 días antes del arranque |
| `risk_equity` | por sesión: capital, efectivo, exposición y caída desde el máximo |
| `risk_trades` | cada operación simulada: día, activo, lado, monto, precio, comisión, spread y razón (`RISK_ENTER`, `RISK_EXIT`, `RISK_REDUCE`, `RISK_RESIZE`, `KILL_SWITCH`, `KILL_SWITCH_FILE`) |
| `risk_log` | por sesión: si hubo rebalanceo, factores de freno, pérdida simulada (5% y 1%) de lo que se mantiene y sus pesos |
| `risk_paper_state` | la cuenta guardada (JSON), la última sesión procesada, cuándo arrancó |
| `runs`, `events` | una fila por tick que hizo algo (fase `risk`) y un evento por operación o ajuste |

## 7. Dividendos

Los precios son ajustados. Cuando un fondo paga un dividendo, el proveedor reescala todo su historial. El job lo detecta comparando el último cierre que usó con el que hay ahora en la base para ese mismo día, y multiplica las acciones del libro por el cociente: equivale a reinvertir el dividendo sin costo. Queda un evento `DIVIDEND_ADJUST`. Después de un dividendo `--verify` no da una diferencia de cero exacto sino de centavos (pasa si es menor a 0,1%).

## 8. Cómo pararla

De menos a más drástico:

1. `enabled: false` en `config.paper_risk.yaml` y PR a `main`. La base y las operaciones quedan.
2. Crear un archivo `KILL_SWITCH_RISK` en la carpeta del repositorio: vende todo en la apertura siguiente y no vuelve a operar. Solo afecta a esta instancia.
3. Un archivo `KILL_SWITCH` para todas las instancias, o desactivar el workflow completo en la pestaña Actions (esto para también a B, DIP y MC).

## 9. Lo que comparte con B, DIP y MC

- **La clave de datos de Tiingo.** RISK hace una consulta de sondeo y, solo si la barra del día ya está, las otras 19. Si no está o algo falla, espera 30 minutos (`retry_minutes`) antes de volver a pedir. El primer arranque baja el historial completo de los 20 activos una sola vez.
- **El bucle y el guardado de estado.** Corre al final de cada tick, después de las otras tres, y no puede hacer fallar el run.
- No comparte base, configuración, cuenta ni archivos de interruptor propios con ninguna.

## 10. Reglas que se respetaron

- No se tocó ningún módulo protegido ni ningún valor de `config.yaml`, `config.paper_b.yaml`, `config.paper_dip.yaml` o `config.paper_mc.yaml`.
- `paper_mc` sigue su experimento sin cambios.
- Nunca `mode: live`, nunca `LIVE_CONFIRM`, ninguna clave impresa ni subida.
