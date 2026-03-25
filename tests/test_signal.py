from bot.signal import compute


def test_strong_long():
    s = compute(1.0, 2.0, 3.0)
    assert s.score == 3
    assert s.label == "strong_long"
    assert s.multiplier == 1.0
    assert s.direction == "long"


def test_moderate_long():
    s = compute(1.0, -1.0, 2.0)
    assert s.score == 2
    assert s.label == "moderate_long"
    assert s.multiplier == 0.5
    assert s.direction == "long"


def test_moderate_short():
    s = compute(1.0, -1.0, -2.0)
    assert s.score == 1
    assert s.label == "moderate_short"
    assert s.multiplier == 0.5
    assert s.direction == "short"


def test_strong_short():
    s = compute(-1.0, -2.0, -3.0)
    assert s.score == 0
    assert s.label == "strong_short"
    assert s.multiplier == 1.0
    assert s.direction == "short"


def test_zero_changes():
    # All exactly zero → data missing or market flat → hold (no trade)
    s = compute(0.0, 0.0, 0.0)
    assert s.score == 0
    assert s.label == "hold"
    assert s.direction == "none"
    assert s.multiplier == 0.0
