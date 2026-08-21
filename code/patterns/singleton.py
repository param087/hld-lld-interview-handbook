"""Singleton: one instance per process, reached through a global access point.

The running example is a ``ConfigManager`` whose ``__new__`` hands every caller the
same object and uses double-checked locking so that two threads racing on the first
call cannot build two. The second section shows the forms Python actually uses (a
metaclass, a ``functools.cache`` factory, the Borg) and the third shows the answer
the rest of this handbook prefers: build one instance in ``main`` and inject it, so
a test can build its own.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import cache
from typing import Any, ClassVar

from common import NotFoundError

RACING_THREADS = 16


# --8<-- [start:classic]
class ConfigManager:
    """The classic form: ``__new__`` returns the one instance, guarded by a lock.

    Two locks, two jobs: ``_lock`` (class level) protects ``_instance`` during
    creation; ``_guard`` (instance level) protects ``_settings`` afterwards. There
    is deliberately no ``__init__``: Python runs ``__init__`` on *every*
    ``ConfigManager()`` call, so state set up there would be wiped each time the
    access point is used. ``_init_once`` runs under the lock, exactly once.
    """

    _instance: ClassVar[ConfigManager | None] = None
    _lock: ClassVar[threading.Lock] = threading.Lock()

    def __new__(cls) -> ConfigManager:
        if cls._instance is None:  # fast path: no lock once the instance exists
            with cls._lock:
                if cls._instance is None:  # second check: the loser of the race stops here
                    instance = super().__new__(cls)
                    instance._init_once()
                    cls._instance = instance  # published last, so readers see a finished object
        return cls._instance

    def _init_once(self) -> None:
        self._guard = threading.RLock()
        self._settings: dict[str, str] = {}

    def get(self, key: str) -> str:
        with self._guard:
            try:
                return self._settings[key]
            except KeyError:
                raise NotFoundError(f"no setting named {key!r}") from None

    def set(self, key: str, value: str) -> None:
        with self._guard:
            self._settings[key] = value

    def load(self, values: Mapping[str, str]) -> None:
        with self._guard:
            self._settings.update(values)

    def snapshot(self) -> dict[str, str]:
        with self._guard:
            return dict(self._settings)

    @classmethod
    def reset(cls) -> None:
        """Test hook: forget the instance. Needing this is the first sign injection would be simpler."""
        with cls._lock:
            cls._instance = None


# --8<-- [end:classic]


# --8<-- [start:pythonic]
class SingletonMeta(type):
    """Metaclass form: ``Cls()`` is intercepted before ``__new__`` and ``__init__`` run.

    ``__init__`` therefore runs exactly once, and every class built with this
    metaclass gets its own ``_instance`` and ``_lock``: a subclass is a second
    singleton, not a second copy of the parent.
    """

    def __init__(cls, name: str, bases: tuple[type, ...], namespace: dict[str, Any]) -> None:
        super().__init__(name, bases, namespace)
        cls._instance = None
        cls._lock = threading.Lock()

    def __call__(cls, *args: Any, **kwargs: Any) -> Any:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__call__(*args, **kwargs)
        return cls._instance


class FeatureFlags(metaclass=SingletonMeta):
    """Constructor arguments after the first call are silently ignored: a known trap."""

    def __init__(self, flags: Mapping[str, bool] | None = None) -> None:
        self.flags = dict(flags or {})

    def enabled(self, name: str) -> bool:
        return self.flags.get(name, False)


@dataclass(frozen=True, slots=True)
class AppConfig:
    """A frozen value: safe to share, impossible to corrupt, trivial to build twice in tests."""

    region: str = "us-east-1"
    max_retries: int = 3


@cache
def default_config() -> AppConfig:
    """The function is the access point and ``cache`` is the instance store.

    ``cache`` holds no lock around the call: two threads racing on the very first
    call can each build an ``AppConfig`` and the cache keeps the last one. Harmless
    for a frozen value, wrong for anything that owns a connection or a thread.
    """
    return AppConfig()


class Borg:
    """Monostate: many instances, one shared ``__dict__``. Identity differs, state agrees."""

    _shared_state: ClassVar[dict[str, Any]] = {}

    def __init__(self) -> None:
        self.__dict__ = self._shared_state


# --8<-- [end:pythonic]


# --8<-- [start:injected]
class Uploader:
    """Takes its configuration as a constructor argument.

    Nothing here enforces that only one ``AppConfig`` exists: ``main`` builds one
    and passes it along, and a test builds a different one. One instance by
    convention rather than by construction is this handbook's default answer.
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config

    def describe(self) -> str:
        return f"upload to {self._config.region} with {self._config.max_retries} retries"


# --8<-- [end:injected]


def main() -> None:
    print("--- classic: every ConfigManager() call returns the same object ---")
    first, second = ConfigManager(), ConfigManager()
    first.set("region", "eu-west-1")
    print(f"first is second: {first is second}; second.get('region') -> {second.get('region')}")

    print(f"--- {RACING_THREADS} threads race to create the first instance ---")
    ConfigManager.reset()
    barrier = threading.Barrier(RACING_THREADS)

    def create(_: int) -> ConfigManager:
        barrier.wait(timeout=5)
        return ConfigManager()

    with ThreadPoolExecutor(max_workers=RACING_THREADS) as pool:
        instances = list(pool.map(create, range(RACING_THREADS)))
    print(f"distinct instances: {len({id(instance) for instance in instances})}")

    print("--- metaclass: __init__ runs once, later arguments are ignored ---")
    flags = FeatureFlags({"beta": True})
    again = FeatureFlags({"beta": False})
    print(f"flags is again: {flags is again}; beta enabled: {again.enabled('beta')}")

    print("--- cached factory and Borg ---")
    print(f"default_config() is default_config(): {default_config() is default_config()}")
    left, right = Borg(), Borg()
    left.theme = "dark"
    print(f"left is right: {left is right}; right.theme: {right.theme}")

    print("--- injection: one instance by convention, built in main ---")
    config = AppConfig(region="eu-west-1", max_retries=5)
    print(Uploader(config).describe())
    local = Uploader(AppConfig(region="local", max_retries=0))
    print(f"a test builds its own: {local.describe()}")


if __name__ == "__main__":
    main()
