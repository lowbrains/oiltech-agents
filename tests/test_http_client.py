import requests

from oiltech_digest import config
from oiltech_digest.ingestion import http_client


class DummyResponse:
    def __init__(self, status_code=200, content=b"ok", headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status={self.status_code}", response=self)


def test_fetch_respects_host_cooldown(monkeypatch):
    http_client._host_cooldown_until.clear()
    http_client._host_next_allowed.clear()
    session_calls = {"n": 0}

    class DummySession:
        def get(self, *args, **kwargs):
            session_calls["n"] += 1
            return DummyResponse(status_code=429, headers={"Retry-After": "60"})

    monkeypatch.setattr(http_client, "_get_session", lambda: DummySession())
    monkeypatch.setattr(http_client, "_wait_for_host_slot", lambda host: None)
    monkeypatch.setattr(http_client.time, "sleep", lambda seconds: None)

    first = http_client.fetch("https://example.com/news")
    second = http_client.fetch("https://example.com/news")

    assert first is None
    assert second is None
    assert session_calls["n"] == 1


def test_fetch_returns_content_on_success(monkeypatch):
    http_client._host_cooldown_until.clear()
    http_client._host_next_allowed.clear()
    class DummySession:
        def get(self, *args, **kwargs):
            return DummyResponse(status_code=200, content=b"hello")

    monkeypatch.setattr(http_client, "_get_session", lambda: DummySession())
    monkeypatch.setattr(http_client, "_wait_for_host_slot", lambda host: None)

    assert http_client.fetch("https://example.com/feed") == b"hello"


def test_proxy_for_returns_none_without_config(monkeypatch):
    monkeypatch.setattr(http_client, "PROXY_URL", "")
    monkeypatch.setattr(http_client, "PROXY_HOST_OVERRIDES", {})
    assert http_client._proxy_for("example.com") is None


def test_proxy_for_uses_global_url(monkeypatch):
    monkeypatch.setattr(http_client, "PROXY_URL", "http://u:p@proxy.local:8080")
    monkeypatch.setattr(http_client, "PROXY_HOST_OVERRIDES", {})
    expected = {"http": "http://u:p@proxy.local:8080", "https": "http://u:p@proxy.local:8080"}
    assert http_client._proxy_for("example.com") == expected


def test_proxy_for_host_override_wins(monkeypatch):
    monkeypatch.setattr(http_client, "PROXY_URL", "http://global:0@proxy.local:8080")
    monkeypatch.setattr(
        http_client, "PROXY_HOST_OVERRIDES", {"example.com": "http://ru:1@ru.proxy:8080"}
    )
    expected = {"http": "http://ru:1@ru.proxy:8080", "https": "http://ru:1@ru.proxy:8080"}
    assert http_client._proxy_for("www.example.com") == expected


def test_proxy_for_prefers_more_specific_host_override(monkeypatch):
    monkeypatch.setattr(http_client, "PROXY_URL", "http://global:0@proxy.local:8080")
    monkeypatch.setattr(
        http_client,
        "PROXY_HOST_OVERRIDES",
        {
            "example.com": "http://general:1@proxy.local:8080",
            "news.example.com": "http://specific:1@proxy.local:8080",
        },
    )
    expected = {
        "http": "http://specific:1@proxy.local:8080",
        "https": "http://specific:1@proxy.local:8080",
    }
    assert http_client._proxy_for("news.example.com") == expected


def test_parse_proxy_host_overrides_from_env_string():
    assert config._parse_proxy_host_overrides(
        " rbc.ru = http://ru:1@proxy.local:8080, .shell.com=https://intl:2@proxy.local:8080,broken"
    ) == {
        "rbc.ru": "http://ru:1@proxy.local:8080",
        "shell.com": "https://intl:2@proxy.local:8080",
    }


def test_request_passes_proxies_to_session(monkeypatch):
    http_client._host_cooldown_until.clear()
    http_client._host_next_allowed.clear()
    monkeypatch.setattr(http_client, "PROXY_URL", "http://u:p@proxy.local:8080")
    monkeypatch.setattr(http_client, "PROXY_HOST_OVERRIDES", {})
    captured = {}

    class DummySession:
        def get(self, *args, **kwargs):
            captured.update(kwargs)
            return DummyResponse(status_code=200, content=b"ok")

    monkeypatch.setattr(http_client, "_get_session", lambda: DummySession())
    monkeypatch.setattr(http_client, "_wait_for_host_slot", lambda host: None)

    assert http_client.fetch("https://example.com/feed") == b"ok"
    assert captured["proxies"] == {
        "http": "http://u:p@proxy.local:8080",
        "https": "http://u:p@proxy.local:8080",
    }


def test_mask_proxy_hides_credentials():
    masked = http_client._mask_proxy("http://user:secret@proxy.local:8080")
    assert "secret" not in masked
    assert "user" not in masked
    assert "proxy.local:8080" in masked


def test_tally_counts_statuses():
    http_client._status_counts.clear()
    http_client._tally(200)
    http_client._tally(200)
    http_client._tally(403)
    assert http_client._status_counts == {"200": 2, "403": 1}


def test_legacy_tls_adapter_enables_renegotiation_and_skips_verify():
    import ssl

    adapter = http_client._LegacyTLSAdapter()
    ctx = adapter.poolmanager.connection_pool_kw.get("ssl_context")
    assert ctx is not None
    assert ctx.verify_mode == ssl.CERT_NONE
    assert ctx.check_hostname is False
    assert ctx.options & getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0x4)


