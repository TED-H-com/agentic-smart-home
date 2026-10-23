import re

with open("tests/test_real_ha.py", "r") as f:
    content = f.read()

content = content.replace(
"""        assert after_restart, "Rule did not execute after real Core restart"
        task2 = engine.task("same rule again", "fixture")
        duplicate = await engine.propose(task2, intent)
""",
"""        assert after_restart, "Rule did not execute after real Core restart"
        task2 = engine.task("same rule again", "fixture")
        intent_duplicate = intent.model_copy()
        intent_duplicate.title = "重新描述同一規則"
        duplicate = await engine.propose(task2, intent_duplicate)
""")

with open("tests/test_real_ha.py", "w") as f:
    f.write(content)
