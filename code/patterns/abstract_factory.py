"""Abstract Factory: build families of objects that must match.

The running example is an ingest pipeline that needs a blob store and a message
queue from the *same* cloud provider. ``CloudProviderFactory`` (the Abstract
Factory) declares one factory method per product; ``AwsFactory`` and
``GcpFactory`` (the Concrete Factories) hand back matching ``Storage`` and
``MessageQueue`` implementations, so ``IngestPipeline`` can never pair an S3
bucket with a Pub/Sub topic. The second section shows the Pythonic form: a
frozen dataclass of factory callables that satisfies the same Protocol.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar, Protocol, runtime_checkable

from common import NotFoundError, ValidationError


# --8<-- [start:products]
@runtime_checkable
class Storage(Protocol):
    """Abstract product: a blob store. ``put`` returns the object's URI."""

    def put(self, key: str, data: bytes) -> str: ...

    def get(self, uri: str) -> bytes: ...


@runtime_checkable
class MessageQueue(Protocol):
    """Abstract product: a queue of small messages, here the URIs of uploaded blobs."""

    def publish(self, message: str) -> str: ...

    def receive(self) -> str | None: ...


class _InMemoryStorage:
    """Stand-in for a cloud bucket. The URI scheme is what a mismatched consumer trips over.

    ``_lock`` protects ``_objects``; a bucket is shared by every thread that uploads.
    """

    scheme: ClassVar[str]

    def __init__(self, bucket: str) -> None:
        self.bucket = bucket
        self._objects: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def put(self, key: str, data: bytes) -> str:
        with self._lock:
            self._objects[key] = data
        return f"{self.scheme}://{self.bucket}/{key}"

    def get(self, uri: str) -> bytes:
        prefix = f"{self.scheme}://{self.bucket}/"
        if not uri.startswith(prefix):
            raise ValidationError(f"{uri!r} is not an object of {prefix!r}")
        with self._lock:
            try:
                return self._objects[uri.removeprefix(prefix)]
            except KeyError:
                raise NotFoundError(f"no object at {uri!r}") from None


class S3Storage(_InMemoryStorage):
    scheme = "s3"


class GcsStorage(_InMemoryStorage):
    scheme = "gs"


class _InMemoryQueue:
    """Stand-in for a managed queue. ``_lock`` protects the deque and the id counter."""

    id_prefix: ClassVar[str]

    def __init__(self, name: str) -> None:
        self.name = name
        self._messages: deque[str] = deque()
        self._published = 0
        self._lock = threading.Lock()

    def publish(self, message: str) -> str:
        with self._lock:
            self._published += 1
            self._messages.append(message)
            return f"{self.id_prefix}-{self._published}"

    def receive(self) -> str | None:
        with self._lock:
            return self._messages.popleft() if self._messages else None


class SqsQueue(_InMemoryQueue):
    id_prefix = "sqs"


class PubSubQueue(_InMemoryQueue):
    id_prefix = "pubsub"


# --8<-- [end:products]


# --8<-- [start:factory]
@runtime_checkable
class CloudProviderFactory(Protocol):
    """The Abstract Factory: one factory method per product in the family."""

    def create_storage(self, bucket: str) -> Storage: ...

    def create_queue(self, name: str) -> MessageQueue: ...


@dataclass(frozen=True, slots=True)
class AwsFactory:
    """Concrete factory. The family's configuration (the region) lives here, not in the client."""

    region: str = "us-east-1"

    def create_storage(self, bucket: str) -> Storage:
        return S3Storage(f"{bucket}-{self.region}")

    def create_queue(self, name: str) -> MessageQueue:
        return SqsQueue(f"{name}.{self.region}")


@dataclass(frozen=True, slots=True)
class GcpFactory:
    project: str = "handbook"

    def create_storage(self, bucket: str) -> Storage:
        return GcsStorage(f"{self.project}-{bucket}")

    def create_queue(self, name: str) -> MessageQueue:
        return PubSubQueue(f"projects/{self.project}/topics/{name}")


