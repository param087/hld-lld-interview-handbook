"""Singleton: one instance, a race-free first creation, the Pythonic forms, and why injection wins."""

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError

import pytest

from common import NotFoundError
from patterns.singleton import (
    AppConfig,
    Borg,
    ConfigManager,
    FeatureFlags,
    SingletonMeta,
    Uploader,
    default_config,
)


@pytest.fixture(autouse=True)
def _fresh_instance() -> Iterator[None]:
    """The reset hook every Singleton grows once it meets a test suite."""
    ConfigManager.reset()
    yield
    ConfigManager.reset()


def test_every_call_returns_the_same_instance_and_state_is_shared() -> None:
    first, second = ConfigManager(), ConfigManager()
    assert first is second
    first.set("region", "eu-west-1")
    assert second.get("region") == "eu-west-1"
    second.load({"tier": "gold"})
    assert first.snapshot() == {"region": "eu-west-1", "tier": "gold"}
    with pytest.raises(NotFoundError):
        first.get("missing")


def test_state_survives_repeated_construction_because_there_is_no_init() -> None:
    ConfigManager().set("region", "eu-west-1")
    assert ConfigManager().get("region") == "eu-west-1"  # an __init__ would have wiped it


def test_first_creation_is_race_free_across_threads() -> None:
    workers = 32
    barrier = threading.Barrier(workers)

    def create(_: int) -> ConfigManager:
        barrier.wait(timeout=5)  # every thread reaches __new__ at the same moment
        return ConfigManager()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        instances = list(pool.map(create, range(workers)))
    assert len({id(instance) for instance in instances}) == 1


def test_reset_forgets_the_instance_which_is_exactly_why_tests_need_it() -> None:
    before = ConfigManager()
    before.set("region", "eu-west-1")
    ConfigManager.reset()
    after = ConfigManager()
    assert after is not before
    assert after.snapshot() == {}
    assert before.get("region") == "eu-west-1"  # holders of the old object keep stale state


def test_metaclass_runs_init_once_and_gives_each_class_its_own_instance() -> None:
    class Counter(metaclass=SingletonMeta):
        def __init__(self, start: int) -> None:
            self.value = start

    class Other(metaclass=SingletonMeta):
        pass

    assert Counter(1) is Counter(2)
    assert Counter(3).value == 1  # later constructor arguments are ignored
    assert Other() is Other()
    assert not isinstance(Other(), Counter)
    assert FeatureFlags() is FeatureFlags()


def test_cached_factory_returns_one_frozen_value() -> None:
    assert default_config() is default_config()
    assert default_config() == AppConfig()
    with pytest.raises(FrozenInstanceError):
        default_config().region = "eu-west-1"  # type: ignore[misc]


def test_borg_instances_differ_but_share_state() -> None:
    left, right = Borg(), Borg()
    left.theme = "dark"
    try:
        assert left is not right
        assert right.theme == "dark"
    finally:
        del left.theme  # the shared dict outlives the test; leave it as found
    assert not hasattr(right, "theme")


def test_injected_config_lets_two_configurations_coexist() -> None:
    prod = Uploader(AppConfig(region="eu-west-1", max_retries=5))
    local = Uploader(AppConfig(region="local", max_retries=0))
    assert prod.describe() == "upload to eu-west-1 with 5 retries"
    assert local.describe() == "upload to local with 0 retries"
