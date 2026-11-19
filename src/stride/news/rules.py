"""Headline -> events, by fixed rules (docs/design/news_agent.md section 3). Pure.

A headline counts for a stock only when the stock is the subject of the action:
its name sits right after the verb ("Upgrades Apple to Buy", "Stake in Tesla") or
starts the clause ("Tesla Receives Analyst Downgrade"). Benzinga tags the acting
company too ("JPMorgan Chase Downgrades Expedia" carries JPM), and such headlines
must not move JPM. One event per stock per headline: the first rule that matches,
in the order of the design table.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from stride.news.config import NewsConfig

GAP = r"[^|]{0,90}?"  # a short stretch of the same headline
# Results against expectations need an earnings word, so "Stake Surpasses 10%" or
# "Surpasses One Million Robots" are not beats.
EARNINGS = r"\b(?:Q[1-4]|quarter(?:ly)?|earnings|EPS|revenue|sales|results)\b"
EXPECTED = r"\b(?:estimates?|expectations|consensus|forecasts?)\b"
BEATS = r"\b(?:beats|tops|exceeds|surpasses)\b"
MISSES = r"\b(?:misses|falls\s+short\s+of|lags)\b"
# Guidance is the company's own: "Bank of America Lowers ... Forecast for Apple" is a
# bank's forecast about another company.
OWN = r"(?!\s+(?:for|on)\b)"
# Regulators: the company must face them ("Report on ... Fraud Risk" is not a probe).
FACES = r"\b(?:faces|under|charged|accused|hit\s+with|subject\s+of)\b"
AUTHORITY = (
    r"(?:\bSEC\b|\bDOJ\b|Department\s+of\s+Justice|\bFBI\b|\bfraud\b|\bprobes?\b"
    r"|\binvestigation\b)"
)

_SELL_RATING = re.compile(r"\bto\s+(?:strong\s+)?(?:sell|underperform|underweight|reduce)\b", re.I)
_TARGET_MOVE = re.compile(r"to\s+\$([\d,]+(?:\.\d+)?)\s+from\s+\$([\d,]+(?:\.\d+)?)", re.I)


@dataclass(frozen=True)
class Event:
    symbol: str
    kind: str
    points: float


def _name_pattern(symbol: str, names: list[str]) -> str:
    """Any name of the stock as a whole word; the ticker too when it has 2+ letters
    (matched in capitals only, so "MA" never matches the word "ma")."""
    alternatives = [re.escape(n) for n in sorted(names, key=len, reverse=True)]
    words = "|".join(alternatives)
    named = rf"(?i:\b(?:{words})(?![\w&]))" if words else ""
    # Tickers count in capitals only: "COST" must not match "cost", nor "CAT" "cat".
    ticker = rf"(?-i:\b{re.escape(symbol)}\b)" if len(symbol) >= 2 else ""  # noqa: PLR2004
    return "(?:" + "|".join(p for p in (named, ticker) if p) + ")"


def rx(text: str) -> re.Pattern[str]:
    return re.compile(text, re.I)


def _rules(n: str) -> list[tuple[str, re.Pattern[str]]]:
    """The design table, in order, for one stock's name pattern `n`."""
    return [
        (
            "INSIDER_BUY",
            rx(rf"{n}{GAP}\binsiders?\s+(?:buys|bought|purchases|purchased|acquires)\b"),
        ),
        (
            "STAKE_UP",
            rx(
                r"\b(?:increases|raises|boosts|builds|takes|acquires|initiates|discloses|adds|ups|lifts)"
                rf"\b{GAP}\bstakes?\s+in\s+{n}"
            ),
        ),
        ("UPGRADE", rx(rf"\bupgrades?\s+{n}|^{GAP}{n}{GAP}\breceives?\b{GAP}\bupgrades?\b")),
        (
            "GUIDANCE_UP",
            rx(
                rf"{n}{GAP}\b(?:raises|boosts|lifts|hikes)\b{GAP}\b(?:guidance|outlook|forecast)\b{OWN}"
            ),
        ),
        ("BEAT", rx(rf"{n}{GAP}(?:{EARNINGS}{GAP}{BEATS}|{BEATS}{GAP}{EXPECTED})")),
        ("BUYBACK", rx(rf"{n}{GAP}\b(?:buyback|repurchase)")),
        (
            "TARGET",
            rx(
                rf"\b(?:raises|lifts|boosts|ups|lowers|cuts|reduces|trims|adjusts)\s+(?:the\s+)?{n}(?:'s)?\s+price\s+target"
                rf"|price\s+target\s+(?:on|for)\s+{n}"
            ),
        ),
        ("DOWNGRADE", rx(rf"\bdowngrades?\s+{n}|^{GAP}{n}{GAP}\breceives?\b{GAP}\bdowngrades?\b")),
        (
            "STAKE_DOWN",
            rx(rf"\b(?:reduces|cuts|trims|sells|exits|lowers|dumps)\b{GAP}\bstakes?\s+in\s+{n}"),
        ),
        ("MISS", rx(rf"{n}{GAP}(?:{EARNINGS}{GAP}{MISSES}|{MISSES}{GAP}{EXPECTED})")),
        (
            "GUIDANCE_DOWN",
            rx(
                rf"{n}{GAP}\b(?:lowers|cuts|slashes|withdraws|reduces)\b{GAP}"
                rf"\b(?:guidance|outlook|forecast)\b{OWN}"
            ),
        ),
        (
            "REGULATOR",
            rx(
                rf"{n}{GAP}{FACES}{GAP}{AUTHORITY}|{n}{GAP}\bdelays\b[^|]{{0,40}}\bfiling"
                rf"|{n}{GAP}accounting\s+irregularit"
            ),
        ),
    ]


def _target_kind(headline: str) -> str | None:
    """Direction of a price-target headline: by its verb, else by `to $X from $Y`."""
    lower = headline.lower()
    if re.search(r"\b(?:raises|lifts|boosts|ups)\b", lower):
        return "TARGET_UP"
    if re.search(r"\b(?:lowers|cuts|reduces|trims)\b", lower):
        return "TARGET_DOWN"
    move = _TARGET_MOVE.search(headline)
    if move:
        new, old = (float(x.replace(",", "")) for x in move.groups())
        if new > old:
            return "TARGET_UP"
        if new < old:
            return "TARGET_DOWN"
    return None  # "adjusts", "maintains" with no direction: no event


class Classifier:
    """Compiled rules for a whole universe."""

    def __init__(self, universe: Mapping[str, list[str]], points: Mapping[str, float]) -> None:
        self._points = dict(points)
        self._rules = {s: _rules(_name_pattern(s, names)) for s, names in universe.items()}

    @classmethod
    def from_config(cls, config: NewsConfig) -> Classifier:
        return cls(config.universe, config.points)

    def classify(self, headline: str) -> list[Event]:
        """Every event the headline carries, at most one per stock, sorted by symbol."""
        text = " ".join(headline.split())
        events = []
        for symbol in sorted(self._rules):
            kind = self._first(symbol, text)
            if kind is not None:
                events.append(Event(symbol, kind, self._points[kind]))
        return events

    def _first(self, symbol: str, text: str) -> str | None:
        for kind, pattern in self._rules[symbol]:
            if not pattern.search(text):
                continue
            if kind == "TARGET":
                direction = _target_kind(text)
                if direction is None:
                    continue
                return direction
            if kind == "DOWNGRADE" and _SELL_RATING.search(text):
                return "DOWNGRADE_SELL"
            return kind
        return None