def factory_for(provider: str, **options: str) -> CloudProviderFactory:
    """A Factory Method that picks the Abstract Factory: called once, at the composition root."""
    factories: dict[str, Callable[..., CloudProviderFactory]] = {"aws": AwsFactory, "gcp": GcpFactory}
    try:
        return factories[provider](**options)
    except KeyError:
        raise ValidationError(f"unknown provider {provider!r}") from None


class IngestPipeline:
    """The client: asks one factory for everything it needs, so the products always match.

    The factory is used in ``__init__`` and not kept; the pipeline holds products,
    and there is no code path through which a second family could get in.
    """

    def __init__(self, provider: CloudProviderFactory, bucket: str = "uploads", topic: str = "uploaded") -> None:
        self._storage = provider.create_storage(bucket)
        self._queue = provider.create_queue(topic)

    @property
    def storage(self) -> Storage:
        return self._storage

    @property
    def queue(self) -> MessageQueue:
        return self._queue

    def ingest(self, key: str, data: bytes) -> str:
        """Producer side: store the blob, publish its URI, return the message id."""
        return self._queue.publish(self._storage.put(key, data))

    def process_next(self) -> bytes | None:
        """Consumer side: take the next URI and read the blob it names."""
        uri = self._queue.receive()
        return None if uri is None else self._storage.get(uri)


# --8<-- [end:factory]


# --8<-- [start:pythonic]
@dataclass(frozen=True, slots=True)
class ProviderKit:
    """A family as data: two factory callables in a frozen dataclass.

    The field names match the Protocol's methods, so ``ProviderKit`` satisfies
    ``CloudProviderFactory`` structurally and ``IngestPipeline`` cannot tell it
    from ``AwsFactory``. A new family is a function returning a kit, not a class;
    a test family is two lambdas.
    """

    create_storage: Callable[[str], Storage]
    create_queue: Callable[[str], MessageQueue]


def aws_kit(region: str = "us-east-1") -> ProviderKit:
    return ProviderKit(
        create_storage=lambda bucket: S3Storage(f"{bucket}-{region}"),
        create_queue=lambda name: SqsQueue(f"{name}.{region}"),
    )


def gcp_kit(project: str = "handbook") -> ProviderKit:
    return ProviderKit(
        create_storage=lambda bucket: GcsStorage(f"{project}-{bucket}"),
        create_queue=lambda name: PubSubQueue(f"projects/{project}/topics/{name}"),
    )


# --8<-- [end:pythonic]


def main() -> None:
    payload = b"%PDF-1.7 invoice for August"
    for name in ("aws", "gcp"):
        pipeline = IngestPipeline(factory_for(name))
        print(f"--- {name}: one factory, two products that match ---")
        print(f"published {pipeline.ingest('invoices/2026-08.pdf', payload)}")
        blob = pipeline.process_next() or b""
        storage, queue = type(pipeline.storage).__name__, type(pipeline.queue).__name__
        print(f"consumed {len(blob)} bytes through {storage} and {queue}")

    print("--- the bug the pattern prevents: an AWS URI handed to a GCP consumer ---")
    uri = S3Storage("uploads-us-east-1").put("invoices/2026-08.pdf", payload)
    try:
        GcsStorage("handbook-uploads").get(uri)
    except ValidationError as exc:
        print(f"rejected: {exc}")

    print("--- Pythonic: a frozen dataclass of callables is the same family as data ---")
    kit = aws_kit(region="eu-west-1")
    print(f"kit satisfies CloudProviderFactory: {isinstance(kit, CloudProviderFactory)}")
    print(f"published {IngestPipeline(kit).ingest('a.txt', b'hello')} via {kit.create_queue('probe').name}")

    try:
        factory_for("azure")
    except ValidationError as exc:
        print(f"rejected: {exc}")


if __name__ == "__main__":
    main()
