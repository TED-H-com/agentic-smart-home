"""Compiled chat LAN acquisition; all scopes, scans and model responses are fixtures."""
import copy
import json

import pytest
from playwright.sync_api import expect
from test_conversation_discovery_ui import conversation_browser, sent_messages  # noqa: F401
from test_network_inventory_ui import inventory_snapshot

SCOPE = "scope_" + "1" * 32
NETWORK = "192.168.50.0/24"


def saved_snapshot(revision=1, scope_id=SCOPE, network=NETWORK):
    value = inventory_snapshot(True, revision)
    for source in value["sources"]:
        if source["id"] != "router_import":
            source["target"] = network
    for row in value["observations"]:
        row["scope_id"] = scope_id
    return value


@pytest.fixture
def chat_network(conversation_browser):  # noqa: F811
    page, state = conversation_browser
    state.update(network=inventory_snapshot(), scan_mode="normal", network_held=[], hold_readback=False,
                 network_scopes={"contract_version": 1, "scopes": [
                     {"id": SCOPE, "label": "家庭有線網路 · 192.168.50.0/24", "interface": "demo0",
                      "address": "192.168.50.10", "network": NETWORK}], "limitations": []})

    def handle(route):
        request = route.request
        path = request.url.split("/api/", 1)[1]
        body = request.post_data_json if request.method == "POST" else None
        state["requests"].append((request.method, path, body, request.headers.get("idempotency-key")))
        if path.endswith("/scopes"):
            result = state["network_scopes"]
        elif request.method == "GET":
            result = copy.deepcopy(state["network"])
            if state["hold_readback"]:
                state["network_held"].append((route, result))
                return
        elif path.endswith("/scan"):
            assert body == {"scope_id": SCOPE}
            assert request.headers.get("x-agentic-csrf") == "fixture-csrf"
            assert request.headers.get("idempotency-key")
            if state["scan_mode"] != "lost_pending":
                state["network"] = saved_snapshot(state["network"]["revision"] + 1)
            result = {"contract_version": 1, "revision": state["network"]["revision"]}
            if state["scan_mode"].startswith("lost"):
                route.abort("failed")
                return
        else:
            raise AssertionError("Unexpected network operation: " + path)
        route.fulfill(status=200, content_type="application/json", body=json.dumps(result))

    page.route("**/api/discovery/inventory**", handle)
    page.goto(state["url"] + "#gateway")
    expect(page.get_by_role("button", name="探索更多設備", exact=True)).to_be_visible()
    yield page, state
    for route, result in state["network_held"]:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(result))


def network_requests(state, method=None):
    return [request for request in state["requests"] if request[1].startswith("discovery/inventory")
            and (method is None or request[0] == method)]


def open_network(page):
    page.get_by_role("button", name="探索更多設備", exact=True).click()
    panel = page.get_by_role("region", name="探索家庭網路")
    expect(panel.get_by_role("button", name="確認探索並交給管家")).to_be_enabled()
    return panel


