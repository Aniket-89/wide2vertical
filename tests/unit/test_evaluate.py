from clipforge.scoring.evaluate import evaluate_at_k


def test_precision_recall_at_k() -> None:
    ranked = [(0, 30), (100, 130), (200, 230), (300, 330)]
    labels = [10, 215, 500]
    r = evaluate_at_k(ranked, labels, 3)
    assert (r.hits, r.covered, r.labels) == (2, 2, 3)
    assert r.precision == round(2 / 3, 4) and r.recall == round(2 / 3, 4)


def test_tolerance_and_edge_cases() -> None:
    assert evaluate_at_k([(0, 30)], [33], 1, tolerance_s=5).hits == 1
    assert evaluate_at_k([(0, 30)], [36], 1, tolerance_s=5).hits == 0
    empty = evaluate_at_k([], [10], 5)
    assert (empty.precision, empty.recall) == (0.0, 0.0)
    assert evaluate_at_k([(0, 30)], [], 5).recall == 0.0
    # K larger than the candidate list: precision over what exists
    assert evaluate_at_k([(0, 30)], [5], 10).precision == 1.0
