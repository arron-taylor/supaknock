"""Administrator forms and server-to-server submission transport."""
import hashlib
import hmac
import secrets
import time
from pathlib import Path
from fastapi.responses import FileResponse
from feedback.client_ip import client_ip
from typing import Annotated, Literal
from uuid import UUID
from fastapi import Depends, Query, Request, Response
from pydantic import BaseModel, Field
from feedback.ai_http import admin_dependency
from feedback.domain import Actor
from feedback.errors import Error
from feedback.forms import FormDefinition, FormEdit, FormField, FormInput, FormSubmissionInput, FormsService
from feedback.outputs import PageOutput


class FormOutput(FormDefinition):
    id: str
    project_id: str
    project_name: str
    project_archived_at: str | None
    version: int
    created_at: str
    updated_at: str


class FormReceipt(BaseModel):
    id: str
    created_at: str


class SubmissionOutput(FormReceipt):
    form_id: str
    form_name: str
    project_id: str
    project_name: str
    answers: dict[str,str]
    fields: list[FormField]
    state: Literal['new','reviewed','archived']
    version: int
    updated_at: str


class SubmissionVersion(FormInput):
    version: int = Field(ge=1)


class SubmissionReview(SubmissionVersion):
    state: Literal['new','reviewed','archived']


class PublicFormOutput(BaseModel):
    id: str
    name: str
    description: str
    project_name: str
    fields: list[FormField]
    ticket: str


class PublicSubmission(FormSubmissionInput):
    ticket: str = Field(max_length=256)
    website: str = Field(default='', max_length=200, description='Honeypot; leave empty.')


class PublicReceipt(BaseModel):
    ok: bool


