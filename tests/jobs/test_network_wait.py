"""The scheduled run waits for DNS after the Mac wakes (docs/runbooks/network.md)."""

from __future__ import annotations

from cyp.jobs.runner import wait_for_network


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


def test_waits_until_dns_answers() -> None:
    clock = _Clock()
    calls: list[str] = []

    def resolve(host: str) -> object:
        calls.append(host)
        if len(calls) < 4:
            raise OSError(8, "nodename nor servname provided, or not known")
        return []

    assert wait_for_network("example.invalid", 900, resolve=resolve, sleep=clock.sleep, clock=clock)
    assert len(calls) == 4 and clock.t == 5 + 10 + 20


def test_gives_up_after_the_timeout() -> None:
    clock = _Clock()

    def resolve(host: str) -> object:
        raise OSError(8, "no dns")

    assert not wait_for_network(
        "example.invalid", 120, resolve=resolve, sleep=clock.sleep, clock=clock
    )
    assert clock.t <= 120
