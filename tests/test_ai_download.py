from contextlib import contextmanager
import hashlib
import io
import json
from pathlib import Path
import socket
from unittest.mock import patch

import pytest

from feedback.ai_download import (MODEL_ID, MODEL_REVISION, ModelDownloadError,
                                  download_model, model_manifest, model_request,
                                  verify_model)


class Response:
    def __init__(self, body=b'', status=200, headers=None):
        self.body = io.BytesIO(body)
        self.status = status
        self.headers = headers or {}

    def read(self, size):
        return self.body.read(size)

    def getheader(self, name, default=None):
        return self.headers.get(name, default)


def fixture_manifest():
    bodies = {'config.json': b'{"safe":true}', 'model.safetensors': b'synthetic-test-weight'}
    data = {'model_id': MODEL_ID, 'revision': MODEL_REVISION, 'profile': {'dimensions': 768},
            'files': [{'path': p, 'size': len(b), 'sha256': hashlib.sha256(b).hexdigest(), 'lfs': p.endswith('.safetensors')}
                      for p, b in bodies.items()]}
    return data, bodies


def transport(manifest, bodies, *, download_url='https://cas-bridge.xethub.hf.co/fixed?signature=test',
              status=200, content_headers=None, extra_headers=None):
    calls = []
    @contextmanager
    def request(method, url, headers, body=None):
        calls.append((method, url, dict(headers)))
        if method == 'POST':
            objects = json.loads(body)['objects']
            assert objects[0]['oid'] == manifest['files'][1]['sha256']
            response = {'transfer': 'basic', 'objects': [
                {'oid': obj['oid'], 'size': obj['size'], 'actions': {'download': {
                    'href': download_url, 'header': extra_headers or {}}}}
                for obj in objects]}
            yield Response(json.dumps(response).encode())
        elif '/raw/' in url:
            yield Response(bodies['config.json'])
        else:
            yield Response(bodies['model.safetensors'], status=status, headers=content_headers)
    return request, calls


def test_curated_manifest_pinned_and_complete():
    manifest = model_manifest()
    assert manifest['model_id'] == MODEL_ID and manifest['revision'] == MODEL_REVISION
    assert len(manifest['files']) == 14
    assert sum(x['size'] for x in manifest['files']) == 1264905363
    assert len({x['path'] for x in manifest['files']}) == 14
    assert {x['path'] for x in manifest['files'] if x['lfs']} == {
        'model.safetensors', '2_Dense/model.safetensors', '3_Dense/model.safetensors', 'tokenizer.json'}
    assert all(len(x['sha256']) == 64 and not Path(x['path']).is_absolute()
               and '..' not in Path(x['path']).parts for x in manifest['files'])


@pytest.mark.parametrize('host', [
    'cas-bridge.xethub.hf.co', 'us.aws.cdn.hf.co', 'us.gcp.cdn.hf.co',
])
def test_negotiated_download_hashes_progress_and_secret_scope(tmp_path, host):
    manifest, bodies = fixture_manifest()
    url = f'https://{host}/fixed?signature=fixture'
    request, calls = transport(manifest, bodies, download_url=url)
    progress = []
    result = download_model(tmp_path / 'stage', 'hf_test_secret', request=request,
                            manifest=manifest, progress=lambda n, total: progress.append((n, total)))
    assert result['bytes'] == sum(len(x) for x in bodies.values())
    assert result['files'] == 2
    assert progress[-1][0] == progress[-1][1] == result['bytes']
    assert calls[0][1] == f'https://huggingface.co/{MODEL_ID}.git/info/lfs/objects/batch'
    assert calls[1][1] == f'https://huggingface.co/{MODEL_ID}/raw/{MODEL_REVISION}/config.json'
    assert calls[0][2]['Authorization'] == calls[1][2]['Authorization'] == 'Bearer hf_test_secret'
    assert not any(name.lower() == 'authorization' for name in calls[2][2])
    assert 'hf_test_secret' not in json.dumps(calls[2])
    assert calls[2][1] == url
    assert verify_model(tmp_path / 'stage', manifest=manifest) == result


