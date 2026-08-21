"""Decorator: wrap an object in another object with the same interface to add behaviour.

Two running examples, because the pattern has two faces in interviews.
``Beverage`` is the coffee-machine classic: ``Espresso`` is the Component,
``AddOn`` the Decorator base, and ``Milk``, ``ExtraShot`` and ``Syrup`` stack in
any order and any number of times. ``Sender`` is the infrastructure version:
``SmtpSender`` sends, ``RetryingSender`` and ``AuditingSender`` wrap it behind the
same ``send`` method and add retries and an audit trail, and the order you stack
them in changes what the trail records. The last section restates both wrappers
as Python function decorators with ``functools.wraps``: the same idea applied to
callables instead of objects.
"""

from __future__ import annotations

import functools
import itertools
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from common import HandbookError, Money, ValidationError

ESPRESSO_PRICE = Money.of("2.00")
MILK_PRICE = Money.of("0.50")
SHOT_PRICE = Money.of("0.80")
SYRUP_PRICE = Money.of("0.40")


# --8<-- [start:beverage]
class Beverage(ABC):
    """The Component: what every drink, decorated or not, can be asked."""

    @abstractmethod
    def description(self) -> str: ...

    @abstractmethod
    def cost(self) -> Money: ...


@dataclass(frozen=True, slots=True)
class Espresso(Beverage):
    """A Concrete Component: the innermost object, the one that gets wrapped."""

    def description(self) -> str:
        return "espresso"

    def cost(self) -> Money:
        return ESPRESSO_PRICE


@dataclass(frozen=True, slots=True)
class AddOn(Beverage):
    """The Decorator base: holds *a* Beverage and forwards both calls unchanged.

    Subclasses override only what they change. Because an ``AddOn`` is itself a
    ``Beverage``, an add-on can wrap another add-on: that recursion is the pattern.
    """

    inner: Beverage

    def description(self) -> str:
        return self.inner.description()

    def cost(self) -> Money:
        return self.inner.cost()


@dataclass(frozen=True, slots=True)
class Milk(AddOn):
    def description(self) -> str:
        return f"{self.inner.description()}, milk"

    def cost(self) -> Money:
        return self.inner.cost() + MILK_PRICE


@dataclass(frozen=True, slots=True)
class ExtraShot(AddOn):
    def description(self) -> str:
        return f"{self.inner.description()}, extra shot"

    def cost(self) -> Money:
        return self.inner.cost() + SHOT_PRICE


@dataclass(frozen=True, slots=True)
class Syrup(AddOn):
    """A decorator with configuration of its own."""

    flavour: str = "vanilla"

    def description(self) -> str:
        return f"{self.inner.description()}, {self.flavour} syrup"

    def cost(self) -> Money:
        return self.inner.cost() + SYRUP_PRICE


# --8<-- [end:beverage]


# --8<-- [start:sender]
class SendError(HandbookError):
    """A transient transport failure: the kind a retry may cure."""


@runtime_checkable
class Sender(Protocol):
    """The Component interface as a Protocol: decorators and test fakes qualify by shape."""

    def send(self, recipient: str, message: str) -> str: ...


class SmtpSender:
    """The Concrete Component, standing in for a real transport.

    ``fail_first`` makes the first N calls raise, which lets the tests and the demo
    exercise the retry without a network and without sleeping.
    """

    def __init__(self, fail_first: int = 0) -> None:
        self._failures_left = fail_first
        self._ids = itertools.count(1)

    def send(self, recipient: str, message: str) -> str:
        if self._failures_left > 0:
            self._failures_left -= 1
            raise SendError("connection reset")
        return f"smtp-{next(self._ids)}"