def install_forms(app, storage, actor, config):
    service = FormsService(storage)
    app.state.forms = service
    Admin = Annotated[dict, Depends(admin_dependency(actor))]
    Current = Annotated[Actor, Depends(actor)]
    Limit = Annotated[int, Query(ge=1, le=100)]
    Offset = Annotated[int, Query(ge=0, le=1000000)]

    def sign(value):
        return hmac.new(config.secret.encode(), ('public-form:'+value).encode(), hashlib.sha256).hexdigest()

    def ticket(fid,version,audience=''):
        payload = f'{version}.{int(time.time())}.{secrets.token_hex(16)}'
        return payload+'.'+sign(fid+audience+':'+payload)

    def verify_ticket(fid,value,audience=''):
        try:
            version,issued,nonce,signature = value.split('.')
            payload = '.'.join((version,issued,nonce))
            if not hmac.compare_digest(signature,sign(fid+audience+':'+payload)) or len(nonce)!=32 or not 0<=time.time()-int(issued)<7200:
                raise ValueError()
            return int(version),nonce
        except (ValueError,TypeError):
            raise Error(403,'form_expired','This form has expired. Reload the page and try again.') from None

    @app.get('/form-widget.js', include_in_schema=False)
    def form_widget():
        return FileResponse(Path(__file__).parent/'web'/'form-widget.js',media_type='application/javascript',headers={'Cache-Control':'no-cache','Access-Control-Allow-Origin':'*'})

    @app.get('/form-widget.css', include_in_schema=False)
    def form_widget_css():
        return FileResponse(Path(__file__).parent/'web'/'form-widget.css',media_type='text/css',headers={'Cache-Control':'no-cache','Access-Control-Allow-Origin':'*'})

    def embed_origin(request):
        # Embed intake has no account authority and never accepts credentials.
        if request.headers.get('authorization') or request.headers.get('cookie'):
            raise Error(403,'credentials_not_allowed','Form embeds must omit credentials')
        origin = request.headers.get('origin','')
        if not origin or origin=='null':
            raise Error(403,'origin_required','Embed this form on an enabled website')
        return origin

    @app.get('/api/v1/embed/forms/{form_id}', tags=['Forms'], response_model=PublicFormOutput)
    def embed_definition(form_id: UUID, request: Request, response: Response):
        """Read an enabled embed from an exact allowed Origin. No cookies or API credentials. Returns a two-hour ticket bound to this form, origin and current definition."""
        origin=embed_origin(request)
        data=service.public_read(str(form_id),origin)
        response.headers['X-Robots-Tag']='noindex, nofollow'
        return {**data,'ticket':ticket(str(form_id),data['version'],':embed:'+origin)}

    @app.post('/api/v1/embed/forms/{form_id}/submissions', tags=['Forms'], response_model=PublicReceipt, status_code=201)
    def embed_submit(form_id: UUID, body: PublicSubmission, request: Request):
        """Submit from an allowed website with its current origin-bound ticket. Only returns ok. Shares public-page spam limits and private inbox; retry with the same ticket, request ID and answers. No account is created."""
        origin=embed_origin(request)
        version,nonce=verify_ticket(str(form_id),body.ticket,':embed:'+origin)
        if body.website:
            service.public_read(str(form_id),origin)
            return {'ok':True}
        request_id='embed_'+sign(str(form_id)+':'+origin+':'+nonce+':'+body.client_request_id)
        return service.public_submit(str(form_id),{'client_request_id':request_id,'answers':body.answers},version,sign('client:'+str(client_ip(request))),origin)

    @app.get('/forms/{form_id}', include_in_schema=False)
    def public_page(form_id: UUID):
        status = 200
        try: service.public_read(str(form_id))
        except Error as exc:
            if exc.status != 404: raise
            status = 404
        return FileResponse(Path(__file__).parent/'web'/'public-form.html',status_code=status,headers={'Cache-Control':'no-store','X-Robots-Tag':'noindex, nofollow','Referrer-Policy':'no-referrer'})

    @app.get('/api/v1/public/forms/{form_id}', tags=['Forms'], response_model=PublicFormOutput)
    def public_definition(form_id: UUID, response: Response):
        """Read an explicitly enabled public form and receive a short-lived submission ticket. No catalog or answers are exposed."""
        data = service.public_read(str(form_id))
        response.headers['X-Robots-Tag']='noindex, nofollow'
        return {**data,'ticket':ticket(str(form_id),data['version'])}

    @app.post('/api/v1/public/forms/{form_id}/submissions', tags=['Forms'], response_model=PublicReceipt, status_code=201)
    def public_submit(form_id: UUID, body: PublicSubmission, request: Request, response: Response):
        """Anonymous hosted-page intake, only while public access and submissions are enabled. Requires the installation Origin and a current two-hour ticket. Answers remain administrator-only. Retry the same ticket, request ID and answers; no account or receipt lookup is granted."""
        if request.headers.get('origin') != config.base_url or request.headers.get('sec-fetch-site') not in (None,'same-origin'):
            raise Error(403,'origin_required','Submit this form from its own page')
        version,nonce = verify_ticket(str(form_id),body.ticket)
        if body.website:
            service.public_read(str(form_id))
            return {'ok':True}
        # Separate public retry IDs from trusted backend IDs; never store the ticket or IP.
        request_id = 'public_'+sign(str(form_id)+':'+nonce+':'+body.client_request_id)
        return service.public_submit(str(form_id),{'client_request_id':request_id,'answers':body.answers},version,sign('client:'+str(client_ip(request))))

    @app.get('/api/v1/admin/forms', tags=['Forms'], response_model=PageOutput[FormOutput])
    def forms(p: Admin, project_id: UUID | None = None, limit: Limit = 25, offset: Offset = 0):
        return service.list(p, str(project_id) if project_id else None, limit, offset)

    @app.post('/api/v1/admin/projects/{project}/forms', tags=['Forms'], status_code=201, response_model=FormOutput)
    def create(project: str, body: FormDefinition, p: Admin):
        return service.save(p, project, None, body.model_dump())

    @app.get('/api/v1/admin/forms/{form_id}', tags=['Forms'], response_model=FormOutput)
    def read(form_id: UUID, p: Admin):
        return service.read(p, str(form_id))

    @app.put('/api/v1/admin/forms/{form_id}', tags=['Forms'], response_model=FormOutput)
    def save(form_id: UUID, body: FormEdit, p: Admin):
        """Save the single current definition immediately. Old answer values are unchanged; no schema history is maintained."""
        return service.save(p, None, str(form_id), body.model_dump())

    @app.post('/api/v1/forms/{form_id}/submissions', tags=['Forms'], status_code=201, response_model=FormReceipt)
    def submit(form_id: UUID, body: FormSubmissionInput, request: Request, a: Current):
        """Backend only, with a project token granting forms:submit. Never place this token in a website or app bundle.

        Reuse client_request_id and exactly the same answers on retry. Tokens cannot read entries.
        Email is unverified contact information, not identity. Browser Origin/Fetch-Metadata requests are rejected.
        """
        if request.headers.get('origin') is not None or request.headers.get('sec-fetch-site') is not None:
            raise Error(403,'backend_required','Submit from your backend, not a browser')
        return service.submit(a, str(form_id), body.model_dump())

    @app.get('/api/v1/admin/forms/{form_id}/submissions', tags=['Forms'], response_model=PageOutput[SubmissionOutput])
    def submissions(form_id: UUID, p: Admin, state: Literal['new','reviewed','archived'] | None = None, limit: Limit = 25, offset: Offset = 0):
        return service.submissions(p, str(form_id), state, limit, offset)

    @app.get('/api/v1/admin/form-submissions/{submission_id}', tags=['Forms'], response_model=SubmissionOutput)
    def submission(submission_id: UUID, p: Admin):
        return service.submission(p, str(submission_id))

    @app.put('/api/v1/admin/form-submissions/{submission_id}/state', tags=['Forms'], response_model=SubmissionOutput)
    def review(submission_id: UUID, body: SubmissionReview, p: Admin):
        return service.review(p, str(submission_id), body.version, body.state)

    @app.delete('/api/v1/admin/form-submissions/{submission_id}', tags=['Forms'], status_code=204)
    def delete(submission_id: UUID, body: SubmissionVersion, p: Admin):
        service.delete(p, str(submission_id), body.version)
        return Response(status_code=204)
