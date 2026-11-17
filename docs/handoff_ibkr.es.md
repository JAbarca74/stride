# Broker IBKR: qué está hecho y qué te toca a ti

Escrito en español a propósito: es para el dueño. Código y PR en inglés.

## Hecho (2026-11-17, con tu visto bueno y tus respuestas Q1-Q8 aceptadas)

- `src/stride/execution/ibkr_broker.py`: `IBKRBroker`, la única vía a dinero real. Sigue `docs/design/ibkr_broker.md`.
- Seis puertas de seguridad antes de enviar nada: modo paper o live (nunca backtest); live exige `LIVE_CONFIRM`; la cuenta `IBKR_ACCOUNT` debe estar gestionada por el Gateway; paper exige cuenta `DU...` y live una que no lo sea; un puerto live (4001, 7496) en paper se rechaza, y al revés; modo solo lectura para tu primera prueba.
- Órdenes: mercado = market-on-open (`OPG`), límite = orden del día. Cantidad = notional / precio de referencia (cierre anterior para mercado, el límite para límite), redondeada hacia abajo al incremento del contrato: una compra nunca supera su monto. Debajo del mínimo no se envía nada.
- Cada orden lleva tu cuenta y `orderRef = stride-...`: `cancel_all` cancela solo las órdenes de STRIDE, nunca las tuyas manuales. El id es el `permId` de IBKR.
- Nunca reintenta un envío; los errores no muestran cuenta, host ni la orden.
- `config.paper_ibkr.yaml`: la instancia DIP, igual, pero sobre IBKR paper y con su propia base `data/paper_ibkr.db`.
- `run_daily` crea el broker IBKR con `execution.broker: ibkr` y lo desconecta al final de cada fase.
- Las fases diarias siguen **rechazando `mode: live`**: activar live es tu paso, en un cambio aparte que revisas tú (fase 5).
- Tests: Gateway falso, 38 casos (puertas, mapeo de estados, comisiones, órdenes de otra sesión, invariantes con hypothesis). Prueba manual de solo lectura: `tests/test_ibkr_paper_smoke.py`.

## Lo que te toca (nadie más puede)

1. Abrir la cuenta IBKR Pro, verificar identidad. Para paper basta la cuenta paper que IBKR crea con la real.
2. En la configuración de la cuenta: activar acceso por API y acciones fraccionarias.
3. Instalar IB Gateway (y opcionalmente IBC para el login automático) en una máquina siempre encendida. GitHub Actions no sirve para esto: el Gateway necesita estar abierto y con sesión iniciada.
4. Poner en el `.env` de esa máquina:
   ```
   IBKR_HOST=127.0.0.1
   IBKR_PORT=4002
   IBKR_CLIENT_ID=1
   IBKR_ACCOUNT=DU...
   ```
5. Instalar el extra: `pip install -e ".[ibkr]"`.
6. Prueba de solo lectura (no puede enviar órdenes):
   ```
   STRIDE_IBKR_PAPER_SMOKE=1 pytest tests/test_ibkr_paper_smoke.py
   ```
7. Correr la instancia paper en esa máquina (cron de `scripts/install_cron.sh` o similar) con `python -m stride.run_daily --config config.paper_ibkr.yaml --phase auto`. Ojo: la cuenta paper de IBKR suele empezar con 1.000.000 USD; STRIDE dimensiona con el patrimonio que reporta el broker, así que conviene reiniciar la cuenta paper con un saldo parecido al que piensas usar.
8. Q8: al menos 20 días hábiles en IBKR paper con fills, cero quiebres de conciliación y costos dentro de lo estimado antes de hablar de live.
