"""Safety layer -- domain enforcement, circuit breakers, anti-spam.

All safety checks run BEFORE any Claude call.
"""

from osbot.safety.circuit_breaker import (
    ban_repo,
    can_attempt_repo,
    record_failure,
    record_timeout,
)
from osbot.safety.domain import has_ai_policy, is_in_domain

__all__ = [
    "ban_repo",
    "can_attempt_repo",
    "has_ai_policy",
    "is_in_domain",
    "record_failure",
    "record_timeout",
]
