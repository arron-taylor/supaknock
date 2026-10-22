"""Simple form definitions and private submissions; no customer identity is inferred."""
import json
import re
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from feedback.errors import Error


class FormInput(BaseModel):
    model_config = ConfigDict(extra='forbid')


class FormField(FormInput):
    key: str = Field(pattern=r'^[a-z][a-z0-9_]{0,39}$')
    label: str = Field(min_length=1, max_length=100)
    type: Literal['text', 'textarea', 'email', 'choice'] = 'text'
    required: bool = False
    choices: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode='after')
    def valid(self):
        if not self.label.strip() or '\x00' in self.label:
            raise ValueError('Give the field a label')
        if self.type == 'choice':
            if not self.choices or len(set(self.choices)) != len(self.choices) or any(not x.strip() or len(x)>100 or '\x00' in x for x in self.choices):
                raise ValueError('Use 1–30 distinct choices of 1–100 characters')
        elif self.choices:
            raise ValueError('Only choice fields have choices')
        return self


class FormDefinition(FormInput):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default='', max_length=2000)
    enabled: bool = False
    public_enabled: bool = False
    embed_enabled: bool = False
    embed_origins: list[str] = Field(default_factory=list, max_length=20, description='Exact HTTPS website origins allowed to embed this form; localhost HTTP is allowed for development.')
    fields: list[FormField] = Field(min_length=1, max_length=20)

    @field_validator('embed_origins')
    @classmethod
    def valid_embed_origins(cls, value):
        from feedback.chat import valid_origin
        if any(len(origin)>2048 or any(ord(char)<33 for char in origin) for origin in value):
            raise ValueError('Use exact website origins without spaces or control characters')
        try:
            return sorted(set(valid_origin(origin) for origin in value))
        except Error as exc:
            raise ValueError(exc.message) from None

    @model_validator(mode='after')
    def valid(self):
        if not self.name.strip() or '\x00' in self.name + self.description:
            raise ValueError('Use a nonempty name and plain text')
        if len({f.key for f in self.fields}) != len(self.fields):
            raise ValueError('Each field needs a unique key')
        return self


class FormEdit(FormDefinition):
    version: int = Field(ge=1, description='Concurrent-edit guard; there is only one saved form definition.')


class FormSubmissionInput(FormInput):
    client_request_id: str = Field(pattern=r'^[a-zA-Z0-9_-]{16,100}$', description='Generate once per submission and reuse for retries, including after token rotation.')
    answers: dict[str, str] = Field(max_length=20)

    @field_validator('answers')
    @classmethod
    def bounded(cls, value):
        if any(len(k)>40 or len(v)>4000 or '\x00' in k+v for k,v in value.items()) or len(json.dumps(value, ensure_ascii=False).encode())>32768:
            raise ValueError('Answers exceed the form submission limits')
        return value


def validate_answers(fields, answers):
    keys = {f['key'] for f in fields}
    if set(answers) - keys:
        raise Error(400, 'invalid_answers', 'Remove answers for fields that are not in this form')
    for field in fields:
        value = answers.get(field['key'], '')
        if not value.strip():
            if field['required']:
                raise Error(400, 'invalid_answers', 'Complete the required field: ' + field['key'])
            continue
        limit = 4000 if field['type']=='textarea' else 254 if field['type']=='email' else 500
        if len(value)>limit:
            raise Error(400, 'invalid_answers', 'Answer is too long: ' + field['key'])
        if field['type']=='email' and not re.fullmatch(r'[^\s@]+@[^\s@]+', value):
            raise Error(400, 'invalid_answers', 'Use a valid email address for: ' + field['key'])
        if field['type']=='choice' and value not in field['choices']:
            raise Error(400, 'invalid_answers', 'Choose an available option for: ' + field['key'])


class FormsService:
    def __init__(self, storage): self.storage = storage

    @staticmethod
    def admin(principal):
        if principal.get('kind') != 'admin' or not principal.get('user_id') or not principal.get('session_id'):
            raise Error(403, 'forbidden', 'Administrator access required')

    def list(self, principal, project, limit, offset):
        self.admin(principal)
        return self.storage.forms_list(principal, project, limit, offset)

    def read(self, principal, form_id):
        self.admin(principal)
        return self.storage.forms_read(principal, form_id)

    def save(self, principal, project, form_id, data):
        self.admin(principal)
        return self.storage.forms_save(principal, project, form_id, data)

    def submit(self, actor, form_id, data):
        if not actor.key_id or 'forms:submit' not in actor.permissions:
            raise Error(403, 'forbidden', 'Use a backend project token with forms:submit permission')
        return self.storage.forms_submit(actor, form_id, data)

    def public_read(self, form_id, embed_origin=None):
        return self.storage.forms_public_read(form_id, embed_origin)

    def public_submit(self, form_id, data, version, client_bucket, embed_origin=None):
        return self.storage.forms_public_submit(form_id, data, version, client_bucket, embed_origin)

    def submissions(self, principal, form_id, state, limit, offset):
        self.admin(principal)
        return self.storage.form_submissions(principal, form_id, state, limit, offset)

    def submission(self, principal, submission_id):
        self.admin(principal)
        return self.storage.form_submission(principal, submission_id)

    def review(self, principal, submission_id, version, state):
        self.admin(principal)
        return self.storage.form_submission_review(principal, submission_id, version, state)

    def delete(self, principal, submission_id, version):
        self.admin(principal)
        return self.storage.form_submission_delete(principal, submission_id, version)