class RetryingSender:
    """A Decorator: the same ``send``, plus retries with exponential backoff.

    The sleep is injected so tests never wait; the delays are still computed and
    handed to it, so a test can assert the schedule. A failure that outlives the
    budget propagates unchanged: the decorator adds behaviour, it does not
    translate errors.
    """

    def __init__(
        self,
        inner: Sender,
        attempts: int = 3,
        base_delay: float = 0.1,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if attempts < 1:
            raise ValidationError("attempts must be at least 1")
        self._inner = inner
        self._attempts = attempts
        self._base_delay = base_delay
        self._sleep = sleep

    def send(self, recipient: str, message: str) -> str:
        attempt = 1
        while True:
            try:
                return self._inner.send(recipient, message)
            except SendError:
                if attempt == self._attempts:
                    raise
                self._sleep(self._base_delay * 2 ** (attempt - 1))
                attempt += 1


class AuditingSender:
    """A Decorator: the same ``send``, plus one audit line per call it sees, success or failure."""

    def __init__(self, inner: Sender, sink: Callable[[str], None]) -> None:
        self._inner = inner
        self._sink = sink

    def send(self, recipient: str, message: str) -> str:
        try:
            receipt = self._inner.send(recipient, message)
        except SendError as exc:
            self._sink(f"{recipient}: failed ({exc})")
            raise
        self._sink(f"{recipient}: ok ({receipt})")
        return receipt


# --8<-- [end:sender]


# --8<-- [start:functional]
def retry[**P, R](
    attempts: int = 3, base_delay: float = 0.1, sleep: Callable[[float], None] = time.sleep
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """``RetryingSender`` for any callable that raises ``SendError``.

    ``functools.wraps`` copies ``__name__``, ``__doc__`` and friends onto the
    wrapper and records the original as ``__wrapped__``, so logs, tracebacks and
    ``inspect`` still see the function you wrote rather than ``wrapper``.
    """
    if attempts < 1:
        raise ValidationError("attempts must be at least 1")

    def decorate(fn: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            attempt = 1
            while True:
                try:
                    return fn(*args, **kwargs)
                except SendError:
                    if attempt == attempts:
                        raise
                    sleep(base_delay * 2 ** (attempt - 1))
                    attempt += 1

        return wrapper

    return decorate


def audited[**P, R](sink: Callable[[str], None]) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """``AuditingSender`` for callables; stacks with ``retry`` in either order."""

    def decorate(fn: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                result = fn(*args, **kwargs)
            except SendError as exc:
                sink(f"{fn.__name__}: failed ({exc})")
                raise
            sink(f"{fn.__name__}: ok ({result})")
            return result

        return wrapper

    return decorate


# --8<-- [end:functional]


def _no_sleep(_seconds: float) -> None:
    return None


def main() -> None:
    print("--- beverages: stack add-ons at runtime, the same add-on twice ---")
    orders: list[Beverage] = [
        Espresso(),
        Milk(Espresso()),
        ExtraShot(ExtraShot(Milk(Espresso()))),
        Syrup(Milk(Espresso()), flavour="hazelnut"),
    ]
    for drink in orders:
        print(f"{drink.description():<40} {drink.cost()}")

    print("--- senders: same interface in and out, retries layered around a flaky transport ---")
    delays: list[float] = []
    sender: Sender = RetryingSender(SmtpSender(fail_first=2), attempts=3, sleep=delays.append)
    receipt = sender.send("user-42", "your order has shipped")
    print(f"receipt {receipt} after {len(delays)} retries, backoff {delays}")

    print("--- order matters: audit outside retry sees one call, inside sees every attempt ---")
    outside: list[str] = []
    flaky: Sender = RetryingSender(SmtpSender(fail_first=2), sleep=_no_sleep)
    AuditingSender(flaky, outside.append).send("user-42", "hello")
    inside: list[str] = []
    audited_transport: Sender = AuditingSender(SmtpSender(fail_first=2), inside.append)
    RetryingSender(audited_transport, sleep=_no_sleep).send("user-42", "hello")
    print(f"audit outside retry: {outside}")
    print(f"audit inside retry:  {inside}")

    print("--- when the budget runs out the original error surfaces, untranslated ---")
    try:
        RetryingSender(SmtpSender(fail_first=5), attempts=3, sleep=_no_sleep).send("user-42", "x")
    except SendError as exc:
        print(f"SendError after 3 attempts: {exc}")

    print("--- function decorators: the same two wrappers for a callable ---")
    transport = SmtpSender(fail_first=1)
    trail: list[str] = []

    @audited(trail.append)
    @retry(attempts=3, sleep=_no_sleep)
    def send_email(recipient: str, message: str) -> str:
        """Send through the module transport."""
        return transport.send(recipient, message)

    print(f"{send_email('user-42', 'hello')} -> {trail}")
    print(f"wraps keeps the identity: name={send_email.__name__!r} doc={send_email.__doc__!r}")


if __name__ == "__main__":
    main()
