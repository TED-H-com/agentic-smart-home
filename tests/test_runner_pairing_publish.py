"""Fake Supervisor only. Does not start the App or call a real Supervisor."""
import json

from agentic_runner.app import Broker, publish_haos_pairing


class Response:
    def __init__(self, status, payload):
        self.status, self.payload = status, payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class Session:
    def __init__(self, status=200, payload=None):
        self.posts = []
        self.status = status
        self.payload = {"result": "ok", "data": {}} if payload is None else payload

    def post(self, url, *, json, headers):
        self.posts.append({"url": url, "json": json, "headers": dict(headers)})
        return Response(self.status, self.payload)


async def test_pairing_options_publish_once_and_never_in_host_mode(tmp_path):
    broker = Broker(tmp_path)
    session = Session()
    assert await publish_haos_pairing(broker, "supervisor-token", host_mode=False, client=session) is True
    assert len(session.posts) == 1
    post = session.posts[0]
    assert post["url"] == "http://supervisor/addons/self/options"
    assert post["json"] == {"options": {"pairing_fingerprint": broker.fingerprint, "pairing_code": broker.code}}
    assert post["headers"]["Authorization"] == "Bearer supervisor-token"
    assert "hassio_api" not in json.dumps(post["json"])
    broker.settings({"token_hash": "a" * 64})
    assert await publish_haos_pairing(broker, "supervisor-token", host_mode=False, client=session) is False
    assert len(session.posts) == 1
    fresh = Broker(tmp_path / "host")
    assert await publish_haos_pairing(fresh, "supervisor-token", host_mode=True, client=session) is False
    assert await publish_haos_pairing(fresh, "", host_mode=False, client=session) is False
    assert len(session.posts) == 1
    broker.db.close()
    fresh.db.close()


async def test_rejected_self_options_does_not_retry_with_hassio_api(tmp_path):
    broker = Broker(tmp_path)
    session = Session(status=403, payload={"result": "error"})
    assert await publish_haos_pairing(broker, "supervisor-token", host_mode=False, client=session) is False
    assert len(session.posts) == 1
    assert set(session.posts[0]["json"]) == {"options"}
    assert "hassio_api" not in json.dumps(session.posts[0]["json"])
    broker.db.close()
