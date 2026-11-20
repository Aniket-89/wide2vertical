import pytest

from clipforge.compliance.allowlist import SourceFacts, Verdict, decide
from clipforge.domain.values import ClipPolicy, SourceStatus

A, R = SourceStatus.ACTIVE, SourceStatus.RETIRED


@pytest.mark.parametrize(
    ("facts", "override", "expected"),
    [
        # not allowlisted / unknown policy
        (None, False, Verdict.REFUSE),
        (None, True, Verdict.ALLOW_WITH_OVERRIDE),
        (SourceFacts(ClipPolicy.UNKNOWN, A, True), False, Verdict.REFUSE),
        (SourceFacts(ClipPolicy.UNKNOWN, A, True), True, Verdict.ALLOW_WITH_OVERRIDE),
        # takedown beats override
        (SourceFacts(ClipPolicy.OWN_CHANNEL, R, True), False, Verdict.REFUSE),
        (SourceFacts(ClipPolicy.OWN_CHANNEL, R, True), True, Verdict.REFUSE),
        (SourceFacts(ClipPolicy.UNKNOWN, R, False), True, Verdict.REFUSE),
        # credit required for campaign / written permission
        (SourceFacts(ClipPolicy.CAMPAIGN, A, False), False, Verdict.REFUSE),
        (SourceFacts(ClipPolicy.CAMPAIGN, A, False), True, Verdict.ALLOW_WITH_OVERRIDE),
        (SourceFacts(ClipPolicy.WRITTEN_PERMISSION, A, False), False, Verdict.REFUSE),
        (SourceFacts(ClipPolicy.CAMPAIGN, A, True), False, Verdict.ALLOW),
        (SourceFacts(ClipPolicy.WRITTEN_PERMISSION, A, True), False, Verdict.ALLOW),
        # credit optional
        (SourceFacts(ClipPolicy.OWN_CHANNEL, A, False), False, Verdict.ALLOW),
        (SourceFacts(ClipPolicy.OPEN_POLICY, A, False), False, Verdict.ALLOW),
        # override on an already-allowed source changes nothing
        (SourceFacts(ClipPolicy.OPEN_POLICY, A, True), True, Verdict.ALLOW),
    ],
)
def test_decide(facts: SourceFacts | None, override: bool, expected: Verdict) -> None:
    d = decide(facts, override=override)
    assert d.verdict is expected
    assert d.reason
