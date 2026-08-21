"""The transcoding DAG: split a source video by GOP, encode every segment of every rendition
in parallel, stitch, package, publish.

What the module demonstrates, in the order an interviewer asks about it:

* ``build_transcode_dag`` turns "one 10-minute upload" into the graph a pipeline really runs:
  one ``split``, ``segments x renditions`` independent ``encode`` tasks, one ``stitch`` per
  rendition, one ``package`` that writes the HLS/DASH manifests, one ``publish``.
* ``Dag`` validates the graph at construction (unknown dependency, cycle), gives a
  deterministic ``topological_order`` and the ``critical_path`` -- the floor on latency no
  amount of hardware removes.
* ``simulate_makespan`` answers the capacity question: how long the same DAG takes on ``W``
  machines, so you can say "4 workers get me 3x, 40 workers get me the critical path".
* ``DagScheduler.run`` executes it for real on a bounded ``ThreadPoolExecutor``: only tasks
  whose dependencies finished are dispatched, each task is retried up to ``max_attempts``,
  a task that keeps failing is dead-lettered and its descendants are marked skipped rather
  than left hanging.

Work is injected as a ``runner`` callable, so tests and the demo are deterministic and fast.
"""

from __future__ import annotations

import heapq
import threading
from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from common import ValidationError


# --8<-- [start:dag]
class Stage(StrEnum):
    """Pipeline stages, in dependency order."""

    SPLIT = "split"  # demux the mezzanine file into GOP-aligned chunks
    ENCODE = "encode"  # one chunk, one rendition: the embarrassingly parallel part
    STITCH = "stitch"  # concatenate a rendition's chunks back into an ordered segment list
    PACKAGE = "package"  # write the media playlists and the master manifest
    PUBLISH = "publish"  # flip the video to ready and warm the CDN


class TaskStatus(StrEnum):
    DONE = "done"
    DEAD = "dead"  # exhausted its retries: dead-lettered for a human or a fallback encoder
    SKIPPED = "skipped"  # an ancestor died, so this task can never become runnable


@dataclass(frozen=True, slots=True)
class Rendition:
    """One rung of the bitrate ladder."""

    name: str
    height: int
    bitrate_kbps: int
    encode_cost: float  # seconds of CPU per segment; 4K costs ~10x what 240p costs


@dataclass(frozen=True, slots=True)
class Task:
    id: str
    stage: Stage
    deps: tuple[str, ...]
    cost: float  # estimated seconds of work, used for the critical path and the simulation


class Dag:
    """An immutable task graph. Construction fails on an unknown dependency or a cycle."""

    def __init__(self, tasks: Iterable[Task]) -> None:
        self._tasks: dict[str, Task] = {}
        for task in tasks:
            if task.id in self._tasks:
                raise ValidationError(f"duplicate task id {task.id!r}")
            self._tasks[task.id] = task
        dependents: dict[str, list[str]] = defaultdict(list)
        for task in self._tasks.values():
            for dep in task.deps:
                if dep not in self._tasks:
                    raise ValidationError(f"task {task.id!r} depends on unknown task {dep!r}")
                dependents[dep].append(task.id)
        self._dependents = {tid: tuple(sorted(children)) for tid, children in dependents.items()}
        self.topological_order()  # raises on a cycle, so a Dag is always acyclic

    @property
    def tasks(self) -> Mapping[str, Task]:
        return MappingProxyType(self._tasks)

    def dependents(self, task_id: str) -> tuple[str, ...]:
        return self._dependents.get(task_id, ())

    def topological_order(self) -> list[str]:
        """Kahn's algorithm over a sorted ready set, so the order is reproducible."""
        indegree = {tid: len(task.deps) for tid, task in self._tasks.items()}
        ready = deque(sorted(tid for tid, n in indegree.items() if n == 0))
        order: list[str] = []
        while ready:
            tid = ready.popleft()
            order.append(tid)
            for child in self.dependents(tid):
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
        if len(order) != len(self._tasks):
            stuck = sorted(set(self._tasks) - set(order))
            raise ValidationError(f"cycle in the task graph involving {stuck}")
        return order

    def serial_cost(self) -> float:
        """What one machine would take: the sum of every task's cost."""
        return sum(task.cost for task in self._tasks.values())

    def critical_path(self) -> tuple[list[str], float]:
        """The longest chain by cost: the makespan you cannot beat with more workers."""
        best: dict[str, float] = {}
        parent: dict[str, str | None] = {}
        for tid in self.topological_order():
            task = self._tasks[tid]
            prev = max(task.deps, key=lambda d: best[d], default=None)
            best[tid] = (best[prev] if prev else 0.0) + task.cost
            parent[tid] = prev
        end = max(best, key=lambda t: best[t])  # ties: first in topological order
        path: list[str] = []
        node: str | None = end
        while node is not None:
            path.append(node)
            node = parent[node]
        return list(reversed(path)), best[end]


