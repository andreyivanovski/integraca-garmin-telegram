from __future__ import annotations

import re
from datetime import date, timedelta

from dateutil import parser as date_parser
from dateutil.relativedelta import FR, MO, SA, SU, TH, TU, WE, relativedelta

_WEEKDAYS = {
    "segunda": MO,
    "seg": MO,
    "terça": TU,
    "terca": TU,
    "ter": TU,
    "quarta": WE,
    "qua": WE,
    "quinta": TH,
    "qui": TH,
    "sexta": FR,
    "sex": FR,
    "sábado": SA,
    "sabado": SA,
    "sab": SA,
    "domingo": SU,
    "dom": SU,
}


def parse_date_pt(text: str, today: date | None = None) -> date:
    """Parse 'hoje', 'amanhã', 'sexta', '2026-07-20', etc."""
    today = today or date.today()
    raw = text.strip().lower()
    raw = raw.replace("ã", "a").replace("á", "a").replace("é", "e")

    if raw in {"hoje", "today"}:
        return today
    if raw in {"amanha", "amanhã", "tomorrow"}:
        return today + timedelta(days=1)
    if raw in {"depois de amanha", "depois de amanhã"}:
        return today + timedelta(days=2)

    for name, weekday in _WEEKDAYS.items():
        if name in raw:
            # próxima ocorrência (incluindo hoje se for o mesmo dia? → próxima se já passou)
            nxt = today + relativedelta(weekday=weekday(+1))
            if nxt == today:
                return today
            return nxt

    # try ISO / dateutil
    cleaned = re.sub(r"[^\d\-/\.]", " ", text).strip()
    try:
        dt = date_parser.parse(cleaned or text, dayfirst=True, fuzzy=True)
        return dt.date()
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"Não entendi a data: {text!r}") from exc
