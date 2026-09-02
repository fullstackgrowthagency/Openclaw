from fx_connector.backoff import backoff_delay


def test_backoff_delay_attempt_zero_is_roughly_base():
    delay = backoff_delay(0, base=1.0, cap=60.0, jitter_fraction=0.2)
    assert 1.0 <= delay <= 1.2


def test_backoff_delay_doubles_up_to_cap():
    delays = [backoff_delay(i, base=1.0, cap=60.0, jitter_fraction=0.0) for i in range(7)]
    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0]  # 2**6=64 already clamped to 60


def test_backoff_delay_never_exceeds_cap_plus_jitter_fraction():
    for attempt in range(20):
        delay = backoff_delay(attempt, base=1.0, cap=60.0, jitter_fraction=0.2)
        assert delay <= 60.0 * 1.2


def test_backoff_delay_jitter_is_never_negative():
    for attempt in range(10):
        assert backoff_delay(attempt, base=1.0, cap=60.0) >= min(60.0, 2 ** attempt)
