# voiceapp/management/commands/db_manage.py
from asgiref.sync import sync_to_async
from voiceapp.models import Conversation, Message

def _create():
    return str(Conversation.objects.create().id)

def _save(convo_id, role, content):
    if content.strip():
        try:
            convo = Conversation.objects.get(id=convo_id)
            Message.objects.create(
                conversation=convo,
                role='user' if role == 'user' else 'assistant',
                content=content.strip()
            )
        except: pass  # noqa: E701, E722

def _summary(convo_id):
    try:
        convo = Conversation.objects.get(id=convo_id)
        msgs = list(convo.messages.order_by('-timestamp')[:6])
        msgs.reverse()
        lines = [f"{'User' if m.role=='user' else 'Assistant'}: {m.content}" for m in msgs]
        return "Previous conversation:\n" + "\n".join(lines) if lines else "No prior conversation."
    except:  # noqa: E722
        return "No prior conversation."

def _latest():
    latest = Conversation.objects.order_by('-created_at').first()
    return str(latest.id) if latest else _create()

# Async wrappers
create_new_conversation = sync_to_async(_create)
save_message_to_db = sync_to_async(_save)
get_conversation_summary = sync_to_async(_summary)
get_or_create_latest_conversation = sync_to_async(_latest)