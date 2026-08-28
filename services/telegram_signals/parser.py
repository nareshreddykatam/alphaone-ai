"""External Telegram signal parser (Multi-Coin AI Futures System, Phases
24-25). A pure function over raw message text -- no network/DB access --
so it is fully unit-testable against real example message shapes without
ever needing a live channel connection.

Deliberately conservative: a message like "BTC LONG" or "Buy BTC" or
"BTC long soon" must NEVER become a trade. VALID requires symbol +
direction + entry + stop-loss + at least one take-profit, with SL/TP on
the correct side of entry for the stated direction. Anything else is
INCOMPLETE, AMBIGUOUS, UNSUPPORTED_SYMBOL, UNSUPPORTED_MARKET, or INVALID
-- never guessed into a trade.
"""
import re
from dataclasses import dataclass
from typing import Optional

# Only USDT-quoted perpetual futures are in scope -- this system's whole
# architecture (CoinDCX-authoritative, USDT-primary) has no meaning for
# any other quote currency.
_SYMBOL_PATTERN = re.compile(r"\b([A-Z]{2,10})[\s/\-]?USDT\b", re.IGNORECASE)
_DIRECTION_PATTERN = re.compile(r"\b(LONG|SHORT|BUY|SELL)\b", re.IGNORECASE)
_ENTRY_PATTERN = re.compile(r"\bEntry\b\s*[:\-]?\s*\$?([\d,]+\.?\d*)", re.IGNORECASE)
_SL_PATTERN = re.compile(r"\b(?:SL|Stop\s*Loss|Stoploss)\b\s*[:\-]?\s*\$?([\d,]+\.?\d*)", re.IGNORECASE)
_TP1_PATTERN = re.compile(r"\b(?:TP\s*1|TP1|Target\s*1|Take\s*Profit\s*1)\b\s*[:\-]?\s*\$?([\d,]+\.?\d*)", re.IGNORECASE)
_TP2_PATTERN = re.compile(r"\b(?:TP\s*2|TP2|Target\s*2|Take\s*Profit\s*2)\b\s*[:\-]?\s*\$?([\d,]+\.?\d*)", re.IGNORECASE)
_TP3_PATTERN = re.compile(r"\b(?:TP\s*3|TP3|Target\s*3|Take\s*Profit\s*3)\b\s*[:\-]?\s*\$?([\d,]+\.?\d*)", re.IGNORECASE)
# A single bare "TP:" (no number suffix) with no TP1/2/3 label at all --
# treated as TP1 only when NONE of the numbered forms above matched, so a
# "TP1 / TP2" message is never double-counted.
_TP_BARE_PATTERN = re.compile(r"\bTP\b\s*[:\-]?\s*\$?([\d,]+\.?\d*)", re.IGNORECASE)
_LEVERAGE_PATTERN = re.compile(r"\b(\d{1,3})\s*[xX]\b")
_TIMEFRAME_PATTERN = re.compile(r"\b(1m|5m|15m|30m|1h|4h|1d)\b", re.IGNORECASE)

_DIRECTION_NORMALIZE = {"LONG": "LONG", "BUY": "LONG", "SHORT": "SHORT", "SELL": "SHORT"}


@dataclass
class ParsedExternalSignal:
    status: str  # VALID / INVALID / INCOMPLETE / AMBIGUOUS / UNSUPPORTED_SYMBOL / UNSUPPORTED_MARKET
    rejection_reason: Optional[str] = None
    raw_symbol: Optional[str] = None
    symbol: Optional[str] = None  # normalized "BASE/USDT"
    direction: Optional[str] = None
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit_1: Optional[float] = None
    take_profit_2: Optional[float] = None
    take_profit_3: Optional[float] = None
    leverage_stated: Optional[int] = None
    timeframe_stated: Optional[str] = None


def _to_number(s: Optional[str]) -> Optional[float]:
    if s is None:
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def normalize_symbol_string(raw: str) -> str:
    """"BTCUSDT" / "BTC/USDT" / "BTC-USDT" / "btc usdt" -> "BTC/USDT"."""
    base = re.sub(r"[\s/\-]?USDT$", "", raw.strip(), flags=re.IGNORECASE).upper()
    return f"{base}/USDT"


