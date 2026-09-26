"""Server-measured round-trip time, one tracker per connected player (§5).

The server sends `ping {server_ms}`; the client echoes it as
`pong {server_ms}` at once. Both ends of the measurement are the
server's own clock — the stamp it put in the ping and the stamp the
runtime put on the pong when it was enqueued — so a client that lies
about time can only make its own pong late, never early. The tracker
keeps the last few samples and answers with their median: one stalled
sample (a GC pause, a radio wake-up) does not move it much, a sustained
change does.

A pong is ignored when it echoes a stamp that was never sent, was
already echoed, or is older than `STALE_MS` — so a late echo cannot
plant an absurd sample, and a client cannot echo one ping many times.
"""
from __future__ import annotations

from collections import deque
from statistics import median

PING_BURST = 5  # pings on connect
PING_BURST_GAP_MS = 250  # between those
PING_INTERVAL_MS = 15_000  # then one every ...
WINDOW = 7  # samples the median is taken over
STALE_MS = 2 * PING_INTERVAL_MS  # an outstanding ping older than this is forgotten
MAX_PENDING = 32


class RttTracker:
    def __init__(self) -> None:
        self.samples: deque[int] = deque(maxlen=WINDOW)
        self.pending: dict[int, None] = {}  # stamps sent, awaiting their echo (insertion-ordered)
        self._last_stamp = 0

    def ping(self, now_ms: int) -> int:
        """The stamp to send. Strictly increasing per player, so two pings
        in one millisecond cannot be confused; the bump is at most a few
        ms and only when the clock stalls."""
        stamp = max(now_ms, self._last_stamp + 1)
        self._last_stamp = stamp
        self._forget_stale(now_ms)
        self.pending[stamp] = None
        while len(self.pending) > MAX_PENDING:
            del self.pending[next(iter(self.pending))]
        return stamp

    def pong(self, server_ms: int, now_ms: int) -> int | None:
        """Record a sample if the echo is for an outstanding ping; the
        sample, or None when the pong was ignored."""
        self._forget_stale(now_ms)
        if server_ms not in self.pending:
            return None
        del self.pending[server_ms]
        sample = max(0, now_ms - server_ms)
        self.samples.append(sample)
        return sample

    @property
    def rtt_ms(self) -> int:
        """The rolling median, 0 until the first sample arrives."""
        if not self.samples:
            return 0
        return int(median(self.samples))

    def _forget_stale(self, now_ms: int) -> None:
        for stamp in list(self.pending):
            if now_ms - stamp > STALE_MS:
                del self.pending[stamp]
            else:
                break  # ordered by stamp
