import threading

import pytest

from common import ValidationError
from hld.transcoding_dag import (
    DEFAULT_LADDER,
    Dag,
    DagScheduler,
    Rendition,
    Stage,
    Task,
    TaskStatus,
    build_transcode_dag,
    simulate_makespan,
)

LADDER = (Rendition("360p", 360, 800, 1.0), Rendition("1080p", 1080, 5_000, 2.0))


def noop(task: Task, attempt: int) -> None:
    """A runner that always succeeds; the scheduler is what is under test."""


def test_dag_shape_matches_the_pipeline() -> None:
    dag = build_transcode_dag(segments=4, ladder=LADDER)
    stages = [dag.tasks[tid].stage for tid in dag.tasks]
    assert stages.count(Stage.ENCODE) == 4 * len(LADDER)
    assert stages.count(Stage.STITCH) == len(LADDER)
    assert len(dag.tasks) == 1 + 8 + 2 + 1 + 1  # split, encodes, stitches, package, publish
    assert dag.tasks["stitch:1080p"].deps == tuple(f"encode:1080p:{i:03d}" for i in range(4))
    assert dag.dependents("package") == ("publish",)


def test_topological_order_never_precedes_a_dependency() -> None:
    dag = build_transcode_dag(segments=3, ladder=DEFAULT_LADDER)
    order = dag.topological_order()
    position = {tid: i for i, tid in enumerate(order)}
    assert len(order) == len(dag.tasks)
    for tid, task in dag.tasks.items():
        assert all(position[dep] < position[tid] for dep in task.deps)


@pytest.mark.parametrize(
    "tasks, message",
    [
        ([Task("a", Stage.ENCODE, ("ghost",), 1.0)], "unknown task"),
        (
            [
                Task("a", Stage.ENCODE, ("b",), 1.0),
                Task("b", Stage.ENCODE, ("a",), 1.0),
            ],
            "cycle",
        ),
        (
            [Task("a", Stage.ENCODE, (), 1.0), Task("a", Stage.STITCH, (), 1.0)],
            "duplicate",
        ),
    ],
)
def test_invalid_graphs_are_rejected_at_construction(tasks: list[Task], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Dag(tasks)


def test_critical_path_bounds_the_makespan() -> None:
    dag = build_transcode_dag(segments=6, ladder=DEFAULT_LADDER)
    path, length = dag.critical_path()
    assert path[0] == "split" and path[-1] == "publish"
    # split 1.0 + one 1080p encode 2.0 + stitch 0.5 + package 0.5 + publish 0.2
    assert length == pytest.approx(4.2)
    assert simulate_makespan(dag, 1) == pytest.approx(dag.serial_cost())
    assert simulate_makespan(dag, 1_000) == pytest.approx(length)
    assert length < simulate_makespan(dag, 4) < dag.serial_cost()


def test_scheduler_starts_a_task_only_after_every_dependency_finished() -> None:
    dag = build_transcode_dag(segments=3, ladder=LADDER)
    lock = threading.Lock()
    finished: set[str] = set()
    violations: list[str] = []

    def runner(task: Task, attempt: int) -> None:
        with lock:
            missing = [dep for dep in task.deps if dep not in finished]
        if missing:
            violations.append(f"{task.id} started before {missing}")
        with lock:
            finished.add(task.id)

    report = DagScheduler(workers=4).run(dag, runner)
    assert violations == []
    assert report.ok
    assert len(report.by_status(TaskStatus.DONE)) == len(dag.tasks)


def test_transient_failures_are_retried_in_place() -> None:
    dag = build_transcode_dag(segments=2, ladder=LADDER)
    attempts: dict[str, int] = {}

    def flaky(task: Task, attempt: int) -> None:
        attempts[task.id] = attempt
        if task.id == "encode:360p:001" and attempt < 3:
            raise TimeoutError("spot instance preempted")

    report = DagScheduler(workers=4, max_attempts=3).run(dag, flaky)
    assert report.ok
    assert report.results["encode:360p:001"].attempts == 3
    assert report.results["encode:360p:000"].attempts == 1


def test_permanent_failure_dead_letters_and_skips_only_its_descendants() -> None:
    dag = build_transcode_dag(segments=2, ladder=LADDER)

    def broken(task: Task, attempt: int) -> None:
        if task.id == "encode:1080p:000":
            raise RuntimeError("unsupported pixel format")

    report = DagScheduler(workers=4, max_attempts=2).run(dag, broken)
    assert report.dead_letters == ("encode:1080p:000",)
    assert report.skipped == ("package", "publish", "stitch:1080p")
    assert report.results["encode:1080p:000"].attempts == 2
    assert report.results["stitch:360p"].status is TaskStatus.DONE  # the other rung survives
    assert not report.ok


def test_independent_encodes_run_on_all_workers_at_once() -> None:
    """Four segment encodes become ready together; a barrier proves they overlap."""
    dag = build_transcode_dag(segments=2, ladder=LADDER)  # 2 segments x 2 renditions
    barrier = threading.Barrier(4, timeout=5.0)

    def runner(task: Task, attempt: int) -> None:
        if task.stage is Stage.ENCODE:
            barrier.wait()

    report = DagScheduler(workers=4).run(dag, runner)
    assert report.ok
    assert report.peak_parallelism == 4


@pytest.mark.parametrize(
    "call",
    [
        lambda: build_transcode_dag(segments=0),
        lambda: build_transcode_dag(segments=2, ladder=()),
        lambda: simulate_makespan(build_transcode_dag(1), 0),
        lambda: DagScheduler(workers=0),
        lambda: DagScheduler(max_attempts=0),
    ],
)
def test_bad_configuration_is_rejected(call: object) -> None:
    with pytest.raises(ValidationError):
        call()  # type: ignore[operator]
