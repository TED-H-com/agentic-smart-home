"""Private delivery receipts, separate from public worker jobs and wire contracts."""
from typing import Annotated, Literal

from pydantic import Field

from .models import Channel, Strict
from .work import Identifier, WorkerId

COMPLETION_KIND = "worker_completion"
CompletionState = Literal["succeeded", "failed", "cancelled", "blocked", "expired", "interrupted"]


class WorkerCompletion(Strict):
    id: Identifier  # One durable intent per job, including repeated Runner receipts.
    message_id: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    case_id: Identifier
    scope: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    source: Channel
    worker_id: WorkerId
    state: CompletionState
    delivery: Literal["pending", "sending", "sent", "unconfirmed", "not_sent"] = "pending"
    created_at: str
    updated_at: str

    def text(self):
        worker = "主機" if self.worker_id == "host-coder" else "家庭現場"
        state = {"succeeded": "已完成", "failed": "未完成", "cancelled": "已取消",
                 "blocked": "未開始", "expired": "已到期", "interrupted": "已中斷"}[self.state]
        return f"{worker}開發助手工作{state}。\nWeb 工作：#workers/{self.id}"