@pytest.mark.parametrize("lost", [False, True])
def test_chat_scan_requires_consent_and_reads_back_before_one_followup(chat_network, lost, tmp_path):
    page, state = chat_network
    state["scan_mode"] = "lost_committed" if lost else "normal"
    assert network_requests(state) == []
    panel = open_network(page)
    expect(panel).to_contain_text("找到一個可探索的主機網路")
    expect(panel).to_contain_text("將結果與已保存的家庭設備觀測交給管家協助")
    assert network_requests(state, "POST") == [] and sent_messages(state) == []
    page.get_by_label("對話訊息", exact=True).fill("保留草稿：接好設備再說。")
    for width in (1440, 768, 390):
        page.set_viewport_size({"width": width, "height": 900 if width == 1440 else 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(tmp_path / f"chat-network-{width}.png"), full_page=True)
    panel.get_by_role("button", name="確認探索並交給管家").click()
    expect(page.get_by_text("合成的管家回覆。", exact=True)).to_be_visible()
    assert len(network_requests(state, "POST")) == 1
    assert len(sent_messages(state)) == 1
    assert len(network_requests(state, "GET")) >= 3
    expect(page.get_by_label("對話訊息", exact=True)).to_have_value("保留草稿：接好設備再說。")


def test_lost_scan_only_rechecks_saved_results_and_rejects_other_scope(chat_network):
    page, state = chat_network
    state["scan_mode"] = "lost_pending"
    panel = open_network(page)
    panel.get_by_role("button", name="確認探索並交給管家").click()
    check = panel.get_by_role("button", name="重新確認探索結果")
    expect(check).to_be_enabled(timeout=10000)
    assert len(network_requests(state, "POST")) == 1 and sent_messages(state) == []
    state["network"] = saved_snapshot(1, "scope_" + "2" * 32, "192.168.60.0/24")
    check.click()
    expect(check).to_be_enabled(timeout=10000)
    assert len(network_requests(state, "POST")) == 1 and sent_messages(state) == []
    state["network"] = saved_snapshot(2)
    check.click()
    expect(page.get_by_text("合成的管家回覆。", exact=True)).to_be_visible()
    assert len(network_requests(state, "POST")) == 1 and len(sent_messages(state)) == 1


def test_scan_without_saved_result_can_be_explicitly_reconfirmed(chat_network):
    page, state = chat_network
    state['scan_mode'] = 'lost_pending'
    panel = open_network(page)
    panel.get_by_role('button', name='確認探索並交給管家').click()
    choose = panel.get_by_role('button', name='重新選擇網路')
    expect(choose).to_be_enabled(timeout=10000)
    assert len(network_requests(state, 'POST')) == 1
    choose.click()
    confirm = panel.get_by_role('button', name='確認探索並交給管家')
    expect(confirm).to_be_enabled()
    assert len(network_requests(state, 'POST')) == 1
    state['scan_mode'] = 'normal'
    confirm.click()
    expect(page.get_by_text('合成的管家回覆。', exact=True)).to_be_visible()
    assert len(network_requests(state, 'POST')) == 2 and len(sent_messages(state)) == 1


def test_hidden_scan_response_cannot_share_into_another_view(chat_network):
    page, state = chat_network
    panel = open_network(page)
    state["hold_readback"] = True
    panel.get_by_role("button", name="確認探索並交給管家").click()
    expect(panel.get_by_text("正在確認已保存的探索結果…", exact=True)).to_be_visible()
    for _ in range(20):
        if state["network_held"]:
            break
        page.wait_for_timeout(50)
    assert len(state["network_held"]) == 1
    page.get_by_role("button", name="對話紀錄", exact=True).click()
    expect(page.get_by_role("region", name="對話紀錄", exact=True)).to_be_visible()
    old, snapshot = state["network_held"].pop()
    state["hold_readback"] = False
    old.fulfill(status=200, content_type="application/json", body=json.dumps(snapshot))
    page.wait_for_timeout(100)
    assert sent_messages(state) == []
    assert len(network_requests(state, "POST")) == 1


def test_multiple_networks_require_choice_and_missing_scopes_do_not_scan(chat_network):
    page, state = chat_network
    state["network_scopes"]["scopes"].append({
        "id": "scope_" + "2" * 32, "label": "另一個實體網路 · 192.168.60.0/24",
        "interface": "demo1", "address": "192.168.60.10", "network": "192.168.60.0/24"})
    page.get_by_role("button", name="探索更多設備", exact=True).click()
    panel = page.get_by_role("region", name="探索家庭網路")
    expect(panel.get_by_label("探索的家庭網路", exact=True)).to_have_value("")
    expect(panel.get_by_role("button", name="確認探索並交給管家")).to_have_count(0)
    panel.get_by_label("探索的家庭網路", exact=True).select_option(SCOPE)
    expect(panel.get_by_role("button", name="確認探索並交給管家")).to_be_enabled()
    assert network_requests(state, "POST") == []
    panel.get_by_role("button", name="收起探索").click()
    state["network_scopes"]["scopes"] = []
    page.get_by_role("button", name="探索更多設備", exact=True).click()
    expect(panel).to_contain_text("目前沒有可探索的主機實體網路")
    expect(panel.get_by_role("button", name="確認探索並交給管家")).to_have_count(0)
    assert network_requests(state, "POST") == [] and sent_messages(state) == []
