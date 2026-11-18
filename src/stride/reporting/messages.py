"""Plain-language Spanish texts for the Telegram bot (owner request, 2026-11-17).

The owner reads the alerts on the phone and did not understand the technical
English ones ("STRIDE [paper] RUN_FAILED: morning failed: ValueError: ..."). Every
message now says, in Spanish: which account, whether the money is simulated, what
happened in plain words, and what to do. Technical details stay at the end, under
"Detalle técnico", for whoever investigates. Pure: no I/O.
"""

from __future__ import annotations

import math

SIDES = {"buy": "compra", "sell": "venta"}
VETOES = {
    "SHORT": "vendería más de lo que se tiene",
    "ORDER_TOO_LARGE": "orden mayor al límite por orden",
    "TOO_MANY_ORDERS": "se llegó al máximo de órdenes del día",
    "SAME_DAY_REVERSAL": "compra y venta del mismo activo el mismo día",
    "MARGIN_BUFFER": "margen insuficiente",
    "DATA_FAILED": "los datos del día no pasaron la validación",
    "SYMBOL_BLOCKED": "símbolo bloqueado por datos",
    "KILL_SWITCH": "kill switch activo",
    "HALTED": "sistema detenido (halt)",
    "UNKNOWN_SYMBOL": "símbolo desconocido",
    "INVALID_ORDER": "orden inválida",
    "ORDER_TOO_SMALL": "menos de una acción entera al cierre anterior",
    "SEND_FAILED": "el envío falló y el broker no tiene la orden",
    "SEND_UNKNOWN": "el envío falló y no se pudo confirmar con el broker",
}

# What each kind of alert means and what the owner should do about it.
KINDS: dict[str, tuple[str, str]] = {
    "RUN_FAILED": (
        "Falló una tarea del día",
        "Nada por ahora: se reintenta sola en el próximo ciclo (5 minutos). "
        "Si el aviso se repite durante horas, pídele a Claude que lo revise.",
    ),
    "RUN_MISSED": (
        "Una tarea del día no se ejecutó",
        "Pídele a Claude que revise el bucle de GitHub Actions.",
    ),
    "DATA_FAILED": (
        "Aviso sobre los precios del día",
        "Nada: el mensaje dice si el día (o algún activo) quedó sin operar; si no lo dice,"
        " es solo un aviso y la cuenta opera normal. Si se repite varios días, pide una"
        " revisión.",
    ),
    "ORDER_VETOED": (
        "Una orden fue bloqueada por el control de riesgo",
        "Nada: el control de riesgo hizo su trabajo y la orden no se envió.",
    ),
    "RECONCILIATION": (
        "Las posiciones no cuadran con el broker",
        "Revisar: si se repite dos días seguidos, esta cuenta se detiene sola (halt).",
    ),
    "DRAWDOWN": (
        "La cuenta cayó más de 20% desde su máximo",
        "Nada: el sistema reduce el apalancamiento automáticamente.",
    ),
    "KILL_SWITCH": (
        "Freno de emergencia activado",
        "La cuenta vende todo y se detiene. Solo tú decides si se reactiva.",
    ),
    "ORDER_PROBLEM": (
        "Problema al enviar una orden",
        "Nada por ahora: el sistema la reintenta o la revisa al día siguiente.",
    ),
    "CASH_FLOW": (
        "Movimiento de efectivo detectado",
        "Confirma que fue un depósito o retiro tuyo.",
    ),
    "PHASE4_VERDICT": (
        "Veredicto de la prueba en papel",
        "Léelo con calma: es el resultado de los 40 días hábiles.",
    ),
    "INFO": ("Resumen", ""),
    "TEST": ("Mensaje de prueba", "Nada: confirma que el bot funciona."),
}

PHASES = {
    "morning": "la mañana (envío de órdenes)",
    "midday": "el mediodía (revisión de órdenes)",
    "close": "el cierre (cuadre con el broker)",
    "evening": "la tarde (precios del día y cálculo de órdenes)",
}

MODES = {
    "paper": "prueba en papel: dinero simulado",
    "backtest": "simulación histórica",
    "live": "DINERO REAL",
}


def usd(value: float | None) -> str:
    """12345.6 -> "12.345,60 USD" (Spanish separators)."""
    if value is None or not math.isfinite(value):
        return "n/d"
    text = f"{value:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return f"{text} USD"


def pct(value: float | None, signed: bool = True) -> str:
    """-0.0113 -> "-1,13%"."""
    if value is None or not math.isfinite(value):
        return "n/d"
    text = f"{value:+.2%}" if signed else f"{value:.2%}"
    return text.replace(".", ",")


def day_es(iso: object) -> str:
    """ "2026-11-17..." -> "17/11/2026"."""
    text = str(iso)[:10]
    parts = text.split("-")
    return f"{parts[2]}/{parts[1]}/{parts[0]}" if len(parts) == 3 else text  # noqa: PLR2004


def side(value: object) -> str:
    return SIDES.get(str(value), str(value))


def reasons(codes: str) -> str:
    """ "MARGIN_BUFFER+SHORT" -> "margen insuficiente; vendería más de lo que se tiene"."""
    return "; ".join(VETOES.get(c, c) for c in codes.split("+") if c)


def veto(text: str) -> str:
    """ "SPY:buy:MARGIN_BUFFER" -> "compra de SPY. Motivo: margen insuficiente"."""
    symbol, action, codes = [*text.split(":", 2), "", ""][:3]
    return f"{side(action)} de {symbol}. Motivo: {reasons(codes) or 'sin detalle'}"


def phase(name: str) -> str:
    return PHASES.get(name, name)


def failed(phase_name: str, detail: str) -> str:
    """The text of a failed daily task."""
    return f"No se pudo completar {phase(phase_name)}.\nDetalle técnico: {detail}"


def alert_text(kind: str, message: str, mode: str, instance: str) -> str:
    """The whole Telegram message of one alert."""
    title, todo = KINDS.get(kind, (kind, ""))
    head = f"{instance or 'STRIDE'} · {title}"
    lines = [head, f"({MODES.get(mode, mode)})", "", message.strip()]
    if todo:
        lines += ["", f"Qué hacer: {todo}"]
    return "\n".join(lines)
