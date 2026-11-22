"""数据版本领域服务：管理数据快照版本，与 data_delivery、subscriber、schema_diff、govern_report 联动。

退役语义（本模块持有）：
- 快照退役分两段：`退役中`（已冻结领用、已关联在途投递）→ `已退役`。
- 退役清单即“快照退役记录”（`store.rows("snapshot_retirement")`），一份退役记录
  对应一份快照，按记录编号幂等重试，中断后重试只结案一次。
- 进入退役后快照不再对新的领用方开放；已开始交付的投递按清单逐笔核定终态。
"""
from __future__ import annotations

from app.services.base import BaseModuleService
from app.services.concurrency import DOMAIN_LOCK
from app.store import store

RETIREMENT_LEDGER = "snapshot_retirement"

# 退役清单判定的投递终态。只有这两个终态算“已核定”。
FINAL_DELIVERY_STATES = ("已交付", "已终止")

# 进入退役后不再接受新领用的快照状态。`已废弃` 是旧版动作留下的终态，按已退役对待。
RETIRED_STATES = ("退役中", "已退役", "已废弃")

# 终态：任何“创建/标注”都不允许把快照从这里改回可领用。
TERMINAL_VERSION_STATES = ("已退役", "已废弃")

# 投递动作 -> (投递终态, 退役清单元数据)。完成回执与废弃申请都可能终结同一笔投递。
DELIVERY_RECEIPT_ACTION = "提交完成回执"
DELIVERY_ABANDON_ACTION = "申请废弃"
DELIVERY_STATES = {
    DELIVERY_RECEIPT_ACTION: ("已交付", "completed", "完成回执"),
    DELIVERY_ABANDON_ACTION: ("已终止", "abandoned", "废弃申请"),
}


