import socket

import httpx
import pytest

from backend import trudvsem


def test_trudvsem_retries_a_timed_out_page(monkeypatch):
    times = iter([0, 8, 8, 9])
    monkeypatch.setattr(trudvsem, "monotonic", lambda: next(times))
    requests = []

    def upstream(request):
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ReadTimeout("slow response", request=request)
        return httpx.Response(200, json={"status": "200", "meta": {"total": "0"}})

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        assert trudvsem.fetch_page(client, "Python", 1, deadline=10) == []

    assert requests[0].url == requests[1].url
    assert [request.extensions["timeout"]["read"] for request in requests] == [8, 2]


@pytest.mark.parametrize(("deadline", "code", "calls"), [
    (45, "TRUDVSEM_REQUEST_TIMEOUT", 2),
    (5, "TRUDVSEM_COLLECTION_TIMEOUT", 1),
])
def test_trudvsem_distinguishes_timeout_limits(monkeypatch, deadline, code, calls):
    elapsed = 0
    requests = []
    monkeypatch.setattr(trudvsem, "monotonic", lambda: elapsed)

    def upstream(request):
        nonlocal elapsed
        requests.append(request)
        elapsed += request.extensions["timeout"]["read"]
        raise httpx.ReadTimeout("slow response", request=request)

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        with pytest.raises(trudvsem.TrudvsemError, match=code):
            trudvsem.fetch_page(client, "Python", 0, deadline=deadline)

    assert len(requests) == calls


def test_trudvsem_reports_dns_failure_without_retry(monkeypatch):
    monkeypatch.setattr(trudvsem, "monotonic", lambda: 0)
    requests = []

    def upstream(request):
        requests.append(request)
        raise httpx.ConnectError("connection failed", request=request) from socket.gaierror(11001, "DNS failure")

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        with pytest.raises(trudvsem.TrudvsemError, match="DISCOVERY_DNS_FAILED"):
            trudvsem.fetch_page(client, "Python", 0, deadline=45)

    assert len(requests) == 1
