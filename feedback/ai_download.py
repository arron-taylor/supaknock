"""Curated model acquisition. Call only for an explicitly authorized admin job.

No inference library or Hub client is allowed to download implicitly. HTTP
redirects are rejected. LFS download actions are negotiated as data through the
official API, then checked against the compiled host allowlist before fetching.
The caller owns durable leases, staging cleanup, activation and encrypted tokens.
"""
from contextlib import contextmanager
import hashlib
import json
import math
import multiprocessing
from pathlib import Path
import time
from urllib.parse import urlsplit, quote

from feedback.webhooks import destination, PinnedHTTP


MODEL_ID = 'google/embeddinggemma-300m'
MODEL_REVISION = '57c266a740f537b4dc058e1b0cda161fd15afa75'
HUB = 'https://huggingface.co'
DOWNLOAD_HOSTS = frozenset({
    'huggingface.co', 'cdn-lfs.huggingface.co', 'cdn-lfs.hf.co',
    'cdn-lfs-us-1.hf.co', 'cdn-lfs-eu-1.hf.co', 'cas-bridge.xethub.hf.co',
    # Exact reviewed CDN edges: https://huggingface.co/.well-known/meta.json
    'us.aws.cdn.hf.co', 'us.gcp.cdn.hf.co',
})
CHUNK_SIZE = 65536
MAX_METADATA = 1024 * 1024
MAX_DOWNLOAD_SECONDS = 1800


