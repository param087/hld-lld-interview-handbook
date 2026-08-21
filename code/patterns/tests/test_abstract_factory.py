"""Abstract Factory: matching families, the mismatch it prevents, and the dataclass-of-callables form."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from common import NotFoundError, ValidationError
from patterns.abstract_factory import (
    AwsFactory,
    CloudProviderFactory,
    GcpFactory,
    GcsStorage,
    IngestPipeline,
    MessageQueue,
    ProviderKit,
    PubSubQueue,
    S3Storage,
    SqsQueue,
    Storage,
    aws_kit,
    factory_for,
    gcp_kit,
)

PAYLOAD = b"%PDF-1.7 invoice"


@pytest.mark.parametrize(
    ("factory", "storage_type", "queue_type", "scheme"),
    [
        (AwsFactory(region="eu-west-1"), S3Storage, SqsQueue, "s3://uploads-eu-west-1/"),
        (GcpFactory(project="acme"), GcsStorage, PubSubQueue, "gs://acme-uploads/"),
        (aws_kit(region="eu-west-1"), S3Storage, SqsQueue, "s3://uploads-eu-west-1/"),
        (gcp_kit(project="acme"), GcsStorage, PubSubQueue, "gs://acme-uploads/"),
    ],
)
def test_each_factory_builds_a_matching_family(
    factory: CloudProviderFactory, storage_type: type, queue_type: type, scheme: str
) -> None:
    assert isinstance(factory, CloudProviderFactory)  # classes and kits satisfy the same Protocol
    pipeline = IngestPipeline(factory)
    assert isinstance(pipeline.storage, storage_type) and isinstance(pipeline.storage, Storage)
    assert isinstance(pipeline.queue, queue_type) and isinstance(pipeline.queue, MessageQueue)
    assert pipeline.storage.put("k", PAYLOAD).startswith(scheme)


def test_pipeline_round_trips_through_one_family() -> None:
    pipeline = IngestPipeline(GcpFactory())
    assert pipeline.ingest("invoices/1.pdf", PAYLOAD) == "pubsub-1"
    assert pipeline.ingest("invoices/2.pdf", b"second") == "pubsub-2"
    assert pipeline.process_next() == PAYLOAD
    assert pipeline.process_next() == b"second"
    assert pipeline.process_next() is None  # an empty queue is not an error


def test_mixing_families_is_the_bug_the_pattern_prevents() -> None:
    uri = S3Storage("uploads-us-east-1").put("invoices/1.pdf", PAYLOAD)
    with pytest.raises(ValidationError):
        GcsStorage("handbook-uploads").get(uri)  # a consumer from the other family
    with pytest.raises(NotFoundError):
        S3Storage("uploads-us-east-1").get(uri)  # right family, different bucket instance, no object


def test_factory_for_picks_the_family_once_from_configuration() -> None:
    assert factory_for("aws", region="ap-south-1") == AwsFactory(region="ap-south-1")
    assert isinstance(factory_for("gcp"), GcpFactory)
    with pytest.raises(ValidationError):
        factory_for("azure")


def test_a_test_family_is_two_lambdas() -> None:
    uploads: list[str] = []

    class RecordingStorage:
        def put(self, key: str, data: bytes) -> str:
            uploads.append(key)
            return f"fake://{key}"

        def get(self, uri: str) -> bytes:
            return PAYLOAD

    kit = ProviderKit(create_storage=lambda bucket: RecordingStorage(), create_queue=PubSubQueue)
    pipeline = IngestPipeline(kit)
    assert pipeline.ingest("k", PAYLOAD) == "pubsub-1"
    assert uploads == ["k"]
    assert pipeline.process_next() == PAYLOAD


def test_queue_and_storage_are_safe_under_concurrent_producers() -> None:
    pipeline = IngestPipeline(AwsFactory())
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(lambda i: pipeline.ingest(f"blob-{i}", bytes([i % 256])), range(400)))
    assert len(set(ids)) == 400  # the counter never handed out a duplicate id
    consumed = [pipeline.process_next() for _ in range(400)]
    assert None not in consumed and pipeline.process_next() is None
