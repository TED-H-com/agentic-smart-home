import copy

import pytest
from agentic_controller.engine import Engine
from agentic_controller.ha import HAError, HomeAssistant
from agentic_controller.models import HELPERS, AutomationIntent, digest, utcnow
from agentic_controller.store import Store

SOURCE = "input_boolean.agentic_motion_test"
TARGET = "input_boolean.agentic_lamp_test"


def pytest_addoption(parser):
    parser.addoption("--real-ha", action="store_true", help="Run isolated real Home Assistant Docker integration")
    parser.addoption("--containers", action="store_true", help="Probe the packaged Runner image")
    parser.addoption("--haos-vm", action="store_true", help="Boot official HAOS in an isolated KVM guest")
    parser.addoption("--linux-install", action="store_true", help="Install product ISO on a disposable nested-KVM disk")


class FakeHA:
    """Contract fixture only. Never included in App images or real-device evidence."""
    def __init__(self):
        self.configs, self.writes, self.calls = {}, [], []
        self.revoked = False
        self.states = {SOURCE: "off", TARGET: "off"}
        self.lose_response_at = None
        self.changed_metadata = False
        self.connected, self.last_observed = True, utcnow()
        self.observations = []
        self.conflicts = []

    async def owner(self, user_id):
        return {"id": "owner", "is_owner": True, "is_active": True} if user_id == "owner" and not self.revoked else None

    async def inventory(self):
        return [{"entity_id": id, "domain": "input_boolean", "name": id, "area": None,
                 "state": state, "availability": "available", "actions": ["turn_on", "turn_off"],
                 "observed_at": utcnow(), "metadata_hash": digest([id, self.changed_metadata])}
                for id, state in self.states.items() if id in HELPERS]

    async def validate(self, config):
        assert config["mode"] == "single"

    async def ensure_write_compatibility(self):
        return None

    async def config(self, id):
        return copy.deepcopy(self.configs.get(id))

    async def dependencies(self, ids, owned):
        return self.conflicts

    async def backup(self, name):
        return {"id": "fixture-backup", "name": name, "created_at": utcnow()}

    async def write(self, id, config):
        self.configs[id] = copy.deepcopy(config)
        self.writes.append(copy.deepcopy(config))
        self.states["automation." + id] = "off" if config.get("initial_state") is False else "on"
        if self.lose_response_at == len(self.writes):
            raise HAError("fixture: response lost after write")

    async def automation_entity(self, id, attempts=20, expected=None):
        return {"entity_id": "automation." + id, "state": self.states["automation." + id], "attributes": {"id": id}}

    async def service(self, domain, action, id):
        self.calls.append((domain, action, id))
        self.states[id] = "on" if action == "turn_on" else "off"

    async def request(self, method, path, *args, **kwargs):
        if path == "/config":
            return {"version": "fixture", "location_name": "測試工作台"}
        if path.startswith("/states/"):
            return {"entity_id": path[8:], "state": self.states[path[8:]], "attributes": {}}
        if path == "/states":
            return [{"entity_id": id, "state": state, "attributes": {}} for id, state in self.states.items()]
        raise AssertionError(path)

    def evidence(self, proposal):
        return HomeAssistant.evidence(self, proposal)


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path / "controller.sqlite3")
    store.set_setting("policy", {"entities": [
        {"entity_id": id, "read": True, "write": "dedicated_helper", "purpose": "isolated test helper"}
        for id in HELPERS]})
    yield store
    store.close()


@pytest.fixture
def ha():
    return FakeHA()


@pytest.fixture
def engine(store, ha):
    return Engine(store, ha)


@pytest.fixture
def intent():
    return AutomationIntent.model_validate({"title": "動作開燈", "trigger": {
        "entity_id": SOURCE, "from_state": "off", "to_state": "on"},
        "actions": [{"entity_id": TARGET, "action": "turn_on"}]})


async def proposal(engine, intent):
    task = engine.task("test request", "fixture")
    task = await engine.propose(task, intent)
    return engine.store.get("proposal", task["proposal_id"])