def parse_external_signal(text: str, supported_symbols: set[str]) -> ParsedExternalSignal:
    """`supported_symbols` is the caller's real, verified-eligible symbol
    set (see services/scanner/multi_coin.py) -- a symbol that parses fine
    but isn't in it is UNSUPPORTED_SYMBOL, never silently traded."""
    if not text or not text.strip():
        return ParsedExternalSignal(status="INVALID", rejection_reason="Empty message.")

    symbol_match = _SYMBOL_PATTERN.search(text)
    direction_matches = _DIRECTION_PATTERN.findall(text)
    entry_match = _ENTRY_PATTERN.search(text)
    sl_match = _SL_PATTERN.search(text)
    tp1_match = _TP1_PATTERN.search(text)
    tp2_match = _TP2_PATTERN.search(text)
    tp3_match = _TP3_PATTERN.search(text)
    leverage_match = _LEVERAGE_PATTERN.search(text)
    timeframe_match = _TIMEFRAME_PATTERN.search(text)

    if tp1_match is None and tp2_match is None and tp3_match is None:
        bare_tp = _TP_BARE_PATTERN.search(text)
        if bare_tp is not None:
            tp1_match = bare_tp

    if symbol_match is None:
        return ParsedExternalSignal(status="INVALID", rejection_reason="No recognizable USDT symbol found in message.")

    raw_symbol = symbol_match.group(0)
    symbol = normalize_symbol_string(raw_symbol)

    if len(set(d.upper() for d in direction_matches)) > 1:
        return ParsedExternalSignal(
            status="AMBIGUOUS", raw_symbol=raw_symbol, symbol=symbol,
            rejection_reason=f"Multiple conflicting direction keywords found: {sorted(set(direction_matches))}.",
        )
    if not direction_matches:
        return ParsedExternalSignal(
            status="INCOMPLETE", raw_symbol=raw_symbol, symbol=symbol,
            rejection_reason="No LONG/SHORT/BUY/SELL direction keyword found.",
        )
    direction = _DIRECTION_NORMALIZE[direction_matches[0].upper()]

    entry = _to_number(entry_match.group(1)) if entry_match else None
    sl = _to_number(sl_match.group(1)) if sl_match else None
    tp1 = _to_number(tp1_match.group(1)) if tp1_match else None
    tp2 = _to_number(tp2_match.group(1)) if tp2_match else None
    tp3 = _to_number(tp3_match.group(1)) if tp3_match else None
    leverage_stated = int(leverage_match.group(1)) if leverage_match else None
    timeframe_stated = timeframe_match.group(1).lower() if timeframe_match else None

    if symbol not in supported_symbols:
        return ParsedExternalSignal(
            status="UNSUPPORTED_SYMBOL", raw_symbol=raw_symbol, symbol=symbol, direction=direction,
            entry_price=entry, stop_loss=sl, take_profit_1=tp1, take_profit_2=tp2, take_profit_3=tp3,
            leverage_stated=leverage_stated, timeframe_stated=timeframe_stated,
            rejection_reason=f"{symbol} is not in the verified-eligible CoinDCX futures whitelist.",
        )

    missing = [name for name, val in (("entry", entry), ("stop_loss", sl), ("take_profit_1", tp1)) if val is None]
    if missing:
        return ParsedExternalSignal(
            status="INCOMPLETE", raw_symbol=raw_symbol, symbol=symbol, direction=direction,
            entry_price=entry, stop_loss=sl, take_profit_1=tp1, take_profit_2=tp2, take_profit_3=tp3,
            leverage_stated=leverage_stated, timeframe_stated=timeframe_stated,
            rejection_reason=f"Missing required field(s): {', '.join(missing)}. Never guessed.",
        )

    # SL/TP must be on the correct side of entry for the stated direction
    # (Phase 16): LONG needs SL < Entry < TPs; SHORT needs SL > Entry > TPs.
    targets = [t for t in (tp1, tp2, tp3) if t is not None]
    if direction == "LONG":
        valid_structure = sl < entry and all(t > entry for t in targets)
    else:
        valid_structure = sl > entry and all(t < entry for t in targets)
    if not valid_structure:
        return ParsedExternalSignal(
            status="INVALID", raw_symbol=raw_symbol, symbol=symbol, direction=direction,
            entry_price=entry, stop_loss=sl, take_profit_1=tp1, take_profit_2=tp2, take_profit_3=tp3,
            leverage_stated=leverage_stated, timeframe_stated=timeframe_stated,
            rejection_reason=f"SL/TP levels are not structurally valid for a {direction} at entry {entry} "
                              f"(SL={sl}, TPs={targets}).",
        )

    return ParsedExternalSignal(
        status="VALID", raw_symbol=raw_symbol, symbol=symbol, direction=direction,
        entry_price=entry, stop_loss=sl, take_profit_1=tp1, take_profit_2=tp2, take_profit_3=tp3,
        leverage_stated=leverage_stated, timeframe_stated=timeframe_stated,
    )