# --8<-- [end:dag]


# --8<-- [start:builder]
DEFAULT_LADDER: tuple[Rendition, ...] = (
    Rendition("240p", 240, 400, 0.4),
    Rendition("720p", 720, 2_500, 1.0),
    Rendition("1080p", 1080, 5_000, 2.0),
)


def build_transcode_dag(
    segments: int,
    ladder: Sequence[Rendition] = DEFAULT_LADDER,
    split_cost: float = 1.0,
) -> Dag:
    """The graph for one upload: 1 split, ``segments x len(ladder)`` encodes, then fan-in.

    Segments are cut on GOP boundaries (a keyframe starts every chunk), which is what makes
    the encodes independent: no encoder needs a frame that lives in another chunk.
    """
    if segments <= 0:
        raise ValidationError("a video has at least one segment")
    if not ladder:
        raise ValidationError("the bitrate ladder cannot be empty")
    tasks = [Task("split", Stage.SPLIT, (), split_cost)]
    stitches: list[str] = []
    for rendition in ladder:
        encodes = []
        for index in range(segments):
            tid = f"encode:{rendition.name}:{index:03d}"
            encodes.append(tid)
            tasks.append(Task(tid, Stage.ENCODE, ("split",), rendition.encode_cost))
        stitch = f"stitch:{rendition.name}"
        stitches.append(stitch)
        tasks.append(Task(stitch, Stage.STITCH, tuple(encodes), 0.5))
    tasks.append(Task("package", Stage.PACKAGE, tuple(stitches), 0.5))
    tasks.append(Task("publish", Stage.PUBLISH, ("package",), 0.2))
    return Dag(tasks)


def simulate_makespan(dag: Dag, workers: int) -> float:
    """Greedy list scheduling (longest task first): wall-clock seconds on ``workers`` machines.

    The answer sits between ``critical_path()`` (infinite workers) and ``serial_cost()`` (one),
    which is the arithmetic behind "how many encoder machines does this fleet need".
    """
    if workers <= 0:
        raise ValidationError("workers must be positive")
    indegree = {tid: len(task.deps) for tid, task in dag.tasks.items()}
    ready = [(-dag.tasks[tid].cost, tid) for tid, n in indegree.items() if n == 0]
    heapq.heapify(ready)
    running: list[tuple[float, str]] = []
    now, free = 0.0, workers
    while ready or running:
        while ready and free:
            _, tid = heapq.heappop(ready)
            free -= 1
            heapq.heappush(running, (now + dag.tasks[tid].cost, tid))
        now, finished = heapq.heappop(running)
        free += 1
        for child in dag.dependents(finished):
            indegree[child] -= 1
            if indegree[child] == 0:
                heapq.heappush(ready, (-dag.tasks[child].cost, child))
    return now


# --8<-- [end:builder]


# --8<-- [start:scheduler]
@dataclass(frozen=True, slots=True)
class TaskResult:
    task_id: str
    attempts: int
    status: TaskStatus
    error: str = ""


@dataclass(frozen=True, slots=True)
class RunReport:
    results: dict[str, TaskResult]
    dead_letters: tuple[str, ...]
    skipped: tuple[str, ...]
    peak_parallelism: int

    @property
    def ok(self) -> bool:
        return not self.dead_letters and not self.skipped

    def by_status(self, status: TaskStatus) -> tuple[str, ...]:
        return tuple(sorted(tid for tid, r in self.results.items() if r.status is status))