class DataVersionService(BaseModuleService):
    MODULE = "data_version"
    ENTITY = "数据快照"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["版本编号", "数据来源", "行数统计", "版本标签", "Schema摘要", "创建原因", "依赖版本", "创建时间"]
    REQUIRED_FIELDS = ["版本编号", "数据来源", "行数统计"]
    STATUS_ORDER = ["创建中", "可用", "标注中", "已废弃", "退役中", "已退役"]
    ACTION_RULES = {
        "创建快照": "可用",
        "标注版本": "标注中",
        "发起退役": "退役中",
        "废弃版本": "已退役",
        "废弃快照": "已退役",
        "完成退役": "已退役",
    }
    # 退役动作别名：发起（冻结领用）与结案分组，避免前端换一个词就落不到领域逻辑。
    RETIRE_START_ACTIONS = {"发起退役", "申请退役", "退役快照"}
    RETIRE_SETTLE_ACTIONS = {"废弃版本", "废弃快照", "完成退役", "结案退役"}
    # 退役/废弃是负向动作：影响接收方能否继续领用已开始交付的数据。
    NEGATIVE_ACTIONS = ["废弃版本", "废弃快照", "完成退役", "结案退役"]

    # 已进入退役流程后不接受的状态，避免把这一版重新列为可领用。
    RETIRED_INTAKE_TARGET = "已退役"

    def run_action(self, entry_id: int, action: str):
        """创建快照同步投递管道和订阅方；标注版本联动治理报告；退役/废弃走退役清单。"""
        with DOMAIN_LOCK:
            if action in self.RETIRE_SETTLE_ACTIONS:
                return self._run_obsolescence(entry_id, action)
            if action in self.RETIRE_START_ACTIONS:
                return self._run_retirement_start(entry_id)
            entry = store.find(self.MODULE, entry_id)
            if entry is not None and action in ("创建快照", "标注版本") and (
                entry.get("status") in TERMINAL_VERSION_STATES
            ):
                # 退役是终态：创建/标注不能把这一版重新列回可领用。
                return entry, f"{self.ENTITY}已退役，不能再执行{action}"
            entry, message = super().run_action(entry_id, action)
            if entry is None:
                return entry, message
            if action == "创建快照":
                self._sync_delivery_and_subscribers(entry)
                self._log_version_event(entry, "created")
                message += "，已同步投递管道和订阅方"
            elif action == "标注版本":
                self._create_govern_report(entry)
                message += "，已联动治理报告"
            return entry, message

    # ---------- 退役：唤醒与结案 ----------

    def _run_retirement_start(self, entry_id: int):
        version = store.find(self.MODULE, entry_id)
        if version is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        current = version.get("status")
        if current in ("已退役", "已废弃"):
            return version, f"{self.ENTITY}已退役，退役清单已结案，不能再次发起"
        if current == "退役中":
            record = self._retirement_record(version)
            return version, f"{self.ENTITY}退役进行中，沿用退役清单{record.get('记录编号', '')}"

        # 这里不经过 BaseModuleService.run_action：合法前驱状态由领域自己判断，
        # 避免快照退役后又被通用动作改回可领用状态。
        version["status"] = "退役中"
        version["pending"] = True
        version["abnormal"] = False
        version["retirement_id"] = self._retirement_id(version)
        record, replayed = self._open_retirement_record(version)
        message = (
            f"{self.ENTITY}已冻结领用，退役清单{record['记录编号']}关联在途投递"
            f"{len(record['投递引用'])}笔"
        )
        if replayed:
            message += "（沿用已有关联，未重复建单）"
        return version, message

    def _run_obsolescence(self, entry_id: int, action: str):
        """废弃动作的统一入口：把快照置为已退役并结案退役清单。

        已退役的快照再收到废弃请求时只回写清单，不重复审计、不重复结案；
        任何情况下都不把退役中的版本改回可领用状态。
        """
        version = store.find(self.MODULE, entry_id)
        if version is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        record = self._retirement_record(version)
        already_retired = version.get("status") in ("已退役", "已废弃")
        if record is None and already_retired:
            # 历史数据：没有退役清单的已废弃版本，补一份终态清单以便追溯。
            record = self._backfill_retirement_record(version)
            return version, f"{self.ENTITY}已退役，已补录退役清单{record['记录编号']}"

        if record is None:
            record, _ = self._open_retirement_record(version)
        version["status"] = self.RETIRED_INTAKE_TARGET
        version["pending"] = False
        version["abnormal"] = True
        version["retirement_id"] = record["记录编号"]
        self._settle_retirement_record(version, record, action)
        message = f"{self.ENTITY}已执行{action}，当前状态{version['status']}"
        if already_retired:
            message += "，退役清单此前已结案，本次仅回写清单"
        else:
            message += f"，退役清单{record['记录编号']}已结案"
        return version, message

    # ---------- 退役清单 ----------

    def _retirement_id(self, version: dict) -> str:
        return f"RETIRE-VER-{int(version['id']):04d}"

    def _retirement_record(self, version: dict) -> dict | None:
        rid = version.get("retirement_id") or self._retirement_id(version)
        for record in store.rows(RETIREMENT_LEDGER):
            if record.get("记录编号") == rid:
                return record
        return None

    def _open_retirement_record(self, version: dict) -> tuple[dict, bool]:
        """建立退役清单；重复发起时沿用原清单，保证重试不裂单。

        调用方可能并发重试，整个“查已有 + 新建”在领域锁内完成，
        因此同一快照最多只会有一份清单。
        """
        with DOMAIN_LOCK:
            return self._open_retirement_record_locked(version)

    def _open_retirement_record_locked(self, version: dict) -> tuple[dict, bool]:
        """建立退役清单；重复发起时沿用原清单，保证重试不裂单。"""
        record = self._retirement_record(version)
        if record is not None:
            # 中断后重试：沿用原清单只加一次重试计数，不另起一张清单。
            record["重试次数"] = int(record.get("重试次数", 0)) + 1
            return record, True
        ledger = store.rows(RETIREMENT_LEDGER)
        record = {
            "id": max((int(r.get("id", 0)) for r in ledger), default=0) + 1,
            "记录编号": self._retirement_id(version),
            "版本编号": version.get("版本编号", ""),
            "版本ID": version["id"],
            "数据来源": version.get("数据来源", ""),
            "快照状态": "退役中",
            "关联投递": 0,
            "待核定": 0,
            "已交付": 0,
            "已终止": 0,
            "投递引用": [],
            "结案动作": "",
            "重试次数": 0,
            "status": "退役中",
            "pending": True,
            "abnormal": False,
            "创建时间": "-",
            "结案时间": "-",
        }
        ledger.append(record)
        self._attach_inflight_deliveries(version, record)
        self._freeze_receivers(version)
        return record, False

    def _freeze_receivers(self, version: dict) -> None:
        """退役启动即冻结接收方：未消费完的数据不再允许按这一版继续领用。

        已经交付出去的数据不回收，只把“待消费”的接收方标记为冻结，
        让接收方在清单核定后再决定是否换新版本。
        """
        source = version.get("数据来源", "")
        for sub in store.rows("subscriber"):
            if sub.get("数据主题") != source:
                continue
            if sub.get("消费状态") == "待消费":
                sub["消费状态"] = "领用冻结"
                sub["pending"] = True
                sub["冻结版本"] = version.get("版本编号", "")

    def _backfill_retirement_record(self, version: dict) -> dict:
        """为没有清单的历史废弃版本补录一份已结案的清单。"""
        ledger = store.rows(RETIREMENT_LEDGER)
        record = {
            "id": max((int(r.get("id", 0)) for r in ledger), default=0) + 1,
            "记录编号": self._retirement_id(version),
            "版本编号": version.get("版本编号", ""),
            "版本ID": version["id"],
            "数据来源": version.get("数据来源", ""),
            "快照状态": "已退役",
            "关联投递": 0,
            "待核定": 0,
            "已交付": 0,
            "已终止": 0,
            "投递引用": [],
            "结案动作": "废弃版本",
            "重试次数": 0,
            "status": "已结案",
            "pending": False,
            "abnormal": True,
            "创建时间": "-",
            "结案时间": "-",
        }
        ledger.append(record)
        version["retirement_id"] = record["记录编号"]
        self._attach_inflight_deliveries(version, record)
        self._recount_record(record)
        return record

    def _attach_inflight_deliveries(self, version: dict, record: dict) -> None:
        """把引用这一版快照的投递全部挂到退役清单上。

        在途投递进清单等核定；已交付/已终止的投递作为既成凭据一并关联，
        清单只做展示，不回头改写它们的终态。
        这一段可以安全重入：重复调用按投递编号去重。
        """
        seen = {ref["投递ID"] for ref in record["投递引用"]}
        for delivery in store.rows("data_delivery"):
            if delivery.get("投递版本") != version.get("版本编号"):
                continue
            if delivery["id"] in seen:
                continue
            terminal = delivery.get("投递终态") or ""
            record["投递引用"].append({
                "投递ID": delivery["id"],
                "管道编号": delivery.get("管道编号", ""),
                "接收方": delivery.get("目标地址", ""),
                "状态": (
                    "已交付" if terminal == "已交付"
                    else "已终止" if terminal == "已终止"
                    else "在途"
                ),
                "终态": terminal,
                "核定动作": "既有凭据" if terminal else "",
            })
            if terminal:
                delivery["退役记录"] = record["记录编号"]
            else:
                delivery.setdefault("退役记录", record["记录编号"])
        self._recount_record(record)

    def _recount_record(self, record: dict) -> None:
        refs = record["投递引用"]
        record["关联投递"] = len(refs)
        record["待核定"] = sum(1 for ref in refs if ref["终态"] not in FINAL_DELIVERY_STATES)
        record["已交付"] = sum(1 for ref in refs if ref["终态"] == "已交付")
        record["已终止"] = sum(1 for ref in refs if ref["终态"] == "已终止")

    def _settle_retirement_record(self, version: dict, record: dict, action: str) -> None:
        """结案：重试时保留原结案动作，不覆盖既成结论。"""
        record["快照状态"] = "已退役"
        if record.get("结案动作"):
            # 结案后重试：只累计重试次数并刷新引用，不重写结论、不追加审计。
            record["重试次数"] = int(record.get("重试次数", 0)) + 1
            self._recount_record(record)
            return
        record["结案动作"] = action
        record["status"] = "已结案"
        record["pending"] = False
        record["abnormal"] = True
        record["结案时间"] = "-"
        self._attach_inflight_deliveries(version, record)
        self._recount_record(record)
        self._audit_obsolescence(version, record)
        self._log_version_event(version, "retired")

    def record_delivery_outcome(self, delivery: dict, action: str) -> dict | None:
        """把投递终态回写到退役清单；返回被更新的退役记录（没有则 None）。"""
        target_state, _, label = DELIVERY_STATES[action]
        record = None
        version_no = delivery.get("投递版本", "")
        if version_no:
            for version in store.rows(self.MODULE):
                if version.get("版本编号") == version_no:
                    record = self._retirement_record(version)
                    break
        if record is None:
            record_id = delivery.get("退役记录", "")
            record = next(
                (r for r in store.rows(RETIREMENT_LEDGER) if r.get("记录编号") == record_id),
                None,
            )
        if record is None:
            return None
        ref = next(
            (item for item in record["投递引用"] if item["投递ID"] == delivery["id"]),
            None,
        )
        if ref is None:
            ref = {
                "投递ID": delivery["id"],
                "管道编号": delivery.get("管道编号", ""),
                "接收方": delivery.get("目标地址", ""),
                "状态": "逾期核定",
                "终态": "",
                "核定动作": "",
            }
            record["投递引用"].append(ref)
        ref["终态"] = target_state
        ref["状态"] = "已交付" if target_state == "已交付" else "已终止"
        ref["核定动作"] = label
        self._recount_record(record)
        return record

    def retirement_status(self, entry_id: int) -> dict:
        """查询快照退役进度：清单 + 在途投递 + 已知终态。"""
        version = store.find(self.MODULE, entry_id)
        if version is None:
            return {"ok": False, "message": f"{self.ENTITY} {entry_id} 不存在或已归档"}
        record = self._retirement_record(version)
        if record is None:
            return {
                "ok": True,
                "version": version,
                "retirement": None,
                "message": "该快照尚未进入退役流程",
            }
        return {
            "ok": True,
            "version": version,
            "retirement": record,
            "message": (
                f"退役清单{record['记录编号']}：待核定{record['待核定']}笔，"
                f"已交付{record['已交付']}笔，已终止{record['已终止']}笔"
            ),
        }

    def next_step_for_delivery(self, delivery: dict) -> dict:
        """按退役清单核定投递的下一步：重发、补交付还是终止。"""
        version_no = delivery.get("投递版本", "")
        version = next(
            (v for v in store.rows(self.MODULE) if v.get("版本编号") == version_no),
            None,
        )
        record = self._retirement_record(version) if version else None
        if version is None:
            return {"decision": "unknown_version", "message": "投递引用的版本不存在"}
        if record is None:
            return {"decision": "continue", "message": "快照未退役，按原计划投递"}
        terminal = delivery.get("投递终态")
        if terminal in FINAL_DELIVERY_STATES:
            return {
                "decision": "terminal",
                "message": f"投递已是终态 {terminal}，不再重发，凭原投递记录追溯",
            }
        if record["status"] != "已结案":
            return {
                "decision": "hold",
                "retirement": record["记录编号"],
                "message": f"快照退役中，投递挂起，等待清单{record['记录编号']}核定",
            }
        if record.get("结案动作") == DELIVERY_ABANDON_ACTION or (
            version.get("status") == "已退役" and delivery.get("status") == "已终止"
        ):
            return {
                "decision": "terminate",
                "retirement": record["记录编号"],
                "message": "快照已退役且该投递按废弃终止，接班人不得重发这一版",
            }
        return {
            "decision": "terminate",
            "retirement": record["记录编号"],
            "message": (
                f"快照已退役，这一版不再可领用；该投递未拿到完成回执，"
                "按清单终止后换新版本重采"
            ),
        }

    def verify_delivery_credentials(self, delivery_id: int) -> dict:
        """接班人复核：数据版本 + 采集凭据齐备才允许重发。"""
        delivery = next(
            (d for d in store.rows("data_delivery") if d["id"] == delivery_id), None
        )
        if delivery is None:
            return {"ok": False, "message": f"投递管道 {delivery_id} 不存在"}
        version_no = delivery.get("投递版本", "")
        version = next(
            (v for v in store.rows(self.MODULE) if v.get("版本编号") == version_no), None
        )
        receipt = delivery.get("投递回执", "")
        issues: list[str] = []
        if version is None:
            issues.append(f"找不到投递引用的数据版本 {version_no or '(未登记)'}")
        if not receipt:
            issues.append("没有采集凭据（完成回执或终止回执）")
        if delivery.get("投递终态") is None and delivery.get("status") in ("已完成",):
            issues.append("投递已标记完成但没有投递终态凭据")
        verdict = self.next_step_for_delivery(delivery)
        redeliverable = not issues and verdict["decision"] in ("redeliver", "continue")
        return {
            "ok": not issues,
            "can_redeliver": redeliverable,
            "version": version,
            "receipt": receipt,
            "terminal_state": delivery.get("投递终态"),
            "retirement": (
                self._retirement_record(version) if version else None
            ),
            "decision": verdict,
            "issues": issues,
            "message": (
                verdict["message"] if not issues
                else "；".join(issues)
            ),
        }

    # ---------- 原有联动 ----------

    def _sync_delivery_and_subscribers(self, version: dict):
        """通知投递管道和订阅方版本已可用；已退役的版本不再唤醒任何下游。"""
        if version.get("status") in RETIRED_STATES:
            return
        source = version.get("数据来源", "")
        for delivery in store.rows("data_delivery"):
            if delivery.get("数据来源") == source and delivery.get("status") != "已终止":
                delivery["status"] = "投递中"
                delivery["pending"] = True
        for sub in store.rows("subscriber"):
            if sub.get("数据主题") == source and sub.get("status") != "已停订":
                sub["消费状态"] = "待消费"
                sub["最后推送"] = "2026-11-17"

    def _log_version_event(self, version: dict, event: str):
        log_rows = store.rows("call_log")
        vid = str(version["id"]).zfill(4)
        log_rows.append({
            "id": max((int(r.get("id", 0)) for r in log_rows), default=0) + 1,
            "记录编号": f"CALL-VER-{vid}",
            "接口端点": version.get("数据来源", ""),
            "请求参数": f"version_event:{event}",
            "响应状态": "200",
            "响应耗时(ms)": 30,
            "返回行数": version.get("行数统计", 0),
            "错误信息": "",
            "调用时间": "2026-11-17",
            "status": "成功", "pending": False, "abnormal": False,
        })

    def _create_govern_report(self, version: dict):
        gov_rows = store.rows("govern_report")
        vid = str(version["id"]).zfill(4)
        gov_rows.append({
            "id": max((int(r.get("id", 0)) for r in gov_rows), default=0) + 1,
            "报告编号": f"GOVE-VER-{vid}",
            "评估范围": f"数据版本标注:{version.get('数据来源', '')}",
            "合规指标": "版本标注规范",
            "当前得分": 90.0,
            "改进事项": f"版本{vid}已标注为{version.get('版本标签', '')}",
            "评估周期": "2026-Q4",
            "负责人": "数据标注组",
            "生成时间": "2026-11-17",
            "status": "已生成", "pending": False, "abnormal": False,
        })

    def _audit_obsolescence(self, version: dict, record: dict):
        """退役结案写入审计链；只在结案那一次写，重试不追加。"""
        audit_rows = store.rows("pipeline_audit")
        if any(
            a.get("记录编号") == record["记录编号"]
            for a in audit_rows
        ):
            return
        audit_rows.append({
            "id": max((int(r.get("id", 0)) for r in audit_rows), default=0) + 1,
            "记录编号": record["记录编号"],
            "管道名称": f"data_version:{version['id']:04d}",
            "操作人": "system",
            "操作类型": "退役",
            "变更摘要": (
                f"快照{version.get('版本编号', '')}退役，关联投递{record['关联投递']}笔，"
                f"已交付{record['已交付']}笔，已终止{record['已终止']}笔"
            ),
            "操作时间": "2026-11-17",
            "来源节点": "data_version",
            "审计结论": "已结案",
            "status": "已记录", "pending": False, "abnormal": True,
        })

    def check_version_lineage(self, source: str) -> dict:
        """跨模块版本谱系检查。"""
        versions = [v for v in store.rows("data_version") if v.get("数据来源") == source]
        deliveries = [d for d in store.rows("data_delivery") if d.get("数据来源") == source]
        subscribers = [s for s in store.rows("subscriber") if s.get("数据主题") == source]
        ledger = store.rows(RETIREMENT_LEDGER)
        return {
            "source": source,
            "versions": len(versions),
            "active_versions": sum(1 for v in versions if v.get("status") == "可用"),
            "retiring_versions": sum(1 for v in versions if v.get("status") == "退役中"),
            "retired_versions": sum(1 for v in versions if v.get("status") == "已退役"),
            "retirement_records": len(ledger),
            "open_retirements": sum(1 for r in ledger if r.get("status") != "已结案"),
            "delivery_pipelines": len(deliveries),
            "subscribers": len(subscribers),
        }
