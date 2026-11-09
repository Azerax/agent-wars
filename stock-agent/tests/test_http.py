import pytest
import requests

from stockagent.data.http import DataError, HttpClient


class _Session:
    def __init__(self, exc=None, responses=None):
        self.exc = exc
        self.responses = list(responses or [])
        self.calls = 0
        self.headers = {}

    def get(self, *a, **kw):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.responses.pop(0)


class _Resp:
    def __init__(self, code, text="", headers=None):
        self.status_code, self.text, self.headers = code, text, headers or {}


def client(session):
    c = HttpClient(":memory:", "test-agent", min_interval=0.0, retries=3)
    c.session = session
    return c


def test_proxy_refusal_is_not_retried_and_trips_the_breaker():
    s = _Session(exc=requests.exceptions.ProxyError("403 from proxy"))
    c = client(s)
    with pytest.raises(DataError, match="refused"):
        c.get_text("https://blocked.example/a")
    with pytest.raises(DataError, match="skipped"):
        c.get_text("https://blocked.example/b")
    assert s.calls == 1


def test_404_fails_fast_without_tripping_the_breaker():
    s = _Session(responses=[_Resp(404), _Resp(200, "ok")])
    c = client(s)
    with pytest.raises(DataError, match="404"):
        c.get_text("https://site.example/missing")
    assert c.get_text("https://site.example/ok") == "ok"


def test_cache_serves_repeat_requests():
    s = _Session(responses=[_Resp(200, '{"a": 1}')])
    c = client(s)
    assert c.get_json("https://site.example/x", ttl=60) == {"a": 1}
    assert c.get_json("https://site.example/x", ttl=60) == {"a": 1}
    assert s.calls == 1