class _Gauge:
    """Observed concurrency. ``_lock`` guards ``_live`` and ``_peak``; both are per-run."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._live = 0
        self._peak = 0

    def __enter__(self) -> None:
        with self._lock:
            self._live += 1
            self._peak = max(self._peak, self._live)

    def __exit__(self, *exc_info: object) -> None:
        with self._lock:
            self._live -= 1

    @property
    def peak(self) -> int:
        with self._lock:
            return self._peak


class DagScheduler:
    """Runs a DAG on a bounded pool: ready tasks only, bounded retries, dead letters, skips.

    All bookkeeping (in-degrees, results, the poison set) lives in ``run`` and is touched only
    by the dispatch loop's own thread; the worker threads touch nothing but the injected
    ``runner`` and the per-run ``_Gauge``, whose lock guards its two counters. That is the
    property to state out loud: the coordinator is single-threaded, the work is not.
    """

    def __init__(self, workers: int = 4, max_attempts: int = 3) -> None:
        if workers <= 0:
            raise ValidationError("workers must be positive")
        if max_attempts <= 0:
            raise ValidationError("max_attempts must be positive")
        self._workers = workers
        self._max_attempts = max_attempts

    def run(self, dag: Dag, runner: Callable[[Task, int], None]) -> RunReport:
        indegree = {tid: len(task.deps) for tid, task in dag.tasks.items()}
        results: dict[str, TaskResult] = {}
        gauge = _Gauge()
        with ThreadPoolExecutor(max_workers=self._workers) as pool:
            inflight: dict[Future[TaskResult], str] = {}

            def dispatch(task_id: str) -> None:
                future = pool.submit(self._attempt, dag.tasks[task_id], runner, gauge)
                inflight[future] = task_id

            for tid in sorted(tid for tid, n in indegree.items() if n == 0):
                dispatch(tid)
            while inflight:
                done, _ = wait(list(inflight), return_when=FIRST_COMPLETED)
                for future in done:
                    task_id = inflight.pop(future)
                    result = future.result()
                    results[task_id] = result
                    if result.status is not TaskStatus.DONE:
                        self._poison(dag, task_id, results)
                        continue
                    for child in dag.dependents(task_id):
                        indegree[child] -= 1
                        if indegree[child] == 0 and child not in results:
                            dispatch(child)
        picked = {
            status: tuple(sorted(tid for tid, r in results.items() if r.status is status))
            for status in (TaskStatus.DEAD, TaskStatus.SKIPPED)
        }
        return RunReport(results, picked[TaskStatus.DEAD], picked[TaskStatus.SKIPPED], gauge.peak)

    def _attempt(
        self, task: Task, runner: Callable[[Task, int], None], gauge: _Gauge
    ) -> TaskResult:
        """Retry in place: a transcode failure is usually a preempted spot instance."""
        error = ""
        for attempt in range(1, self._max_attempts + 1):
            try:
                with gauge:
                    runner(task, attempt)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                continue
            return TaskResult(task.id, attempt, TaskStatus.DONE)
        return TaskResult(task.id, self._max_attempts, TaskStatus.DEAD, error)

    @staticmethod
    def _poison(dag: Dag, task_id: str, results: dict[str, TaskResult]) -> None:
        """Mark every descendant skipped so the run terminates instead of stalling."""
        stack = list(dag.dependents(task_id))
        while stack:
            child = stack.pop()
            if child in results:
                continue
            results[child] = TaskResult(child, 0, TaskStatus.SKIPPED, f"upstream {task_id} failed")
            stack.extend(dag.dependents(child))


# --8<-- [end:scheduler]


def main() -> None:
    import time

    dag = build_transcode_dag(segments=6)
    path, length = dag.critical_path()
    print(f"upload vid-42: 6 segments x {len(DEFAULT_LADDER)} renditions = {len(dag.tasks)} tasks")
    print(f"serial cost {dag.serial_cost():.1f}s | critical path {length:.1f}s: {' -> '.join(path)}")
    for workers in (1, 4, 24):
        span = simulate_makespan(dag, workers)
        print(f"  {workers:>2} workers -> makespan {span:5.1f}s ({dag.serial_cost() / span:.1f}x)")

    flaky = {"encode:720p:002": 1}  # fails its first attempt, then succeeds
    log: list[str] = []

    def runner(task: Task, attempt: int) -> None:
        time.sleep(0.005)  # stand in for a real encode so the parallelism is observable
        if attempt <= flaky.get(task.id, 0):
            log.append(f"{task.id} attempt {attempt} failed (spot instance preempted)")
            raise TimeoutError("encoder vanished")

    report = DagScheduler(workers=4).run(dag, runner)
    for line in log:
        print("  retry:", line)
    done = len(report.by_status(TaskStatus.DONE))
    print(f"run: {done}/{len(dag.tasks)} done, peak parallelism {report.peak_parallelism}, ok={report.ok}")

    def broken_1080p(task: Task, attempt: int) -> None:
        if task.id == "encode:1080p:000":
            raise RuntimeError("unsupported pixel format")

    degraded = DagScheduler(workers=4).run(dag, broken_1080p)
    print(f"degraded run: dead letters {list(degraded.dead_letters)}")
    print(f"  skipped {list(degraded.skipped)}")
    print(f"  {len(degraded.by_status(TaskStatus.DONE))} tasks still succeeded: publish the ladder that survived")


if __name__ == "__main__":
    main()
