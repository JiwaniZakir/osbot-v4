"""osbot.intel -- shared intelligence-gathering utilities.

Re-exports the public API: GraphQLClient, analyze_codebase, read_policy,
detect_duplicates.  Layer 2 -- depends on state, not on gateway or higher.
"""

from osbot.intel.codebase import analyze_codebase
from osbot.intel.duplicates import detect_duplicates
from osbot.intel.graphql import GraphQLClient
from osbot.intel.policy import read_policy

__all__ = [
    "GraphQLClient",
    "analyze_codebase",
    "detect_duplicates",
    "read_policy",
]
