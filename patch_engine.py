with open("controller/agentic_controller/engine.py", "r") as f:
    content = f.read()

old_block = """            if mode == "upsert" and intent.resource_id is None:
                for owned in self.store.list("owned", 10000):
                    if owned.get("intent", {}).get("title") == intent.title:
                        raise Rejected(f"名稱已存在：{intent.title}")
                    if owned.get("semantic") == semantic and owned.get("status") == "deployed":
                        live = await self.ha.config(owned["id"])
                        if digest(live) == owned["last_hash"]:
                            if commit_guard:
                                await commit_guard()
                            return self.store.transition(task, "succeeded", resource_id=owned["id"],
                                message="相同規則已存在，未建立重複自動化", verification="existing_config_readback")"""

new_block = """            if mode == "upsert" and intent.resource_id is None:
                for owned in self.store.list("owned", 10000):
                    if owned.get("semantic") == semantic and owned.get("status") == "deployed":
                        live = await self.ha.config(owned["id"])
                        if digest(live) == owned["last_hash"]:
                            if commit_guard:
                                await commit_guard()
                            return self.store.transition(task, "succeeded", resource_id=owned["id"],
                                message="相同規則已存在，未建立重複自動化", verification="existing_config_readback")
                    if owned.get("intent", {}).get("title") == intent.title:
                        raise Rejected(f"名稱已存在：{intent.title}")"""

content = content.replace(old_block, new_block)
with open("controller/agentic_controller/engine.py", "w") as f:
    f.write(content)
