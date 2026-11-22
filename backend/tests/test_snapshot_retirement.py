"""快照退役与投递终态的验收脚本。

只依赖标准库 + 仓库自带的 venv，不引入 pytest：
    cd backend && .venv/bin/python tests/test_snapshot_retirement.py

覆盖题面要求：
1. 同一投递上“废弃申请 + 完成回执”竞争，只确认一个终态；
2. 退役中断后重试不复裂清单、不重复结案、不重复审计；
3. 退役后不能再把这一版重新列为可领用；
4. 已发数据仍能追溯到原版本和终态，后续废弃不覆盖交付凭据；
5. 退役清单回写在途任务结果，接班人凭版本 + 采集凭据复核能否重发。
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import service_for  # noqa: E402
from app.store import store  # noqa: E402

VERSIONS = service_for("data_version")
DELIVERIES = service_for("data_delivery")
RETIREMENTS = service_for("snapshot_retirement")
AUDITS = service_for("pipeline_audit")


def fresh() -> None:
    store.reset()


def new_version(no: str, source: str) -> dict:
    version, missing = VERSIONS.create_entry(
        {"版本编号": no, "数据来源": source, "行数统计": "1200"}
    )
    assert not missing, missing
    version, message = VERSIONS.run_action(version["id"], "创建快照")
    assert version["status"] == "可用", message
    return version


def new_delivery(no: str, source: str, version_no: str) -> dict:
    delivery, missing = DELIVERIES.create_entry(
        {
            "管道编号": no,
            "数据来源": source,
            "目标地址": f"https://sink.example.com/{no}",
            "投递版本": version_no,
        }
    )
    assert not missing, missing
    return delivery


def audit_count(record_no: str) -> int:
    return sum(1 for row in store.rows("pipeline_audit") if row["记录编号"] == record_no)


def ledger_count() -> int:
    return len(store.rows("snapshot_retirement"))


def case_retire_after_start_then_receipt() -> None:
    """启动 V7 投递暂不签收 → 发起退役 → 旧投递补回执 → 结案后可追溯且不可再领。"""
    fresh()
    version = new_version("DATA-V7", "采集主题V7")
    delivery = new_delivery("PIPE-V7", "采集主题V7", "DATA-V7")
    delivery, message = DELIVERIES.run_action(delivery["id"], "启动投递")
    assert delivery["status"] == "投递中", message
    assert delivery["投递领用"] == "DATA-V7"
    assert not delivery["投递终态"], "启动后不应提前写终态"

    version, message = VERSIONS.run_action(version["id"], "发起退役")
    assert version["status"] == "退役中", message
    record = VERSIONS.retirement_status(version["id"])["retirement"]
    assert record["关联投递"] == 1 and record["待核定"] == 1, record
    assert DELIVERIES.next_step(delivery["id"])["next"]["decision"] == "hold"

    delivery, message = DELIVERIES.run_action(delivery["id"], "提交完成回执")
    assert delivery["投递终态"] == "已交付", message
    assert delivery["投递回执"].startswith("completed:"), delivery
    record = VERSIONS.retirement_status(version["id"])["retirement"]
    assert record["待核定"] == 0 and record["已交付"] == 1, record

    version, message = VERSIONS.run_action(version["id"], "废弃版本")
    assert version["status"] == "已退役", message
    record = VERSIONS.retirement_status(version["id"])["retirement"]
    assert record["status"] == "已结案" and record["结案动作"] == "废弃版本"

    # 已发数据仍能找到原版本和终态。
    credentials = VERSIONS.verify_delivery_credentials(delivery["id"])
    assert credentials["ok"] and credentials["terminal_state"] == "已交付"
    assert credentials["version"]["版本编号"] == "DATA-V7"
    assert credentials["can_redeliver"] is False

    # 新任务不能再领这一版；旧投递也不能被重新启动。
    fresh_delivery = new_delivery("PIPE-V7-B", "采集主题V7", "DATA-V7")
    fresh_delivery, message = DELIVERIES.run_action(fresh_delivery["id"], "启动投递")
    assert fresh_delivery["status"] == "待配置", message
    assert "已退役" in message or "不再接受" in message, message
    delivery, message = DELIVERIES.run_action(delivery["id"], "启动投递")
    assert delivery["投递终态"] == "已交付", "重发不得推翻已有终态"


def case_competing_events_only_one_terminal() -> None:
    """废弃申请先到，完成回执后到：只确认一个终态，凭据不被覆盖。"""
    fresh()
    version = new_version("DATA-RACE", "采集主题RACE")
    delivery = new_delivery("PIPE-RACE", "采集主题RACE", "DATA-RACE")
    DELIVERIES.run_action(delivery["id"], "启动投递")
    VERSIONS.run_action(version["id"], "发起退役")

    delivery, message = DELIVERIES.run_action(delivery["id"], "申请废弃")
    assert delivery["投递终态"] == "已终止", message
    receipt = delivery["投递回执"]
    delivery, message = DELIVERIES.run_action(delivery["id"], "提交完成回执")
    assert delivery["投递终态"] == "已终止", message
    assert delivery["投递回执"] == receipt, "后到事件不得覆盖先到凭据"
    assert "竞争说明" in delivery
    assert "只确认一个终态" in message, message

    # 再重复一次废弃申请也不重复结案。
    delivery, message = DELIVERIES.run_action(delivery["id"], "申请废弃")
    assert "不重复结案" in message, message


def case_retry_after_interruption() -> None:
    """退役中断后重试：清单不裂、审计只一条、结案动作不被覆盖。"""
    fresh()
    version = new_version("DATA-RETRY", "采集主题RETRY")
    VERSIONS.run_action(version["id"], "发起退役")
    assert ledger_count() == 1
    VERSIONS.run_action(version["id"], "发起退役")  # 中断后重复发起
    assert ledger_count() == 1, "重复发起不得新建退役清单"

    VERSIONS.run_action(version["id"], "废弃版本")
    record = VERSIONS.retirement_status(version["id"])["retirement"]
    assert record["status"] == "已结案"
    for _ in range(3):
        VERSIONS.run_action(version["id"], "废弃版本")
    record = VERSIONS.retirement_status(version["id"])["retirement"]
    assert record["重试次数"] == 3, record
    assert record["结案动作"] == "废弃版本"
    assert audit_count(record["记录编号"]) == 1, "重试不得重复结案/重复审计"
    assert ledger_count() == 1


def case_concurrent_competition() -> None:
    """并发提交两个终态事件：最终只留一个终态。"""
    fresh()
    version = new_version("DATA-THREAD", "采集主题THREAD")
    delivery = new_delivery("PIPE-THREAD", "采集主题THREAD", "DATA-THREAD")
    DELIVERIES.run_action(delivery["id"], "启动投递")
    VERSIONS.run_action(version["id"], "发起退役")
    errors: list[str] = []

    def submit(action: str) -> None:
        try:
            DELIVERIES.run_action(delivery["id"], action)
        except Exception as exc:  # pragma: no cover - 失败才触发
            errors.append(repr(exc))

    threads = [
        threading.Thread(target=submit, args=(action,))
        for action in ("提交完成回执", "申请废弃") * 4
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors, errors
    assert delivery["投递终态"] in ("已交付", "已终止")
    states = {ref["终态"] for ref in VERSIONS.retirement_status(version["id"])["retirement"]["投递引用"]}
    assert states == {delivery["投递终态"]}, states


def case_receiver_freeze_and_next_step() -> None:
    """退役影响接收方领用；清单给出在途任务下一步。"""
    fresh()
    version = new_version("DATA-SUB", "采集主题SUB")
    subscriber, _ = service_for("subscriber").create_entry(
        {"订阅编号": "SUBS-V7", "订阅方": "下游消费方", "数据主题": "采集主题SUB"}
    )
    subscriber["消费状态"] = "待消费"
    delivery = new_delivery("PIPE-SUB", "采集主题SUB", "DATA-SUB")
    DELIVERIES.run_action(delivery["id"], "启动投递")

    VERSIONS.run_action(version["id"], "发起退役")
    assert subscriber["消费状态"] == "领用冻结", subscriber
    assert subscriber["冻结版本"] == "DATA-SUB"

    record = VERSIONS.retirement_status(version["id"])["retirement"]
    balance = RETIREMENTS.balance(record["id"])
    assert balance["ok"]
    assert balance["steps"][0]["decision"] == "hold"
    assert balance["steps"][0]["投递ID"] == delivery["id"]


def main() -> int:
    cases = [
        case_retire_after_start_then_receipt,
        case_competing_events_only_one_terminal,
        case_retry_after_interruption,
        case_concurrent_competition,
        case_receiver_freeze_and_next_step,
    ]
    for case in cases:
        case()
        print(f"PASS {case.__name__}")
    print(f"{len(cases)} cases passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
