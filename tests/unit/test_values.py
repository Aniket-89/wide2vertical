from uuid import UUID

import pytest

from clipforge.domain.ids import new_id
from clipforge.domain.values import BlobKey, TimeRange, WorkspaceId

WS = WorkspaceId(UUID("01936f00-0000-7000-8000-000000000001"))


def test_time_range_validates_and_measures() -> None:
    r = TimeRange(10, 40)
    assert r.duration_s == 30
    assert r.overlap_s(TimeRange(30, 50)) == 10
    assert r.overlap_s(TimeRange(40, 50)) == 0
    with pytest.raises(ValueError):
        TimeRange(5, 5)
    with pytest.raises(ValueError):
        TimeRange(-1, 5)


def test_blob_key_is_always_workspace_prefixed() -> None:
    k = BlobKey.for_workspace(WS, "videos", "abc", "audio.m4a")
    assert k.value == f"ws/{WS}/videos/abc/audio.m4a"
    assert k.workspace_id == WS


@pytest.mark.parametrize(
    "bad",
    ["videos/abc", "ws/not-a-uuid/x", f"ws/{WS}/../escape", f"ws/{WS}/a b", f"ws/{WS}"],
)
def test_blob_key_rejects_unsafe(bad: str) -> None:
    with pytest.raises(ValueError):
        BlobKey(bad)


def test_new_id_is_uuid7_and_time_ordered() -> None:
    a, b = new_id(), new_id()
    assert a.version == 7
    assert a < b
