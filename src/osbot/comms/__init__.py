"""Communications -- comment generation, banned phrase enforcement, and webhook/email alerts.

Re-exports the main entry points for outgoing text and notifications.
"""

from osbot.comms.comments import generate_comment
from osbot.comms.email import send_email
from osbot.comms.phrases import BANNED_PHRASES, contains_banned, scrub_banned
from osbot.comms.blocker import notify_blocker
from osbot.comms.webhook import send_alert

__all__ = [
    "generate_comment",
    "BANNED_PHRASES",
    "contains_banned",
    "scrub_banned",
    "send_alert",
    "send_email",
    "notify_blocker",
]