def test_fetch_legacy_tls_returns_none_on_error(monkeypatch):
    def boom(*args, **kwargs):
        raise requests.exceptions.SSLError("renegotiation")

    monkeypatch.setattr(http_client, "_get_legacy_session", lambda: type("S", (), {"get": boom})())
    assert http_client._fetch_legacy_tls("https://legacy.example", 5) is None


def test_timeout_host_gets_cooldown_and_is_skipped_next_time(monkeypatch):
    """Молчаливый хост закрывается так же, как громкий.

    До этой правки пауза ставилась только по статусу 403/429, а таймаут
    переспрашивался заново для каждого адреса: пакет из 25 статей одного
    издания стоил 25x63 с и не доживал до конца lease.
    """
    http_client._host_cooldown_until.clear()
    http_client._host_next_allowed.clear()
    calls = {"n": 0}

    class DeadSession:
        def get(self, *args, **kwargs):
            calls["n"] += 1
            raise requests.exceptions.ConnectTimeout("read timed out")

    monkeypatch.setattr(http_client, "_get_session", lambda: DeadSession())
    monkeypatch.setattr(http_client, "_wait_for_host_slot", lambda host: None)
    monkeypatch.setattr(http_client.time, "sleep", lambda seconds: None)

    assert http_client.fetch("https://dead.example.com/a") is None
    spent_on_first = calls["n"]
    # Вторая статья того же издания не должна стоить ни одного запроса.
    assert http_client.fetch("https://dead.example.com/b") is None

    assert spent_on_first == config.RETRY_ATTEMPTS
    assert calls["n"] == spent_on_first


def test_page_level_error_does_not_close_whole_host(monkeypatch):
    """404 у одной страницы не повод закрывать издание целиком."""
    http_client._host_cooldown_until.clear()
    http_client._host_next_allowed.clear()
    calls = {"n": 0}

    class NotFoundSession:
        def get(self, *args, **kwargs):
            calls["n"] += 1
            return DummyResponse(status_code=404, content=b"")

    monkeypatch.setattr(http_client, "_get_session", lambda: NotFoundSession())
    monkeypatch.setattr(http_client, "_wait_for_host_slot", lambda host: None)
    monkeypatch.setattr(http_client.time, "sleep", lambda seconds: None)

    assert http_client.fetch("https://live.example.com/missing") is None
    assert not http_client._is_host_cooling_down("live.example.com")
    assert http_client.fetch("https://live.example.com/other") is None
    assert calls["n"] == 2 * config.RETRY_ATTEMPTS


def test_probe_failure_does_not_blacklist_host(monkeypatch):
    """Диагностика источника ходит с одной попыткой — закрывать по ней нельзя."""
    http_client._host_cooldown_until.clear()
    http_client._host_next_allowed.clear()

    class DeadSession:
        def get(self, *args, **kwargs):
            raise requests.exceptions.ConnectionError("no route")

    monkeypatch.setattr(http_client, "_get_session", lambda: DeadSession())
    monkeypatch.setattr(http_client, "_wait_for_host_slot", lambda host: None)
    monkeypatch.setattr(http_client.time, "sleep", lambda seconds: None)

    assert http_client.probe("https://slow.example.com/feed") is None
    assert not http_client._is_host_cooling_down("slow.example.com")
