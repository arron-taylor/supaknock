"""Installation-wide administrator inbox, independent of HTTP authentication transport."""
from feedback.domain import Actor
from feedback.errors import Error
from feedback.services import require_admin
from feedback.storage.base import Storage


class NotificationService:
    def __init__(self, storage: Storage):
        self.storage = storage

    @staticmethod
    def _authorize(actor: Actor, session_id: str | None):
        require_admin(actor)
        if actor.key_id or not actor.user_id or not session_id:
            raise Error(403, 'forbidden', 'A native administrator session is required')

    def list(self, actor: Actor, session_id: str | None, unread_only: bool, limit: int, offset: int):
        self._authorize(actor, session_id)
        return self.storage.notifications(actor.user_id, session_id, unread_only, limit, offset)

    def summary(self, actor: Actor, session_id: str | None):
        self._authorize(actor, session_id)
        return self.storage.notification_summary(actor.user_id, session_id)

    def set_read(self, actor: Actor, session_id: str | None, notification_id: str, read: bool):
        self._authorize(actor, session_id)
        return self.storage.set_notification_read(actor.user_id, session_id, notification_id, read)

    def read_all(self, actor: Actor, session_id: str | None, through_sequence: int):
        self._authorize(actor, session_id)
        return self.storage.read_all_notifications(actor.user_id, session_id, through_sequence)
