"""Project knowledge policy. Imported text is untrusted data, never instructions."""
from feedback.errors import Error

MAX_MARKDOWN_BYTES = 65536


def validate_markdown(filename, name, content):
    if not isinstance(filename, str) or not 1 <= len(filename) <= 255 or '/' in filename or '\\' in filename or any(ord(ch) < 32 or ord(ch) == 127 for ch in filename) or not filename.casefold().endswith(('.md', '.markdown')):
        raise Error(400, 'invalid_input', 'Choose a Markdown file ending in .md or .markdown')
    if not isinstance(name, str) or not name.strip() or len(name) > 300:
        raise Error(400, 'invalid_input', 'Give this source a name of at most 300 characters')
    if not isinstance(content, str):
        raise Error(400, 'invalid_input', 'Supply UTF-8 Markdown text')
    try:
        encoded = content.encode('utf-8')
        filename.encode('utf-8')
        name.encode('utf-8')
    except UnicodeError:
        raise Error(400, 'invalid_input', 'Supply valid UTF-8 Markdown text') from None
    if len(encoded) > MAX_MARKDOWN_BYTES:
        raise Error(413, 'document_too_large', 'Markdown documents can contain at most 64 KiB of UTF-8 text')
    if not content.strip('\ufeff \t\r\n') or any(ord(ch) < 32 and ch not in '\t\r\n' for ch in content):
        raise Error(400, 'invalid_input', 'Supply a nonempty Markdown document without binary content')


class KnowledgeService:
    def __init__(self, storage):
        self.storage = storage

    @staticmethod
    def authorize(principal):
        if not isinstance(principal, dict) or principal.get('kind') != 'admin' or not principal.get('user_id') or not principal.get('session_id'):
            raise Error(403, 'forbidden', 'Native administrator access required')

    def library(self, project, principal, query='', source_id=None, limit=25, offset=0, item_id=None):
        self.authorize(principal)
        return self.storage.ai_knowledge_library(project, principal, query, source_id, limit, offset, item_id)

    def sources(self, project, principal, limit=25, offset=0):
        self.authorize(principal)
        return self.storage.ai_knowledge_sources(project, principal, limit, offset)

    def source(self, project, principal, source_id):
        self.authorize(principal)
        return self.storage.ai_knowledge_source(project, principal, source_id)

    def import_markdown(self, project, principal, client_id, filename, name, content, *, source_id=None, version=None):
        self.authorize(principal)
        validate_markdown(filename, name, content)
        return self.storage.ai_knowledge_import(project, principal, client_id, filename, name, content, source_id=source_id, version=version)

    def documents(self, project, principal, source_id, limit=25, offset=0):
        self.authorize(principal)
        return self.storage.ai_knowledge_documents(project, principal, source_id, limit, offset)

    def document(self, project, principal, source_id, document_id):
        self.authorize(principal)
        return self.storage.ai_knowledge_document(project, principal, source_id, document_id)

    def change(self, project, principal, source_id, version, active, approved):
        self.authorize(principal)
        return self.storage.ai_knowledge_change(project, principal, source_id, version, active=active, approved=approved)

    def delete(self, project, principal, source_id, version):
        self.authorize(principal)
        return self.storage.ai_knowledge_change(project, principal, source_id, version, active=False, approved=False, delete=True)
