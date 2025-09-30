# voiceapp/db_helpers.py

from __future__ import annotations

from typing import List, Tuple, Optional
from asgiref.sync import sync_to_async
from django.db.models import QuerySet

from .models import Conversation, Message
# ----------------------------
# Low-level SYNC implementations
# ----------------------------

def _save_message_sync(conversation_id: str | int, role: str, content: str) -> None:
    """
    Create a Message row if content is non-empty.
    role: "user" or "assistant"
    """
    content = (content or "").strip()
    if not content:
        return

    conv = Conversation.objects.get(id=conversation_id)
    Message.objects.create(
        conversation=conv,
        role="user" if role == "user" else "assistant",
        content=content,
    )


def _get_history_sync(conversation_id: str | int, limit: int = 6) -> str:
    """
    Return a compact text history (last `limit` messages, oldest -> newest).
    """
    qs: QuerySet[Message] = (
        Message.objects.filter(conversation_id=conversation_id)
        .order_by("-timestamp")[:limit]
    )
    items = list(reversed(qs))
    if not items:
        return "No prior conversation."
    lines = [
        f"{'User' if m.role == 'user' else 'Assistant'}: {m.content}"
        for m in items
    ]
    return "Previous conversation:\n" + "\n".join(lines)


def _get_latest_conversation_id_sync() -> str:
    """
    Return latest Conversation id; create one if none exists.
    """
    if Conversation.objects.exists():
        return str(Conversation.objects.order_by("-created_at").first().id)
    return str(Conversation.objects.create().id)


def _list_recent_conversations_sync(limit: int = 10) -> List[Tuple[str, str]]:
    """
    Return (id, created_at.isoformat()) for recent conversations.
    """
    rows = Conversation.objects.order_by("-created_at")[:limit]
    return [(str(c.id), c.created_at.isoformat()) for c in rows]


def _db_health_check_sync() -> bool:
    """
    Quick DB connectivity check (minimal query).
    """
    # Any trivial query that hits the DB:
    _ = Conversation.objects.order_by("id").first()
    return True

save_message = sync_to_async(_save_message_sync)
get_history = sync_to_async(_get_history_sync)
get_latest_conversation_id = sync_to_async(_get_latest_conversation_id_sync)
list_recent_conversations = sync_to_async(_list_recent_conversations_sync)
db_health_check = sync_to_async(_db_health_check_sync)


# getlist  -> recent conversations (ids + created_at)
getlist = list_recent_conversations

# gettest  -> simple DB health check
gettest = db_health_check

# getlatest -> latest conversation id (creates if empty)
getlatest = get_latest_conversation_id

# gethistory -> history string for a conversation
gethistory = get_history

# getsave_message -> save a message row
getsave_message = save_message


__all__ = [
    # clear names
    "save_message",
    "get_history",
    "get_latest_conversation_id",
    "list_recent_conversations",
    "db_health_check",
    # aliases you requested
    "getlist",
    "gettest",
    "getlatest",
    "gethistory",
    "getsave_message",
]