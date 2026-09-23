"""Курс доллара ЦБ РФ — для пересчёта затрат на ИИ (cost_usd) в рубли на экране «Статистика».

Каждый месяц пересчитывается по курсу на свой последний день (текущий — на сегодня):
июльские затраты по сентябрьскому курсу искажали бы сравнение с бюджетом. ЦБ на дату
выходного отдаёт последний установленный курс и пишет его дату в ответе — её и
показываем. Недоступен ЦБ — берётся ANALYTICS_USD_RUB с пометкой «допущение»."""

from __future__ import annotations

from datetime import date, datetime
import logging
import threading
import time
from typing import Any
import xml.etree.ElementTree as ET

import requests

from oiltech_digest import config

log = logging.getLogger(__name__)

CBR_DAILY_URL = "https://www.cbr.ru/scripts/XML_daily.asp"
USD_CODE = "USD"
TODAY_TTL_SECONDS = 6 * 3600

_cache: dict[str, tuple[dict[str, Any], float]] = {}
_lock = threading.Lock()


def parse_cbr_daily(xml: bytes, code: str = USD_CODE) -> dict[str, Any] | None:
    """Курс валюты из XML_daily.asp: рублей за 1 единицу и дата, на которую он установлен."""
    root = ET.fromstring(xml)
    for valute in root.findall("Valute"):
        if (valute.findtext("CharCode") or "").strip() != code:
            continue
        nominal = int((valute.findtext("Nominal") or "1").strip())
        value = float((valute.findtext("Value") or "").strip().replace(",", "."))
        actual = datetime.strptime(root.attrib["Date"], "%d.%m.%Y").date()
        return {"rate": value / nominal, "date": actual.isoformat(), "source": "ЦБ РФ"}
    return None


def usd_rub(on: date, *, today: date | None = None) -> dict[str, Any]:
    """Курс на дату; кэш: прошлые даты — навсегда (курс уже не изменится), сегодня — 6 ч."""
    today = today or date.today()
    key = on.isoformat()
    with _lock:
        cached = _cache.get(key)
    if cached and (on < today or time.monotonic() - cached[1] < TODAY_TTL_SECONDS):
        return cached[0]
    try:
        response = requests.get(CBR_DAILY_URL, params={"date_req": on.strftime("%d/%m/%Y")}, timeout=8)
        response.raise_for_status()
        rate = parse_cbr_daily(response.content)
        if rate is None:
            raise ValueError("в ответе ЦБ нет USD")
    except Exception as exc:  # noqa: BLE001 - экран не должен падать из-за курса
        log.warning("курс ЦБ на %s недоступен: %s — берётся ANALYTICS_USD_RUB", key, exc)
        return {"rate": config.ANALYTICS_USD_RUB, "date": None, "source": "допущение"}
    with _lock:
        _cache[key] = (rate, time.monotonic())
    return rate
