"""快照退役记录领域服务：让退役清单本身可查、可导出、可逐笔核定。

清单数据由 DataVersionService 在退役时建立（`app.store` 的 `snapshot_retirement`），
本服务只读展示 + 提供“按清单核定下一步”的只读接口，结案仍由 data_version 负责。
"""
from __future__ import annotations

from app.services.base import BaseModuleService
from app.services.data_version import DataVersionService
from app.store import store


class SnapshotRetirementService(BaseModuleService):
    MODULE = "snapshot_retirement"
    ENTITY = "快照退役记录"
    OPERATION_TERM = "退役"
    LIST_FIELDS = ["记录编号", "版本编号", "数据来源", "快照状态", "关联投递", "待核定", "已交付", "已终止"]
    REQUIRED_FIELDS = ["记录编号"]
    STATUS_ORDER = ["退役中", "已结案"]
    ACTION_RULES: dict[str, str] = {}
    NEGATIVE_ACTIONS = []

    def __init__(self) -> None:
        self.versions = DataVersionService()

    def get_entry(self, entry_id: int):
        return store.find(self.MODULE, entry_id)

    def create_entry(self, values):
        """清单由退役流程建立，不允许手工登记，避免出现无版本可追溯的孤儿清单。"""
        return None, ["记录编号（退役清单由数据版本的“发起退役”自动建立）"]

    def run_action(self, entry_id: int, action: str):
        return None, "快照退役记录不支持手工动作，结案请回到数据版本执行废弃/重试"

    def balance(self, entry_id: int) -> dict:
        """按退役清单核定在途任务：列出每笔投递的下一步。"""
        record = store.find(self.MODULE, entry_id)
        if record is None:
            return {"ok": False, "message": f"{self.ENTITY} {entry_id} 不存在"}
        steps = []
        for ref in record["投递引用"]:
            delivery = next(
                (d for d in store.rows("data_delivery") if d["id"] == ref["投递ID"]),
                None,
            )
            if delivery is None:
                steps.append({**ref, "decision": "missing", "message": "投递记录已被清理"})
                continue
            steps.append({
                **ref,
                **self.versions.next_step_for_delivery(delivery),
            })
        return {
            "ok": True,
            "retirement": record,
            "steps": steps,
            "message": (
                f"清单{record['记录编号']}：待核定{record['待核定']}笔，"
                f"结案动作{record.get('结案动作') or '（未结案）'}"
            ),
        }
