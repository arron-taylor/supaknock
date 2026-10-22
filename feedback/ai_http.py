"""Shared native-administrator boundary for optional AI administration."""
from typing import Annotated

from fastapi import Depends, Request

from feedback.domain import Actor
from feedback.errors import Error


def admin_dependency(actor):
    def administrator(request: Request, current: Annotated[Actor, Depends(actor)]):
        if request.headers.get('X-Chat-Token'):
            raise Error(400, 'ambiguous_auth', 'Use one authentication method')
        session = getattr(request.state, 'session', None)
        if current.role != 'admin' or not current.user_id or current.key_id or not session:
            raise Error(403, 'forbidden', 'Administrator access required')
        origin = request.headers.get('Origin')
        if origin is not None and origin != request.app.state.config.base_url:
            raise Error(403, 'forbidden', 'Use the installation administration page')
        # Storage rechecks this session and role in each sensitive transaction.
        return {'kind': 'admin', 'user_id': current.user_id, 'session_id': session['session_id']}
    return administrator
