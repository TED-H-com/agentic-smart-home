"""Fixed Owner broker adapters and artifact preview, protocol fixtures only."""
import base64
import hashlib
import time
from types import SimpleNamespace

import pytest
from agentic_controller.models import WorkerPreview, digest
from agentic_controller.operations import Operations, UpdateManifest, broker_call
from fastapi import HTTPException
from pydantic import ValidationError


class Files:
    def __init__(self):
        self.expired = False
        self.job = {'id':'a'*32,'case_id':'b'*32,'created_epoch':time.time()}
        self.files = {'c'*32:{'name':'index.html','content_base64':base64.b64encode(b'<h1>fixture</h1>').decode(),
                              'sha256':hashlib.sha256(b'<h1>fixture</h1>').hexdigest()}}

    def authority(self, request):
        return {'owner':None,'web_epoch':None,'expires':time.time()+60}

    def get(self, identifier):
        return {**self.job,'content_expired':self.expired}

    def context_days(self):
        return 7

    async def artifact(self, identifier, artifact):
        return self.files[artifact]


async def test_preview_lost_reply_recovers_exact_manifest_and_never_persists_bytes(store, monkeypatch):
    workers = Files()
    operations = Operations(store, workers)
    saved, calls = {}, []
    async def broker(method, path, body=None, *, store=None):
        calls.append((method,path,body))
        if method == 'GET':
            if path.split('/')[-1] in saved:
                return saved[path.split('/')[-1]]
            raise HTTPException(404)
        saved[body['id']] = {'id':body['id'],'state':'running'}
        raise HTTPException(503, 'lost after acceptance')
    monkeypatch.setattr('agentic_controller.operations.broker_call', broker)
    request = SimpleNamespace(headers={'idempotency-key':'preview-once'})
    body = WorkerPreview(artifact_ids=['c'*32])
    with pytest.raises(HTTPException):
        await operations.preview('a'*32, body, request)
    repeated = await operations.preview('a'*32, body, request)
    assert repeated['state'] == 'running'
    submissions = [c for c in calls if c[0] == 'POST']
    assert len(submissions) == 1
    manifest = submissions[0][2]
    assert manifest['artifact_digest'] == digest([{'path':'index.html','sha256':workers.files['c'*32]['sha256']}])
    assert 'content_base64' not in str(store.list('worker_preview'))


async def test_preview_scope_recheck_after_artifact_fetch(store, monkeypatch):
    workers = Files()
    operations = Operations(store, workers)
    original = workers.artifact
    async def revoke(*args):
        data = await original(*args)
        store.set_setting('hermes_web_epoch', 'revoked')
        return data
    workers.artifact = revoke
    async def broker(method, path, body=None, *, store=None):
        assert method == 'GET'  # Revocation must prevent renderer launch.
        raise HTTPException(404)
    monkeypatch.setattr('agentic_controller.operations.broker_call', broker)
    with pytest.raises(HTTPException) as error:
        await operations.preview('a'*32, WorkerPreview(artifact_ids=['c'*32]), SimpleNamespace(headers={'idempotency-key':'scope-test'}))
    assert error.value.status_code == 403


@pytest.mark.parametrize('path', ['/../actions','http://attacker','/updates/../../actions','/coding/jobs','/previews/a/revoke'])
async def test_owner_broker_rejects_arbitrary_destinations_without_socket(path):
    with pytest.raises(HTTPException) as error:
        await broker_call('POST', path, {})
    assert error.value.status_code == 422


def manifest():
    return {'format':1,'publisher':'trusted-publisher','version':'0.7.0','source_commit':'a'*40,
            'image':'ghcr.io/trusted/controller@sha256:'+'a'*64,'image_id':'sha256:'+'b'*64,
            'compatibility':{'schema':3,'worker_contract':1,'runner_protocol':1,'runtime_digest':'c'*64,'ha_core_versions':['2026.10.1']},
            'tests':[{'name':'real-engine','result':'passed','sha256':'d'*64}],
            'issued_at':1728000000,'expires_at':1728086400}


def test_signed_manifest_preserves_exact_canonical_number_types():
    raw = manifest()
    projected = UpdateManifest.model_validate(raw).model_dump(by_alias=True)
    assert type(projected['issued_at']) is int
    assert projected == raw and digest(projected) == digest(raw)
    raw['issued_at'] += 0.5
    assert digest(UpdateManifest.model_validate(raw).model_dump(by_alias=True)) == digest(raw)
    with pytest.raises(ValidationError):
        UpdateManifest.model_validate({**raw,'public_key':'attacker-key'})
    with pytest.raises(ValidationError):
        UpdateManifest.model_validate({**raw,'issued_at':True})