@pytest.mark.parametrize('url', [
    'http://cas-bridge.xethub.hf.co/a', 'https://127.0.0.1/a',
    'https://evil.test/a', 'https://huggingface.co.evil.test/a',
    'https://user:secret@huggingface.co/a', 'https://huggingface.co:8443/a',
    'https://huggingface.co/a#fragment', 'https://huggingface.co/a\r\nAuthorization: secret',
    'https://us.aws.cdn.hf.co.evil.test/a', 'https://us.gcp.cdn.hf.co.evil.test/a',
    'https://evil.us.aws.cdn.hf.co/a', 'https://us-aws.cdn.hf.co/a',
    'https://us.aws.cdn.hf.co@evil.test/a', 'https://us.gcp.cdn.hf.co./a',
    'https://us-east-1.aws.cdn.hf.co/a',  # Unreviewed edges are not implicitly allowed.
])
def test_unapproved_lfs_destination_is_never_fetched(tmp_path, url):
    manifest, bodies = fixture_manifest()
    request, calls = transport(manifest, bodies, download_url=url)
    with pytest.raises(ModelDownloadError, match='invalid_download_destination'):
        download_model(tmp_path / 'stage', 'hf_test', request=request, manifest=manifest)
    assert len(calls) == 1
    assert not (tmp_path / 'stage').exists()


@pytest.mark.parametrize('host', [
    'cas-bridge.xethub.hf.co', 'us.aws.cdn.hf.co', 'us.gcp.cdn.hf.co',
])
def test_cdn_cannot_request_authorization_headers(tmp_path, host):
    manifest, bodies = fixture_manifest()
    request, calls = transport(manifest, bodies, download_url=f'https://{host}/fixed',
                               extra_headers={'Authorization': 'anything'})
    with pytest.raises(ModelDownloadError, match='invalid_download_manifest'):
        download_model(tmp_path / 'stage', 'hf_secret', request=request, manifest=manifest)
    assert len(calls) == 1


@pytest.mark.parametrize('host', ['us.aws.cdn.hf.co', 'us.gcp.cdn.hf.co'])
@pytest.mark.parametrize('status', [302, 307])
def test_reviewed_cdn_redirect_is_not_followed_even_back_to_hub(tmp_path, host, status):
    manifest, bodies = fixture_manifest()
    request, calls = transport(manifest, bodies, download_url=f'https://{host}/fixed',
                               status=status, content_headers={'Location': 'https://huggingface.co/next'})
    with pytest.raises(ModelDownloadError, match='redirect_not_allowed'):
        download_model(tmp_path / 'stage', 'hf_test_secret', request=request, manifest=manifest)
    assert len(calls) == 3
    assert not any(url == 'https://huggingface.co/next' for _, url, _ in calls)
    assert 'hf_test_secret' not in json.dumps(calls[2])


@pytest.mark.parametrize('status,code', [(302, 'redirect_not_allowed'), (307, 'redirect_not_allowed'),
                                       (401, 'model_access_required'), (403, 'model_access_required'),
                                       (503, 'download_http_failed')])
def test_http_failures_never_follow_redirects_or_echo_errors(tmp_path, status, code):
    manifest, bodies = fixture_manifest()
    request, calls = transport(manifest, {**bodies, 'model.safetensors': b'upstream_secret'}, status=status)
    with pytest.raises(ModelDownloadError) as exc:
        download_model(tmp_path / 'stage', 'hf_secret', request=request, manifest=manifest)
    assert str(exc.value) == code
    assert len(calls) == 3


@pytest.mark.parametrize('alteration,headers,code', [
    (b'short', {}, 'model_hash_mismatch'),
    (b'x' * 50, {}, 'download_too_large'),
    (b'x' * len(b'synthetic-test-weight'), {}, 'model_hash_mismatch'),
    (b'synthetic-test-weight', {'Content-Encoding': 'gzip'}, 'unexpected_encoding'),
    (b'synthetic-test-weight', {'Content-Length': '999999'}, 'download_too_large'),
])
def test_corrupt_oversized_and_encoded_files_never_verify(tmp_path, alteration, headers, code):
    manifest, bodies = fixture_manifest()
    request, _ = transport(manifest, {**bodies, 'model.safetensors': alteration}, content_headers=headers)
    with pytest.raises(ModelDownloadError, match=code):
        download_model(tmp_path / 'stage', 'hf_secret', request=request, manifest=manifest)


def test_cancellation_and_existing_directory_never_erase_data(tmp_path):
    manifest, bodies = fixture_manifest()
    request, calls = transport(manifest, bodies)
    stage = tmp_path / 'stage'
    with pytest.raises(ModelDownloadError, match='cancelled'):
        download_model(stage, 'hf_secret', request=request, manifest=manifest, cancelled=lambda: True)
    assert not calls and not stage.exists()
    stage.mkdir()
    (stage / 'keep').write_text('existing')
    with pytest.raises(ModelDownloadError, match='staging_exists'):
        download_model(stage, 'hf_secret', request=request, manifest=manifest)
    assert (stage / 'keep').read_text() == 'existing' and not calls


