"""AI disclosure (EU AI Act Article 50): people must be told they are dealing with an AI.

The first message the bot sends a member in a conversation starts with a line saying it
is an AI assistant. Discord slash commands have no sessions, so a "conversation" is a run
of messages with no gap longer than `new_conversation_after_hours`; after that the line is
shown again. The state lives in SQLite, so a restart doesn't make the bot forget.

The line can only be removed with `removed_by_written_request` (a reference to the
client's written request: date and who asked). A blank text falls back to the default."""


def disclosure_line(settings) -> str:
    """The text to show, or "" only when its removal is on written record."""
    if (settings.ai_disclosure_removed_by_written_request or "").strip():
        return ""
    text = (settings.ai_disclosure or "").strip()
    return text or f"I'm an AI assistant for {settings.community_name}."


class Disclosure:
    def __init__(self, settings, store, clock=None):
        self.s, self.store = settings, store
        self.clock = clock or store.clock

    def opening(self, user_id: str) -> str:
        """The line plus a blank line if this message starts a conversation, else ""."""
        line = disclosure_line(self.s)
        if not line:
            return ""
        state = self.store.ai_disclosure_state(user_id)
        gap = self.s.ai_new_conversation_after_hours * 3600
        if state and state["disclosed_at"] and self.clock() - (state["last_message_at"] or 0) < gap:
            return ""
        return line + "\n\n"

    def sent(self, user_id: str, opening: str):
        """Call BEFORE awaiting the send, so a second message that arrives while the first is
        still being delivered doesn't repeat the line. If the send fails, call `unsent`."""
        self.store.note_ai_message(user_id, disclosed=bool(opening))

    def unsent(self, user_id: str, opening: str):
        """The message carrying the line was not delivered: show it on the next message."""
        if opening:
            self.store.forget_ai_disclosure(user_id)
