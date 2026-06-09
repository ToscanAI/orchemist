"""Tests for the canonical ``now_utc()`` timestamp helper (issue #932 item 2).

These guard the single seam that the tz-awareness sweep unified ~113 source
sites + the 5 deprecated ``datetime.utcnow()`` call sites onto:

  * ``now_utc()`` must return a timezone-aware UTC datetime, so every swept
    site produces consistent ``+00:00`` timestamps and the Py3.12
    ``datetime.utcnow()`` DeprecationWarning is retired.
  * The re-tag idiom applied at the 5 RISK sites (daemon/cli parsed stored
    strings, schemas/recovery ``opened_at`` fields, progress elapsed math)
    must let a previously-naive value subtract against ``now_utc()`` without
    raising ``TypeError: can't subtract offset-naive and offset-aware``.
"""

from datetime import datetime, timedelta, timezone

from orchestration_engine.timestamps import now_utc


def test_now_utc_is_timezone_aware_utc():
    dt = now_utc()
    assert dt.tzinfo is not None                       # aware, not naive
    assert dt.utcoffset() == timedelta(0)              # zero offset == UTC
    assert dt.isoformat().endswith("+00:00")           # serializes as aware UTC


def test_retag_naive_stored_value_subtracts_without_error():
    """Regression: a naive stored datetime, re-tagged, subtracts against
    ``now_utc()`` without TypeError (the B1/B5 daemon/cli re-tag path)."""
    naive_started = datetime(2026, 1, 1, 0, 0, 0)       # simulates an old naive-stored started_at
    assert naive_started.tzinfo is None
    retagged = naive_started.replace(tzinfo=timezone.utc)
    elapsed = (now_utc() - retagged).total_seconds()    # must NOT raise
    assert elapsed > 0


def test_two_now_utc_values_are_comparable_and_monotonic():
    """Two aware values produced by the helper compare/subtract cleanly
    (the internal-consistent B6/B7/B10 pair contract)."""
    first = now_utc()
    second = now_utc()
    assert second >= first                              # no aware-vs-naive TypeError
    assert (second - first).total_seconds() >= 0