def test_cancellation_during_transfer_stops_further_requests(tmp_path):
    manifest, bodies = fixture_manifest()
    request, calls = transport(manifest, bodies)
    cancelled = []
    with pytest.raises(ModelDownloadError, match='cancelled'):
        download_model(tmp_path / 'stage', 'hf_secret', request=request, manifest=manifest,
                       cancelled=lambda: bool(cancelled), progress=lambda *_: cancelled.append(True))
    assert len(calls) == 2


def test_symlinks_and_modified_installed_files_rejected(tmp_path):
    manifest, bodies = fixture_manifest()
    request, _ = transport(manifest, bodies)
    stage = tmp_path / 'stage'
    download_model(stage, 'hf_secret', request=request, manifest=manifest)
    weights = stage / 'model.safetensors'
    weights.write_bytes(b'x' * len(bodies['model.safetensors']))
    with pytest.raises(ModelDownloadError, match='model_hash_mismatch'):
        verify_model(stage, manifest=manifest)
    weights.unlink()
    weights.symlink_to(stage / 'config.json')
    with pytest.raises(ModelDownloadError, match='invalid_model_file'):
        verify_model(stage, manifest=manifest)


def test_public_network_validation_applies_even_to_approved_host():
    with patch('feedback.webhooks.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('127.0.0.1', 443))]):
        with pytest.raises(ModelDownloadError, match='network_failed'):
            with model_request('GET', 'https://huggingface.co/a', {}):
                pytest.fail('Private address should never connect')


@pytest.mark.parametrize('host', ['us.aws.cdn.hf.co', 'us.gcp.cdn.hf.co'])
def test_reviewed_cdn_mixed_dns_answers_are_rejected_before_connection(host):
    # A usable public IPv4 answer must not hide a private IPv6 answer.
    answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443)),
               (socket.AF_INET6, socket.SOCK_STREAM, 6, '', ('::1', 443, 0, 0))]
    with patch('feedback.webhooks.socket.getaddrinfo', return_value=answers), \
            patch('feedback.ai_download.PinnedHTTP') as connection:
        with pytest.raises(ModelDownloadError, match='network_failed'):
            with model_request('GET', f'https://{host}/fixed?signature=fixture', {}):
                pytest.fail('Mixed public/private DNS answers should never connect')
        connection.assert_not_called()


def test_timeout_before_request_and_safe_token_errors(tmp_path):
    manifest, bodies = fixture_manifest()
    request, calls = transport(manifest, bodies)
    with pytest.raises(ModelDownloadError, match='download_timeout'):
        with patch('feedback.ai_download.time.monotonic', side_effect=[0, 1]):
            download_model(tmp_path / 'stage', 'hf_secret', request=request, manifest=manifest, timeout=.1)
    assert not calls
    for token in ('', 'secret\r\nx: bad'):
        with pytest.raises(ModelDownloadError, match='model_access_required'):
            download_model(tmp_path / 'stage', token, request=request, manifest=manifest)


def _stalled_download(connection, directory, token, timeout):
    import time
    while True:
        time.sleep(.05)


def _successful_download(connection, directory, token, timeout):
    connection.send(('progress', 3, 3))
    connection.send(('ready', {'bytes': 3}))
    connection.close()


def _crashing_download(connection, directory, token, timeout):
    connection.close()


def test_supervision_bounds_stalled_transfer_and_cancellation(tmp_path):
    import time
    from feedback.ai_download import supervised_download
    start = time.monotonic()
    with pytest.raises(ModelDownloadError, match='download_timeout'):
        supervised_download(tmp_path / 'stage', 'hf_test', timeout=.5,
                            _target=_stalled_download)
    assert time.monotonic() - start < 3
    start = time.monotonic()
    with pytest.raises(ModelDownloadError, match='cancelled'):
        supervised_download(tmp_path / 'stage', 'hf_test', timeout=5,
                            cancelled=lambda: time.monotonic() - start > .5,
                            _target=_stalled_download)
    assert time.monotonic() - start < 3


def test_supervision_progress_success_and_worker_crash(tmp_path):
    from feedback.ai_download import supervised_download
    updates = []
    result = supervised_download(tmp_path / 'stage', 'hf_test', timeout=5,
                                 progress=lambda *args: updates.append(args),
                                 _target=_successful_download)
    assert result == {'bytes': 3} and updates == [(3, 3)]
    with pytest.raises(ModelDownloadError, match='download_failed'):
        supervised_download(tmp_path / 'stage', 'hf_test', timeout=5,
                            _target=_crashing_download)
