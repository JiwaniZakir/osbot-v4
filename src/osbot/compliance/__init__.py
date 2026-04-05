"""Compliance -- CLA checking and assignment detection.

Re-exports the main entry points for compliance checks.
"""

from osbot.compliance.assignment import requires_assignment
from osbot.compliance.cla import check_cla

__all__ = ["check_cla", "requires_assignment"]
