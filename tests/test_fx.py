"""Курс ЦБ РФ для экрана «Статистика»: разбор ответа, кэш, запасной курс."""

from datetime import date

from oiltech_digest import config, fx

# Фрагмент настоящего ответа XML_daily.asp (windows-1251, запятая в числе, дата курса
# в атрибуте — на выходной ЦБ отдаёт последний установленный курс).
CBR_XML = (
    '<?xml version="1.0" encoding="windows-1251"?><ValCurs Date="29.08.2026" name="Foreign Currency Market">'
    '<Valute ID="R01035"><NumCode>826</NumCode><CharCode>GBP</CharCode><Nominal>1</Nominal>'
    '<Name>Фунт</Name><Value>112,0101</Value></Valute>'
    '<Valute ID="R01235"><NumCode>840</NumCode><CharCode>USD</CharCode><Nominal>1</Nominal>'
    '<Name>Доллар США</Name><Value>85,6007</Value><VunitRate>85,6007</VunitRate></Valute>'
    '<Valute ID="R01375"><NumCode>156</NumCode><CharCode>CNY</CharCode><Nominal>10</Nominal>'
    '<Name>Юань</Name><Value>118,0000</Value></Valute></ValCurs>'
).encode("windows-1251")


class _Response:
    def __init__(self, content: bytes):
        self.content = content

    def raise_for_status(self):
        pass


def test_parse_takes_usd_its_nominal_and_the_rate_date():
    assert fx.parse_cbr_daily(CBR_XML) == {"rate": 85.6007, "date": "2026-08-29", "source": "ЦБ РФ"}
    assert fx.parse_cbr_daily(CBR_XML, code="CNY")["rate"] == 11.8  # номинал 10 юаней


def test_past_date_is_cached_forever_and_failure_falls_back_to_assumption(monkeypatch):
    fx._cache.clear()
    calls = []
    monkeypatch.setattr(fx.requests, "get", lambda url, params, timeout: calls.append(params) or _Response(CBR_XML))
    today = date(2026, 9, 19)
    assert fx.usd_rub(date(2026, 8, 31), today=today)["rate"] == 85.6007
    assert fx.usd_rub(date(2026, 8, 31), today=today)["rate"] == 85.6007
    assert calls == [{"date_req": "31/08/2026"}]  # второй раз — из кэша

    def down(*args, **kwargs):
        raise OSError("cbr.ru недоступен")

    monkeypatch.setattr(fx.requests, "get", down)
    fallback = fx.usd_rub(date(2026, 7, 31), today=today)
    assert fallback == {"rate": config.ANALYTICS_USD_RUB, "date": None, "source": "допущение"}
    fx._cache.clear()
