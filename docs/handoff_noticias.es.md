# Agente de noticias (`paper_news`): cómo funciona

Escrito en español a propósito: es para el dueño. Diseño completo (en inglés): `docs/design/news_agent.md`.

## Qué hace

- **Cada 5 minutos** lee los titulares nuevos de Alpaca (Benzinga) sobre 40 empresas grandes de EE. UU. Es gratis con tu cuenta de Alpaca y no usa la API de Anthropic: decide con reglas fijas, sin costo por consulta.
- Busca **eventos**: un inversor grande que compra o aumenta su participación (+2), un directivo que compra acciones (+2), analistas que suben la recomendación (+1), resultados o pronósticos mejores de lo esperado (+1), recompras (+1); y los negativos (bajas de recomendación, pronósticos peores, investigaciones de reguladores).
- Si una noticia es fuerte (2 puntos o más, para bien o para mal), **te avisa por Telegram al momento**, en español, con el titular y lo que pasará.
- **Compra** una acción cuando sus noticias de las últimas 5 sesiones suman +2 o más: 10% de la cuenta, máximo 10 acciones, sin apalancamiento. **Vende** si las noticias se vuelven negativas, a las 20 sesiones o si cae 10%.
- No compra si el mercado (SPY) está por debajo de su promedio de 200 días.
- Freno de emergencia: si la cuenta cae 20% desde su máximo, vende todo y se detiene.
- Dinero simulado: 10.000 USD. Nunca envía órdenes a un broker.

## Lo que hay que saber

- Las reglas confirman que la empresa es la afectada y no la que actúa: "JPMorgan baja a Expedia" no cuenta para JPMorgan.
- Alpaca gratis solo guarda unos 7 meses de noticias, así que la prueba con el pasado fue corta (138 sesiones) y **salió mal**: -12,2% contra -2,0% de SPY. Es muy poco tiempo para concluir, pero conviene saberlo. No cambié las reglas para que el pasado saliera bien, porque eso engaña.
- La prueba real es hacia adelante: se juzga a las **60 sesiones** (unos 3 meses) con cuatro criterios: sin freno de emergencia, ganarle a SPY, costos bajo 2% y al menos 10 operaciones completas.
- La SEC (avisos oficiales de compras de más de 5%) queda fuera por ahora: exige un nombre y un correo de contacto en cada consulta. Si quieres sumarla, dime qué contacto usar.

## Cómo pararlo

Crea `KILL_SWITCH_NEWS` en el repo de estado, o pon `enabled: false` en `config.paper_news.yaml`. Para ver su estado: `python -m stride.run_news --report` sobre la copia de `data/paper_news.db`.
