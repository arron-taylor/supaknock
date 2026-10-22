from dataclasses import dataclass
from datetime import datetime, timezone
import uuid

STATUSES = ("under_consideration", "planned", "in_progress", "shipped")
EVENTS = ("request.created", "request.updated", "request.resubmitted", "request.accepted",
          "request.rejected", "request.status_changed", "request.deleted", "chat.message_created", "chat.support_mode_changed")
PERMISSIONS = ("read", "participate", "audience:read", "manage", "forms:submit")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def utc_timestamp(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_id():
    return str(uuid.uuid4())


@dataclass(frozen=True)
class Actor:
    user_id: str | None = None
    role: str = "anonymous"
    project_id: str | None = None
    key_id: str | None = None
    permissions: tuple = ()
    external_id: str | None = None
    # Transport-observed metadata for private administration, never actor payloads.
    ip_address: str | None = None

    def manages(self, project):
        return self.role == "admin" or (self.project_id == project and "manage" in self.permissions)

    def can(self, project, permission):
        return self.role == "admin" or (self.project_id == project and permission in self.permissions)

    def record(self):
        return {"user_id": self.user_id, "api_key_id": self.key_id, "kind": "integration" if self.key_id else "native"}
