"""T: The manager/controller/service classes: where the behaviour and locks live.

T: Inject Clock and IdGenerator from common; never call time.time()/uuid4 here.
"""

from __future__ import annotations

import threading

from common import Clock, IdGenerator, SequentialIdGenerator, SystemClock

from lld.lld_problem.models import ExampleEntity


class ExampleService:
    def __init__(self, clock: Clock | None = None, ids: IdGenerator | None = None) -> None:
        self._clock = clock or SystemClock()
        self._ids = ids or SequentialIdGenerator("ex")
        self._entities: dict[str, ExampleEntity] = {}
        self._lock = threading.RLock()

    def create(self) -> ExampleEntity:
        with self._lock:
            entity = ExampleEntity(id=self._ids.next_id())
            self._entities[entity.id] = entity
            return entity
