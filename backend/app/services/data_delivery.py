"""数据投递领域服务：管理数据管道投递，与 subscriber、call_log、govern_report、data_version 联动。

领用与终态（本模块持有）：
- 投递必须通过 `投递版本` 领用一份快照；领用时快照必须处于可领用状态。
- 投递同时只承认一个终态：`已交付`（完成回执）或 `已终止`（废弃申请）。
  两个竞争事件谁先落库谁生效，后到的只写一条竞争说明，不覆盖凭据。
- 已发数据的投递记录保留 `投递版本`/`投递终态`/`投递回执`，后续快照退役不覆盖。
"""
from __future__ import annotations

from app.services.base import BaseModuleService
from app.services.concurrency import DOMAIN_LOCK
from app.services.data_version import (
    DELIVERY_ABANDON_ACTION,
    DELIVERY_RECEIPT_ACTION,
    DELIVERY_STATES,
    FINAL_DELIVERY_STATES,
    RETIRED_STATES,
    DataVersionService,
)
from app.store import store


class DataDeliveryService(BaseModuleService):
    MODULE = "data_delivery"
    ENTITY = "投递管道"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["管道编号", "数据来源", "目标地址", "投递格式", "分区策略", "投递频率", "压缩方式", "最后投递"]
    EXTRA_FIELDS = ["投递版本"]
    REQUIRED_FIELDS = ["管道编号", "数据来源", "目标地址"]
    STATUS_ORDER = ["待配置", "投递中", "已完成", "投递异常", "已终止"]
    ACTION_RULES = {"启动投递": "投递中", "暂停投递": "已完成", "标记异常": "投递异常"}
    NEGATIVE_ACTIONS = []

    # 只有这些投递状态允许启动/继续交付。
    CLOSED_STATES = ("已终止",)

    def __init__(self) -> None:
        self.versions = DataVersionService()

    def run_action(self, entry_id: int, action: str):
        """启动投递前校验领用；完成回执/废弃申请走单一终态判定。"""
        with DOMAIN_LOCK:
            if action in (DELIVERY_RECEIPT_ACTION, DELIVERY_ABANDON_ACTION):
                return self._deliver_terminal(entry_id, action)
            entry = store.find(self.MODULE, entry_id)
            if entry is None:
                return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
            if action in ("启动投递", "暂停投递") and entry.get("投递终态"):
                return entry, (
                    f"{self.ENTITY}已是终态{entry['投递终态']}，"
                    "凭原投递记录追溯，不能再次启动"
                )
            entry, message = super().run_action(entry_id, action)
            if entry is None:
                return entry, message
            if action == "启动投递":
                claimed, claim_message = self._claim_version(entry)
                if not claimed:
                    # 领用失败时回退状态，不让“已启动”落在没有版本的管道上。
                    entry["status"] = "待配置"
                    entry["pending"] = True
                    return entry, claim_message
                self._notify_subscribers(entry)
                self._log_delivery(entry, "started")
                message = claim_message + "，已通知订阅方并记录调用日志"
            elif action == "暂停投递":
                self._create_data_version(entry)
                self._log_delivery(entry, "completed")
                message += "，已创建数据版本"
            elif action == "标记异常":
                self._trigger_govern_report(entry)
                self._log_delivery(entry, "error")
                message += "，已触发治理报告"
            return entry, message

    # ---------- 领用 ----------

    def _claim_version(self, delivery: dict) -> tuple[bool, str]:
        """启动投递时领用快照。没有版本可领用时拒绝启动，而不是静默放过。"""
        version_no = delivery.get("投递版本", "")
        if not version_no:
            return False, "启动投递失败：未登记要投递的数据版本，无法领用快照"
        version = next(
            (v for v in store.rows("data_version") if v.get("版本编号") == version_no),
            None,
        )
        if version is None:
            return False, f"启动投递失败：数据版本{version_no}不存在"
        if version.get("status") in RETIRED_STATES:
            return False, (
                f"启动投递失败：数据版本{version_no}当前为{version.get('status')}，"
                "已退役版本不再接受新的领用"
            )
        delivery["投递领用"] = version_no
        delivery["投递领用状态"] = version.get("status")
        delivery["投递终态"] = ""
        delivery["投递回执"] = ""
        return True, f"已领用数据版本{version_no}（{version.get('status')}）"

    # ---------- 终态：完成回执 / 废弃申请 ----------

    def _deliver_terminal(self, entry_id: int, action: str):
        delivery = store.find(self.MODULE, entry_id)
        if delivery is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        target_state, receipt_kind, label = DELIVERY_STATES[action]
        current = delivery.get("投递终态") or ""
        if current in FINAL_DELIVERY_STATES:
            if current == target_state:
                record = self.versions.record_delivery_outcome(delivery, action) if current else None
                return delivery, (
                    f"{self.ENTITY}已结案为{current}，重复的{label}不重复结案"
                    + (f"，退役清单{record['记录编号']}保持已结案" if record else "")
                )
            # 竞争事件：同一投递上另一个终态先到，先到的凭据不覆盖。
            delivery["竞争说明"] = (
                f"{label}晚于{current}到达，未覆盖既有终态"
            )
            self._log_delivery(delivery, "conflict")
            return delivery, (
                f"{self.ENTITY}已由先到的{current}结案，{label}未生效；"
                "投递只确认一个终态，原交付凭据保留"
            )

        if action == DELIVERY_RECEIPT_ACTION and delivery.get("status") == "已终止":
            delivery["竞争说明"] = "投递已被废弃动作终止，完成回执未生效"
            self._log_delivery(delivery, "conflict")
            return delivery, (
                f"{self.ENTITY}已按废弃申请终止，完成回执晚到不改变终态"
            )

        version_no = delivery.get("投递版本", "")
        version = next(
            (v for v in store.rows("data_version") if v.get("版本编号") == version_no),
            None,
        )
        if version is not None and version.get("status") in ("已退役", "已废弃"):
            # 快照退役后才到的事件仍按投递自身凭据结案：已开始交付的数据允许结案，
            # 但终态只写投递记录，不回滚快照状态。
            delivery["版本已退役"] = True

        delivery["status"] = "已终止" if target_state == "已终止" else "已完成"
        delivery["pending"] = False
        delivery["abnormal"] = target_state == "已终止"
        delivery["投递终态"] = target_state
        delivery["投递回执"] = f"{receipt_kind}:{delivery.get('投递版本', '')}"
        delivery["投递结案时间"] = "-"
        record = self.versions.record_delivery_outcome(delivery, action)
        self._log_delivery(delivery, receipt_kind)
        message = f"{self.ENTITY}已{action}，终态{target_state}"
        if record is not None:
            message += (
                f"，退役清单{record['记录编号']}已回写（待核定{record['待核定']}笔）"
            )
            if record["待核定"] == 0:
                message += "，该清单已全部核定"
        return delivery, message

    # ---------- 原有联动 ----------

    def _notify_subscribers(self, delivery: dict):
        """启动投递通知所有关联订阅方。"""
        source = delivery.get("数据来源", "")
        did = str(delivery["id"]).zfill(4)
        for sub in store.rows("subscriber"):
            if sub.get("数据主题") == source or sub.get("订阅方") and source in str(sub.get("订阅方", "")):
                sub["最后推送"] = "2026-11-17"
                sub["消费状态"] = "待消费"
        # 触发webhook路由
        for wh in store.rows("webhook_router"):
            if wh.get("触发事件") == "data_delivery":
                wh["最后触发"] = "2026-11-17"
                wh["投递状态"] = f"delivery_{did}"

    def _log_delivery(self, delivery: dict, event: str):
        log_rows = store.rows("call_log")
        did = str(delivery["id"]).zfill(4)
        log_rows.append({
            "id": max((int(r.get("id", 0)) for r in log_rows), default=0) + 1,
            "记录编号": f"CALL-DELIV-{did}",
            "接口端点": delivery.get("数据来源", ""),
            "请求参数": delivery.get("目标地址", ""),
            "响应状态": "200" if event != "error" else "500",
            "响应耗时(ms)": 300 if event == "started" else 500,
            "返回行数": 0,
            "错误信息": "" if event != "error" else f"管道{did}投递异常",
            "调用时间": "2026-11-17",
            "status": "成功" if event != "error" else "超时",
            "pending": event != "error",
            "abnormal": event == "error",
        })

    def _create_data_version(self, delivery: dict):
        """投递完成创建数据版本快照。"""
        ver_rows = store.rows("data_version")
        did = str(delivery["id"]).zfill(4)
        ver_rows.append({
            "id": max((int(r.get("id", 0)) for r in ver_rows), default=0) + 1,
            "版本编号": f"DATA-DELIV-{did}",
            "数据来源": delivery.get("数据来源", ""),
            "行数统计": 1000,
            "版本标签": "delivery_complete",
            "Schema摘要": delivery.get("投递格式", "JSON"),
            "创建原因": f"投递管道{did}完成",
            "依赖版本": delivery.get("分区策略", ""),
            "创建时间": "2026-11-17",
            "status": "可用", "pending": True, "abnormal": False,
        })

    def _trigger_govern_report(self, delivery: dict):
        """投递异常触发治理报告。"""
        gov_rows = store.rows("govern_report")
        did = str(delivery["id"]).zfill(4)
        gov_rows.append({
            "id": max((int(r.get("id", 0)) for r in gov_rows), default=0) + 1,
            "报告编号": f"GOVE-DELIV-{did}",
            "评估范围": f"投递管道异常:{delivery.get('数据来源', '')}",
            "合规指标": "数据投递成功率",
            "当前得分": 50.0,
            "改进事项": f"管道{did}投递异常，需重启或排查",
            "评估周期": "2026-Q4",
            "负责人": "数据运维",
            "生成时间": "2026-11-17",
            "status": "待生成", "pending": True, "abnormal": True,
        })

    def next_step(self, delivery_id: int) -> dict:
        """按退役清单核定该投递的下一步，并给出接班人复核凭据。"""
        delivery = store.find(self.MODULE, delivery_id)
        if delivery is None:
            return {"ok": False, "message": f"{self.ENTITY} {delivery_id} 不存在"}
        return {
            "ok": True,
            "delivery": delivery,
            "next": self.versions.next_step_for_delivery(delivery),
            "credentials": self.versions.verify_delivery_credentials(delivery_id),
        }

    def calc_pipeline_health(self) -> dict:
        """跨模块管道健康度：汇总投递管道+订阅方+数据版本+调用日志。"""
        deliveries = store.rows("data_delivery")
        subscribers = store.rows("subscriber")
        versions = store.rows("data_version")
        logs = [l for l in store.rows("call_log") if str(l.get("记录编号", "")).startswith("CALL-DELIV-")]
        active = sum(1 for d in deliveries if d.get("status") == "投递中")
        error = sum(1 for d in deliveries if d.get("status") == "投递异常")
        return {
            "total_pipelines": len(deliveries),
            "active": active,
            "error": error,
            "subscribers_waiting": sum(1 for s in subscribers if s.get("消费状态") == "待消费"),
            "version_snapshots": len(versions),
            "logged_events": len(logs),
            "terminal_delivered": sum(1 for d in deliveries if d.get("投递终态") == "已交付"),
            "terminal_terminated": sum(1 for d in deliveries if d.get("投递终态") == "已终止"),
        }
