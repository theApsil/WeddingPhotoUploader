"""Rate limiter unit tests + health check."""

from app.rate_limit import RateLimiter


def test_rate_limiter_blocks_after_limit():
    limiter = RateLimiter(limit_per_minute=3)
    now = 1_000_000.0
    assert limiter.allow("1.2.3.4", now=now)[0] is True
    assert limiter.allow("1.2.3.4", now=now + 1)[0] is True
    assert limiter.allow("1.2.3.4", now=now + 2)[0] is True
    assert limiter.allow("1.2.3.4", now=now + 3)[0] is False


def test_rate_limiter_window_slides():
    limiter = RateLimiter(limit_per_minute=2)
    now = 1_000_000.0
    assert limiter.allow("ip", now=now)[0] is True
    assert limiter.allow("ip", now=now + 1)[0] is True
    assert limiter.allow("ip", now=now + 2)[0] is False
    # After 60s the first hit expires.
    assert limiter.allow("ip", now=now + 61)[0] is True


def test_health(client):
    res = client.get("/api/health")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert data["storage_backend"] == "local"
    assert data["storage_configured"] is True
    assert "image/jpeg" in data["allowed_types"]
    # Internal storage path must not leak.
    assert "storage_dir" not in data


def test_rate_limiter_blocked_does_not_consume():
    limiter = RateLimiter(limit_per_minute=2)
    now = 1_000_000.0
    assert limiter.blocked("ip", now=now) is False
    assert limiter.blocked("ip", now=now) is False  # peeking twice uses nothing
    limiter.allow("ip", now=now)
    limiter.allow("ip", now=now + 1)
    assert limiter.blocked("ip", now=now + 2) is True
    assert limiter.blocked("ip", now=now + 61.5) is False  # window slid
