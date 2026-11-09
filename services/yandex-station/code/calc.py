"""Instant arithmetic for processor turns.

Ported from the fin bot calculator pattern (ExpenseSyncBot parser.ts):
tokenize -> two-phase precedence (× ÷ before + −) -> decimal-exact evaluate,
never eval(). Russian numerals and word operators are supported
(«сколько будет двадцать пять умножить на четыре»). Returns the answer as a
string, or None when the text is not pure arithmetic.
"""
from __future__ import annotations

import re
from decimal import Decimal, DivisionByZero, InvalidOperation

NUMERALS: dict[str, int] = {
    "ноль": 0, "один": 1, "одну": 1, "два": 2, "две": 2, "три": 3, "четыре": 4,
    "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10,
    "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14,
    "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17, "восемнадцать": 18,
    "девятнадцать": 19, "двадцать": 20, "тридцать": 30, "сорок": 40,
    "пятьдесят": 50, "шестьдесят": 60, "семьдесят": 70, "восемьдесят": 80,
    "девяносто": 90, "сто": 100, "двести": 200, "триста": 300, "четыреста": 400,
    "пятьсот": 500, "шестьсот": 600, "семьсот": 700, "восемьсот": 800,
    "девятьсот": 900, "тысяча": 1000, "тысячи": 1000, "тысяч": 1000,
}

# Longest-first so «умножить на» wins over «умножить».
WORD_OPS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"умножить на|умножь на|умножить|умножь"), "*"),
    (re.compile(r"разделить на|поделить на|делить на|разделить|поделить"), "/"),
    (re.compile(r"плюс"), "+"),
    (re.compile(r"минус"), "-"),
]

QUESTION_RE = re.compile(r"^(сколько будет|скольким будет|сколько это|посчитай|чему равно|чему равняется|вычисли)\s+")
SYMBOL_RE = re.compile(r"^[+\-*/]$")
NUMBER_RE = re.compile(r"^\d+(?:\.\d+)?$")

DIV_ZERO_REPLY = "На ноль делить нельзя."


def _compose(words: list[str]) -> int | None:
    """Additive Russian numeral run («двести пятьдесят четыре» -> 254); thousands are multiplicative."""
    total = 0
    current = 0
    seen = False
    for word in words:
        value = NUMERALS[word]
        seen = True
        if value >= 1000:
            total += (current or 1) * value
            current = 0
        else:
            current += value
    return total + current if seen else None


def _tokenize(expression: str) -> list[str | Decimal] | None:
    tokens: list[str | Decimal] = []
    parts = [part for part in expression.split(" ") if part]
    index = 0
    while index < len(parts):
        part = parts[index]
        if SYMBOL_RE.match(part):
            if not tokens or isinstance(tokens[-1], str):
                return None
            tokens.append(part)
            index += 1
        elif NUMBER_RE.match(part):
            tokens.append(Decimal(part))
            index += 1
        elif part in NUMERALS:
            run: list[str] = []
            while index < len(parts) and parts[index] in NUMERALS:
                run.append(parts[index])
                index += 1
            value = _compose(run)
            if value is None:
                return None
            tokens.append(Decimal(value))
        else:
            return None
    # Strictly alternating number/operator/number structure.
    if len(tokens) < 3 or isinstance(tokens[-1], str):
        return None
    for position, token in enumerate(tokens):
        expects_number = position % 2 == 0
        if expects_number == isinstance(token, str):
            return None
    return tokens


def _has_zero_divisor(tokens: list[str | Decimal]) -> bool:
    return any(tokens[index] == "/" and tokens[index + 1] == 0 for index in range(len(tokens) - 1))


def _evaluate(tokens: list[str | Decimal]) -> Decimal | None:
    values: list[Decimal] = []
    operators: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if isinstance(token, Decimal):
            values.append(token)
        elif token in ("*", "/"):
            left, right = values.pop(), tokens[index + 1]
            if not isinstance(right, Decimal):
                return None
            try:
                values.append(left * right if token == "*" else left / right)
            except (DivisionByZero, InvalidOperation):
                return None
            index += 1
        else:
            operators.append(token)
        index += 1
    result = values[0]
    for position, operator in enumerate(operators):
        right = values[position + 1]
        result = result + right if operator == "+" else result - right
    return result


def evaluate(text: str) -> str | None:
    """Answer string for a pure-arithmetic utterance, else None."""
    expression = text.lower().replace("ё", "е").replace("×", "*").replace("÷", "/")
    expression = QUESTION_RE.sub("", expression)
    for pattern, symbol in WORD_OPS:
        expression = pattern.sub(f" {symbol} ", expression)
    expression = expression.replace(",", ".")
    # Space-pad symbols so glued input («2+2») tokenizes.
    expression = re.sub(r"\s*([+\-*/])\s*", r" \1 ", expression)
    expression = re.sub(r"\s+", " ", expression).strip()
    if not any(symbol in expression for symbol in "+-*/"):
        return None
    tokens = _tokenize(expression)
    if tokens is None:
        return None
    if _has_zero_divisor(tokens):
        return DIV_ZERO_REPLY
    result = _evaluate(tokens)
    if result is None:
        return None
    return format(result.normalize(), "f")