class ModelDownloadError(Exception):
    """Only stable codes cross the worker/API boundary; never raw HTTP errors."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def model_manifest():
    data = json.loads(Path(__file__).with_name('ai_model_manifest.json').read_text())
    if data['model_id'] != MODEL_ID or data['revision'] != MODEL_REVISION:
        raise ModelDownloadError('invalid_manifest')
    return data


def _approved_url(url, hub_only=False):
    try:
        u = urlsplit(url)
        hosts = {'huggingface.co'} if hub_only else DOWNLOAD_HOSTS
        if (len(url) > 16384 or u.scheme != 'https' or u.hostname not in hosts
                or u.port not in (None, 443) or u.username is not None
                or u.password is not None or u.fragment
                or any(ord(c) <= 32 or ord(c) == 127 for c in url)):
            raise ValueError()
        return u
    except (TypeError, ValueError):
        raise ModelDownloadError('invalid_download_destination') from None


@contextmanager
def model_request(method, url, headers, body=None):
    _approved_url(url)
    try:
        # destination() validates every DNS answer; connect to the chosen public
        # address with certificate verification for the original hostname.
        # Signed CDN URLs can be long; validate syntax above and DNS separately.
        u = urlsplit(url)
        _, address, port = destination(f'https://{u.hostname}')
        conn = PinnedHTTP(u.hostname, address, port, True)
        conn.timeout = 15
        try:
            conn.request(method, (u.path or '/') + ('?' + u.query if u.query else ''), body, headers)
            response = conn.getresponse()
            yield response
        finally:
            conn.close()
    except ModelDownloadError:
        raise
    except Exception:
        raise ModelDownloadError('network_failed') from None


def _check_status(response):
    if response.status in (401, 403):
        raise ModelDownloadError('model_access_required')
    if 300 <= response.status < 400:
        raise ModelDownloadError('redirect_not_allowed')
    if response.status != 200:
        raise ModelDownloadError('download_http_failed')
    encoding = response.getheader('Content-Encoding', '').lower()
    if encoding not in ('', 'identity'):
        raise ModelDownloadError('unexpected_encoding')


def _checkpoint(cancelled, deadline):
    if cancelled and cancelled():
        raise ModelDownloadError('cancelled')
    if time.monotonic() >= deadline:
        raise ModelDownloadError('download_timeout')


def _read(response, limit, deadline, cancelled=None, consume=None):
    _check_status(response)
    length = response.getheader('Content-Length')
    if length is not None:
        try:
            if not 0 <= int(length) <= limit:
                raise ValueError()
        except (ValueError, TypeError):
            raise ModelDownloadError('download_too_large') from None
    total, parts = 0, []
    while True:
        _checkpoint(cancelled, deadline)
        block = response.read(min(CHUNK_SIZE, limit - total + 1))
        if not block:
            break
        total += len(block)
        if total > limit:
            raise ModelDownloadError('download_too_large')
        if consume:
            consume(block)
        else:
            parts.append(block)
    return total if consume else b''.join(parts)


def _lfs_actions(files, headers, request, deadline, cancelled):
    body = json.dumps({'operation': 'download', 'transfers': ['basic'],
                       'objects': [{'oid': x['sha256'], 'size': x['size']} for x in files]}).encode()
    with request('POST', f'{HUB}/{MODEL_ID}.git/info/lfs/objects/batch',
                 {**headers, 'Accept': 'application/vnd.git-lfs+json',
                  'Content-Type': 'application/vnd.git-lfs+json'}, body) as response:
        raw = _read(response, MAX_METADATA, deadline, cancelled)
    try:
        data = json.loads(raw)
        if data.get('transfer', 'basic') != 'basic' or len(data['objects']) != len(files):
            raise ValueError()
        expected = {x['sha256']: x for x in files}
        result = {}
        for obj in data['objects']:
            oid = obj['oid']
            if oid not in expected or oid in result or obj['size'] != expected[oid]['size'] or 'error' in obj:
                raise ValueError()
            action = obj['actions']['download']
            url = action['href']
            u = _approved_url(url)
            # Signed URLs carry their own restricted authorization. Never
            # forward the user's HF token to a CDN or accept arbitrary headers.
            extra = action.get('header') or {}
            if extra and (u.hostname != 'huggingface.co' or set(k.lower() for k in extra) != {'authorization'}):
                raise ValueError()
            result[oid] = url
        return result
    except ModelDownloadError:
        raise
    except (KeyError, ValueError, TypeError, AttributeError):
        raise ModelDownloadError('invalid_download_manifest') from None


def verify_model(directory, *, manifest=None, cancelled=None):
    """Hash regular installed files, without following symlinks or using network."""
    directory = Path(directory)
    manifest = model_manifest() if manifest is None else manifest
    total = 0
    for item in manifest['files']:
        path = directory / item['path']
        if path.is_symlink() or any(p.is_symlink() for p in path.parents if p != directory.parent):
            raise ModelDownloadError('invalid_model_file')
        if not path.is_file() or path.stat().st_size != item['size']:
            raise ModelDownloadError('model_file_missing')
        h = hashlib.sha256()
        with path.open('rb') as source:
            while block := source.read(CHUNK_SIZE):
                if cancelled and cancelled():
                    raise ModelDownloadError('cancelled')
                h.update(block)
        if h.hexdigest() != item['sha256']:
            raise ModelDownloadError('model_hash_mismatch')
        total += item['size']
    return {'model_id': manifest['model_id'], 'revision': manifest['revision'],
            'bytes': total, 'files': len(manifest['files']), 'profile': manifest['profile']}


def download_model(directory, token, *, request=model_request, progress=None,
                   cancelled=None, manifest=None, timeout=MAX_DOWNLOAD_SECONDS):
    """Write a new staging directory; caller must verify inference before activation.

    `manifest`/`request` are internal test seams, never user-configurable inputs.
    The caller has to fence progress/activation against the durable admin job.
    Failure leaves owned staging files for explicit cancellation/recovery.
    """
    if not isinstance(token, str) or not 1 <= len(token) <= 2048 or any(ord(c) <= 32 or ord(c) > 126 for c in token):
        raise ModelDownloadError('model_access_required')
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= MAX_DOWNLOAD_SECONDS:
        raise ModelDownloadError('invalid_download_limits')
    manifest = model_manifest() if manifest is None else manifest
    directory = Path(directory)
    if directory.exists() or directory.is_symlink():
        raise ModelDownloadError('staging_exists')
    deadline = time.monotonic() + timeout
    _checkpoint(cancelled, deadline)
    headers = {'Authorization': f'Bearer {token}', 'User-Agent': 'SuggestionBox/0.2',
               'Accept-Encoding': 'identity'}
    files = manifest['files']
    lfs = [x for x in files if x['lfs']]
    actions = _lfs_actions(lfs, headers, request, deadline, cancelled) if lfs else {}
    directory.mkdir(parents=True, mode=0o700)
    completed, total = 0, sum(x['size'] for x in files)
    for item in files:
        _checkpoint(cancelled, deadline)
        path = directory / item['path']
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        url = actions[item['sha256']] if item['lfs'] else f'{HUB}/{MODEL_ID}/raw/{MODEL_REVISION}/{quote(item["path"], safe="/")}'
        u = _approved_url(url)
        fetch_headers = dict(headers)
        if u.hostname != 'huggingface.co':
            fetch_headers.pop('Authorization')
        hasher, received = hashlib.sha256(), 0
        with path.open('xb') as target:
            def consume(block):
                nonlocal received
                target.write(block)
                hasher.update(block)
                received += len(block)
                if progress:
                    progress(completed + received, total)
            with request('GET', url, fetch_headers) as response:
                _read(response, item['size'], deadline, cancelled, consume)
            target.flush()
            import os
            os.fsync(target.fileno())
        if received != item['size'] or hasher.hexdigest() != item['sha256']:
            raise ModelDownloadError('model_hash_mismatch')
        completed += received
    return verify_model(directory, manifest=manifest, cancelled=cancelled)


def _download_child(connection, directory, token, timeout):
    # This process bounds DNS, response headers, slow-drip bodies and hashing.
    # No exception traceback, request header or signed URL crosses the boundary.
    try:
        last = 0

        def progress(completed, total):
            nonlocal last
            now = time.monotonic()
            if now - last >= .25 or completed == total:
                connection.send(('progress', completed, total))
                last = now

        result = download_model(directory, token, timeout=timeout, progress=progress)
        connection.send(('ready', result))
    except ModelDownloadError as exc:
        connection.send(('error', exc.code))
    except Exception:
        connection.send(('error', 'download_failed'))
    finally:
        connection.close()


def supervised_download(directory, token, *, progress=None, cancelled=None,
                        timeout=MAX_DOWNLOAD_SECONDS, _target=_download_child):
    """Run acquisition in a killable process with an absolute wall-time bound.

    Call off the ASGI loop. `_target` is solely a protocol-test seam. The caller
    still owns durable authorization/lease fencing, staging and activation.
    """
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= MAX_DOWNLOAD_SECONDS:
        raise ModelDownloadError('invalid_download_limits')
    deadline = time.monotonic() + timeout
    _checkpoint(cancelled, deadline)
    ctx = multiprocessing.get_context('spawn')
    reader, writer = ctx.Pipe(duplex=False)
    process = ctx.Process(target=_target, args=(writer, str(directory), token, timeout), daemon=True)
    try:
        process.start()
        writer.close()
        while True:
            _checkpoint(cancelled, deadline)
            if reader.poll(min(.1, max(0, deadline - time.monotonic()))):
                try:
                    message = reader.recv()
                except EOFError:
                    raise ModelDownloadError('download_failed') from None
                _checkpoint(cancelled, deadline)
                if message[0] == 'progress' and len(message) == 3:
                    if progress:
                        progress(message[1], message[2])
                elif message[0] == 'ready' and len(message) == 2:
                    return message[1]
                elif message[0] == 'error' and len(message) == 2:
                    raise ModelDownloadError(message[1])
                else:
                    raise ModelDownloadError('download_failed')
            elif not process.is_alive():
                raise ModelDownloadError('download_failed')
    finally:
        writer.close()
        reader.close()
        if process.pid is not None:
            if process.is_alive():
                process.terminate()
            process.join(timeout=1)
            if process.is_alive():
                process.kill()
                process.join(timeout=1)
            process.close()
