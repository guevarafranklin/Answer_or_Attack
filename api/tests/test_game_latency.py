"""§5 — the per-player RTT tracker, on its own."""
from app.game import latency
from app.game.latency import RttTracker

T = 1_700_000_000_000


def test_samples_are_pong_minus_ping_on_the_server_clock():
    tr = RttTracker()
    stamp = tr.ping(T)
    assert stamp == T
    assert tr.pong(stamp, T + 80) == 80
    assert tr.rtt_ms == 80


def test_median_over_a_rolling_window():
    tr = RttTracker()
    for i, delay in enumerate([40, 60, 50, 45, 900]):
        stamp = tr.ping(T + i * 1000)
        tr.pong(stamp, stamp + delay)
    assert tr.rtt_ms == 50  # the outlier is one vote
    # The window slides: WINDOW samples of 200 push everything else out.
    for i in range(latency.WINDOW):
        stamp = tr.ping(T + 10_000 + i * 1000)
        tr.pong(stamp, stamp + 200)
    assert tr.rtt_ms == 200 and len(tr.samples) == latency.WINDOW


def test_unknown_duplicate_and_stale_pongs_are_ignored():
    tr = RttTracker()
    stamp = tr.ping(T)
    assert tr.pong(stamp + 1, T + 10) is None  # never sent
    assert tr.pong(stamp, T + 10) == 10
    assert tr.pong(stamp, T + 20) is None  # echoed twice
    old = tr.ping(T + 100)
    assert tr.pong(old, T + 100 + latency.STALE_MS + 1) is None  # too old to trust
    assert list(tr.samples) == [10]
    assert tr.rtt_ms == 10


def test_stamps_are_unique_even_within_one_millisecond():
    tr = RttTracker()
    a, b, c = tr.ping(T), tr.ping(T), tr.ping(T)
    assert (a, b, c) == (T, T + 1, T + 2)
    assert tr.ping(T + 1000) == T + 1000
    assert tr.pong(b, T + 50) == 49  # off by the bump, never negative
    assert tr.pong(T + 2, T + 1) == 0


def test_outstanding_pings_are_capped():
    tr = RttTracker()
    stamps = [tr.ping(T + i) for i in range(latency.MAX_PENDING + 5)]
    assert len(tr.pending) == latency.MAX_PENDING
    assert tr.pong(stamps[0], T + 100) is None  # the oldest were forgotten
    assert tr.pong(stamps[-1], T + 100) is not None


def test_no_samples_means_zero():
    assert RttTracker().rtt_ms == 0
