# Cuenta Kelly paralela (T23): cómo opera

Escrito en español a propósito: es para el dueño. Código y PR en inglés.

## Qué es

La cuenta paralela de la hipótesis H3 (SPEC secciones 1 y 8): una cuenta **simulada** de 10.000 USD que sigue exactamente las mismas señales que la instancia DIP (núcleo SPY más sleeve dip de cripto), pero con margen y con el apalancamiento de la sección 7 (Kelly completo, objetivo de volatilidad, topes). Sirve para medir si Kelly completo crece más que la cuenta real sin disparar el kill switch.

- Configuración: `config.paper_kelly.yaml` (copia de `config.paper_dip.yaml`; `tests/test_paper_kelly.py` comprueba que solo cambian capital, margen, broker y la fuente del apalancamiento).
- Broker: siempre `shadow` (simulado sobre precios guardados). Nunca Alpaca, nunca IBKR, nunca dinero real. No usa claves de broker.
- Base de datos: `data/paper_kelly.db`. Kill switch propio: `KILL_SWITCH_KELLY`; halt propio: `HALT_KELLY`.
- Sin depósitos mensuales: su crecimiento es solo el de la estrategia.

## Margen simulado (SPEC 1.5, pregunta 4)

Solo con `risk.allow_margin: true` (esta cuenta; B y DIP siguen siendo de contado):
- Puede pedir prestado hasta `(L_borrow - 1) x patrimonio`, con `risk.sim_broker_leverage: 2.0` (Reg T).
- El saldo negativo paga DTB3 + 1.5% anual, actual/360, por día calendario.
- Requisito de mantenimiento: 25% del valor de cada posición, por su múltiplo en ETFs apalancados. Si el patrimonio cae por debajo, el simulador vende la posición más grande al cierre (evento `MARGIN_CALL`).
- El guard sigue exigiendo el colchón del 25% de liquidez sobrante: es lo que limita el apalancamiento en la práctica.

Una llamada de margen en esta cuenta simulada cambia posiciones sin una orden registrada, así que aparece como `RECONCILIATION_BREAK` y deja en HALT solo a esta instancia (falla segura). Si pasa, revísala antes de quitar el `HALT_KELLY`.

Una instancia nueva espera a su primera tarde (18:00 de Nueva York): hasta entonces sus fases de mañana, mediodía y cierre quedan como "not started" (PR #119).

## Dónde corre

En el mismo bucle de GitHub Actions (`scripts/actions_loop.sh`), después de B y DIP; un fallo suyo nunca marca el run como fallido. Su base se guarda en `JAbarca74/stride-state` con las demás.

Costo extra: unas 7 peticiones a Tiingo en la hora de las 18:00 de Nueva York (los mismos símbolos que DIP, en su propia base).

## Resultado del backtest (datos pasados, nunca una promesa)

Medido el 2026-11-17 con los precios locales hasta 2026-10-26, 2007-2026, fills simulados:

| | CAGR | Vol | Sharpe | DD máx |
| --- | --- | --- | --- | --- |
| DIP (100 USD + 20/mes) | 13.6% | 12.9% | 0.93 | 28.4% |
| Kelly (10.000 USD, margen) | 18.4% | 22.0% | 0.81 | 31.1% |
| SPY | 10.9% | 19.5% | 0.55 | 55.2% |

Kelly rota mucho más (16.7x al año, costos 1.17% al año) porque su apalancamiento se recalcula cada día; 546 órdenes fueron vetadas por el colchón de margen del guard; no hubo ninguna llamada de margen ni kill switch.

## Cómo pararla

`enabled` no existe en `run_daily`: para pararla, crea `KILL_SWITCH_KELLY` en el repo de estado o quita su línea de `scripts/actions_loop.sh`. Para verla: `python -m stride.reporting.daily --verdict --config config.paper_kelly.yaml` sobre la copia del repo de estado.
