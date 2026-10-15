"""
Circuit breaker for calls to a dependency (here: Claude).

  closed     normal: calls go through; consecutive failures are counted
  open       after `threshold` failures in a row: calls are refused instantly for `cooldown` seconds
  half-open  after the cooldown: ONE call is let through as a probe; success -> closed, failure -> open again

Why: during an outage, every caller would otherwise wait for the full timeout and retries, and we would keep
hammering a service that is already struggling.
"""
import time


class CircuitOpen(Exception):
    """Refused without calling the dependency. retry_after = seconds until the breaker lets a probe through."""

    def __init__(self, retry_after):
        super().__init__(f"circuit open, retry in {retry_after}s")
        self.retry_after = retry_after


class CircuitBreaker:
    def __init__(self, threshold=3, cooldown=30.0, clock=time.monotonic):
        self.threshold, self.cooldown, self.clock = threshold, cooldown, clock    # clock: injectable for tests
        self.failures = 0
        self.opened_at = None

    @property
    def state(self):
        if self.opened_at is None:
            return "closed"
        return "open" if self.clock() - self.opened_at < self.cooldown else "half-open"

    def check(self):
        """Call before using the dependency. Raises CircuitOpen while open."""
        if self.state == "open":
            raise CircuitOpen(retry_after=max(1, round(self.cooldown - (self.clock() - self.opened_at))))
        if self.state == "half-open":
            self.opened_at = self.clock()        # let THIS call probe; others are refused until it reports back

    def success(self):
        self.failures, self.opened_at = 0, None

    def failure(self):
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = self.clock()
