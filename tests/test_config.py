"""Tests for Settings._validate_ceilings model validator.

Covers ceiling bounds (five_hour_ceiling, seven_day_ceiling, opus_ceiling)
and max_workers validation, including env-var-driven construction.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from osbot.config import Settings


def test_default_settings_valid() -> None:
    """Settings() with all defaults should construct without error."""
    s = Settings()
    assert s.five_hour_ceiling == 0.60
    assert s.seven_day_ceiling == 0.50
    assert s.opus_ceiling == 0.40
    assert s.max_workers == 5


def test_five_hour_ceiling_zero_rejected() -> None:
    """five_hour_ceiling=0.0 is not in (0.0, 1.0] and must be rejected."""
    with pytest.raises(ValidationError) as exc_info:
        Settings(five_hour_ceiling=0.0)
    assert "five_hour_ceiling" in str(exc_info.value)


def test_five_hour_ceiling_above_one_rejected() -> None:
    """five_hour_ceiling=1.01 exceeds the upper bound and must be rejected."""
    with pytest.raises(ValidationError):
        Settings(five_hour_ceiling=1.01)


def test_five_hour_ceiling_negative_rejected() -> None:
    """Negative five_hour_ceiling must be rejected."""
    with pytest.raises(ValidationError):
        Settings(five_hour_ceiling=-0.1)


def test_five_hour_ceiling_exactly_one_accepted() -> None:
    """five_hour_ceiling=1.0 is on the inclusive upper bound and must be accepted."""
    s = Settings(five_hour_ceiling=1.0)
    assert s.five_hour_ceiling == 1.0


def test_seven_day_ceiling_out_of_range_rejected() -> None:
    """seven_day_ceiling=1.5 exceeds the upper bound and must be rejected."""
    with pytest.raises(ValidationError):
        Settings(seven_day_ceiling=1.5)


def test_opus_ceiling_out_of_range_rejected() -> None:
    """opus_ceiling=0.0 is not in (0.0, 1.0] and must be rejected."""
    with pytest.raises(ValidationError) as exc_info:
        Settings(opus_ceiling=0.0)
    assert "opus_ceiling" in str(exc_info.value)


def test_max_workers_zero_rejected() -> None:
    """max_workers=0 must be rejected with a message mentioning max_workers."""
    with pytest.raises(ValidationError) as exc_info:
        Settings(max_workers=0)
    assert "max_workers" in str(exc_info.value)


def test_max_workers_negative_rejected() -> None:
    """Negative max_workers must be rejected."""
    with pytest.raises(ValidationError):
        Settings(max_workers=-1)


def test_max_workers_one_accepted() -> None:
    """max_workers=1 is the minimum valid value and must be accepted."""
    s = Settings(max_workers=1)
    assert s.max_workers == 1


def test_env_var_override_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Validator must run on env-loaded values; OSBOT_FIVE_HOUR_CEILING=2.0 must raise."""
    monkeypatch.setenv("OSBOT_FIVE_HOUR_CEILING", "2.0")
    with pytest.raises(ValidationError):
        Settings()
