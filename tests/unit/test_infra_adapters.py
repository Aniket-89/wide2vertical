from pathlib import Path
from uuid import UUID

from clipforge.adapters.blobstore.local import LocalBlobStore
from clipforge.adapters.eventbus.in_process import InProcessEventBus
from clipforge.adapters.queue.dramatiq_queue import all_queue_names, queue_name
from clipforge.domain.events import SourceRetired, VideoIngested
from clipforge.domain.values import BlobKey, WorkspaceId
from clipforge.ports.infra import Priority

WS = WorkspaceId(UUID("01936f00-0000-7000-8000-000000000001"))


def test_local_blobstore_roundtrip(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path / "blobs")
    src = tmp_path / "in.txt"
    src.write_text("hello")
    key = BlobKey.for_workspace(WS, "v1", "a.txt")

    assert store.put(key, src) == 5
    assert store.exists(key)
    with store.open_local(key) as p:
        assert p.read_text() == "hello"
    assert list(store.iter_keys(BlobKey.for_workspace(WS, "v1"))) == [key]
    store.delete(key)
    assert not store.exists(key)


def test_event_bus_dispatches_by_type_and_isolates_failures() -> None:
    bus = InProcessEventBus()
    got: list[str] = []

    def boom(_: object) -> None:
        raise RuntimeError("bad subscriber")

    bus.subscribe(VideoIngested, boom)
    bus.subscribe(VideoIngested, lambda e: got.append("video"))
    bus.subscribe(SourceRetired, lambda e: got.append("source"))

    bus.publish(VideoIngested(workspace_id=WS, video_id=UUID(int=1), override_used=False))
    assert got == ["video"]


def test_priority_maps_to_separate_queues() -> None:
    assert queue_name("default", Priority.HIGH) == "default_high"
    assert queue_name("default", Priority.NORMAL) == "default"
    assert set(all_queue_names("transcribe")) == {"transcribe", "transcribe_high", "transcribe_low"}


def test_worker_broker_declares_all_queues_and_producer_does_not_install() -> None:
    import dramatiq

    from clipforge.adapters.queue.dramatiq_queue import make_broker
    from clipforge.infra.registry import QUEUE_BASES

    before = dramatiq.get_broker()
    producer = make_broker("redis://127.0.0.1:1/0")
    assert dramatiq.get_broker() is before  # producers never replace the global broker

    worker = make_broker("redis://127.0.0.1:1/0", queue_bases=QUEUE_BASES)
    declared = worker.get_declared_queues()
    for q in (
        "default",
        "default_high",
        "default_low",
        "transcribe",
        "transcribe_high",
        "transcribe_low",
    ):
        assert q in declared
    del producer
