"""Single centralized INR formatter for backend-generated text (Telegram
messages). The frontend has its own formatINR (apps/web/lib/currency.ts,
via Intl.NumberFormat) -- this is the Python-side equivalent so Telegram
output uses the same Indian digit-grouping convention (lakhs/crores)
instead of scattering ad-hoc f-strings across services/telegram/bot.py.
"""
from typing import Optional


def _group_indian(integer_str: str) -> str:
    if len(integer_str) <= 3:
        return integer_str
    last3 = integer_str[-3:]
    rest = integer_str[:-3]
    groups = []
    while len(rest) > 2:
        groups.insert(0, rest[-2:])
        rest = rest[:-2]
    if rest:
        groups.insert(0, rest)
    return ",".join(groups) + "," + last3


def format_inr(value: Optional[float], show_sign: bool = False) -> str:
    """Indian-numbering INR string, e.g. format_inr(1000000) == '₹10,00,000.00'.
    Returns 'N/A' for None -- never fabricates a value. `show_sign` prefixes
    a '+' for positive values (for P&L display)."""
    if value is None:
        return "N/A"
    sign = "-" if value < 0 else ("+" if (show_sign and value > 0) else "")
    magnitude = abs(value)
    rupees = int(magnitude)
    paise = round((magnitude - rupees) * 100)
    if paise == 100:
        rupees += 1
        paise = 0
    return f"{sign}₹{_group_indian(str(rupees))}.{paise:02d}"
