"""数据清洗业务规则：状态流转、字段校验与筛选口径都收在这里。

清洗主流程：登记（契约字段一起落库）→ 提交校验 → 启用 → 按批次执行清洗
（空值策略 + 格式校验 + 异常过滤 + 去重）→ 清洗后的数据集同步写入清洗字段台账。
执行被中断时保留断点，下次执行从最后完整批次继续写入。
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from app.store import store

MODULE = "cleanse"
SOURCE_TABLE = "cleanse_source"    # 待清洗的源记录
RESULT_TABLE = "cleanse_result"    # 清洗后的数据集
ABNORMAL_TABLE = "cleanse_abnormal"  # 异常过滤记录
BATCH_TABLE = "cleanse_batch"      # 批次与断点
LEDGER_TABLE = "cleanse_ledger"    # 清洗字段台账（只存标准化记录）

ENTRY_FIELDS = ["规则编号", "关联数据集", "空值策略", "格式校验", "异常过滤", "去重字段", "规则版本", "生效日期"]
REQUIRED_FIELDS = ["规则编号", "关联数据集", "空值策略"]
# 规则契约字段：登记时一起落库，启用前必须齐全，否则执行时无法区分正常记录与异常过滤记录
CONTRACT_FIELDS = ["空值策略", "格式校验", "异常过滤", "规则版本"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用规则": "已启用", "停用规则": "已停用"}
# 状态机：每个动作只允许从指定前置状态发起，禁止草稿直接启用
TRANSITION_SOURCES = {"提交校验": {"草稿"}, "启用规则": {"待校验"}, "停用规则": {"已启用"}}
NEGATIVE_ACTIONS = ["停用规则"]

META_FIELDS = {"id", "数据集", "来源批次"}
AMOUNT_PATTERN = re.compile(r"^-?\d+(\.\d{1,2})?$")
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# 格式校验登记册：校验名 -> (校验字段, 合法模式, 未通过原因)
FORMAT_CHECKS: dict[str, tuple[str, re.Pattern[str], str]] = {
    "金额格式校验": ("金额", AMOUNT_PATTERN, "金额格式校验未通过"),
    "日期格式校验": ("日期", DATE_PATTERN, "日期格式校验未通过"),
}


def _is_empty(value: Any) -> bool:
    return value is None or str(value).strip() == ""


def _version_key(text: Any) -> tuple[int, ...]:
    """把 V2、v1.2 这类版本号转成可比较的元组，缺省按 0 处理。"""
    nums = tuple(int(part) for part in re.findall(r"\d+", str(text or "")))
    return nums or (0,)


class CleanseService:
    # ---------- 列表与明细 ----------

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("规则编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        dataset = str(entry.get("关联数据集") or "")
        batches = [row for row in store.rows(BATCH_TABLE) if row.get("数据集") == dataset]
        # 明细页需要复核口径：规则字段 + 该数据集最近一次执行的结果统计
        summary = {
            "已清洗条数": sum(1 for row in store.rows(RESULT_TABLE) if row.get("数据集") == dataset),
            "异常过滤条数": sum(1 for row in store.rows(ABNORMAL_TABLE) if row.get("数据集") == dataset),
            "已完成批次": [row.get("来源批次") for row in batches if row.get("状态") == "已完成"],
            "最近批次状态": batches[-1]["状态"] if batches else "未执行",
            "最近批次原因": batches[-1].get("原因", "") if batches else "",
        }
        return {**entry, "执行摘要": summary}

    # ---------- 登记：契约字段一起落库 ----------

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if _is_empty(values.get(field))]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        # 规则版本、空值策略、格式校验等契约字段必须与必填字段一起落库，
        # 不能只存必填字段而把契约字段丢掉
        for field in ENTRY_FIELDS:
            value = values.get(field)
            entry[field] = str(value).strip() if value is not None else ""
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    # ---------- 状态流转：只允许沿状态机走 ----------

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"清洗规则 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据清洗可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        current = str(entry.get("status") or STATUS_ORDER[0])
        allowed_sources = TRANSITION_SOURCES[action]
        if current not in allowed_sources:
            expect = "、".join(sorted(allowed_sources))
            return None, f"当前状态为「{current}」，仅「{expect}」状态可执行{action}，请按草稿→待校验→已启用流转"
        if action == "启用规则":
            lack = [field for field in CONTRACT_FIELDS if _is_empty(entry.get(field))]
            if lack:
                return None, f"启用前需补齐规则契约字段：{'、'.join(lack)}，否则执行时无法区分正常记录与异常过滤记录"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"清洗规则已{action}"

    # ---------- 版本仲裁：同一数据集多版本时给出唯一适用规则 ----------

    def resolve_rule(self, dataset: str, batch_date: str) -> tuple[dict[str, Any] | None, str]:
        """按生效日期仲裁：不晚于批次日期的已启用规则里，生效日期最新者优先；
        生效日期并列时规则版本高者优先，再并列 id 大者优先。边界日期上新旧规则
        同时适用时，新规则（生效日期更晚）胜出，保证批次只有唯一清洗结果。"""
        candidates = [
            row for row in store.rows(MODULE)
            if row.get("关联数据集") == dataset
            and row.get("status") == "已启用"
            and not _is_empty(row.get("生效日期"))
            and str(row.get("生效日期")) <= batch_date
        ]
        if not candidates:
            enabled = [
                row for row in store.rows(MODULE)
                if row.get("关联数据集") == dataset and row.get("status") == "已启用"
            ]
            if enabled:
                earliest = min(str(row.get("生效日期")) for row in enabled)
                return None, f"批次日期 {batch_date} 早于规则最早生效日期 {earliest}，没有可适用的清洗规则"
            return None, f"数据集「{dataset}」没有已启用的清洗规则，请先登记并启用规则"
        winner = max(
            candidates,
            key=lambda row: (str(row.get("生效日期")), _version_key(row.get("规则版本")), int(row.get("id", 0))),
        )
        if len(candidates) > 1:
            others = "、".join(
                f"{row.get('规则编号')}({row.get('规则版本')})" for row in candidates if row is not winner
            )
            note = f"同一数据集存在多个适用规则，按生效日期+规则版本仲裁采用 {winner.get('规则编号')}({winner.get('规则版本')})，未采用：{others}"
        else:
            note = "唯一适用规则"
        return winner, note

    # ---------- 清洗执行：按批次处理，断点续传，结果同步台账 ----------

    def execute(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str, bool]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"清洗规则 {entry_id} 不存在或已归档", False
        dataset = str(entry.get("关联数据集") or "")
        batch_date = str(values.get("批次日期") or date.today().isoformat())
        rule, note = self.resolve_rule(dataset, batch_date)
        if rule is None:
            return None, note, False

        if values.get("重新执行"):
            self._reset_dataset(dataset)

        sources = [
            row for row in store.rows(SOURCE_TABLE)
            if row.get("数据集") == dataset
        ]
        sources.sort(key=lambda row: int(row.get("id", 0)))
        if not sources:
            return {
                "数据集": dataset,
                "批次日期": batch_date,
                "执行状态": "空数据",
                "清洗条数": 0,
                "异常条数": 0,
            }, f"数据集「{dataset}」没有待清洗的源记录", True

        # 按来源批次分组，保持源记录顺序；批次是断点恢复的最小单位
        batch_order: list[str] = []
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in sources:
            name = str(row.get("来源批次") or "未标注批次")
            if name not in grouped:
                grouped[name] = []
                batch_order.append(name)
            grouped[name].append(row)

        done = {
            str(row.get("来源批次"))
            for row in store.rows(BATCH_TABLE)
            if row.get("数据集") == dataset and row.get("状态") == "已完成"
        }
        pending_batches = [name for name in batch_order if name not in done]

        interrupt_at = str(values.get("中断演练批次") or "").strip()
        if not interrupt_at and values.get("中断演练") and pending_batches:
            interrupt_at = pending_batches[0]

        seen_keys = self._seen_keys(dataset, rule)
        report = {
            "数据集": dataset,
            "批次日期": batch_date,
            "采用规则编号": rule.get("规则编号"),
            "采用规则版本": rule.get("规则版本"),
            "生效日期": rule.get("生效日期"),
            "仲裁说明": note,
            "跳过批次": sorted(done),
            "完成批次": [],
            "清洗条数": 0,
            "异常条数": 0,
            "执行状态": "已完成",
        }
        if not pending_batches:
            report["清洗条数"] = sum(1 for row in store.rows(RESULT_TABLE) if row.get("数据集") == dataset)
            report["异常条数"] = sum(1 for row in store.rows(ABNORMAL_TABLE) if row.get("数据集") == dataset)
            return report, f"数据集「{dataset}」所有来源批次均已完成，无待处理批次", True

        for name in pending_batches:
            if interrupt_at and name == interrupt_at:
                reason = f"批次 {name} 执行超时被中断，已保留断点，可从断点重试"
                self._record_batch(dataset, name, rule, "已中断", grouped[name], 0, 0, reason)
                report["执行状态"] = "已中断"
                report["中断原因"] = reason
                return report, reason, False
            try:
                cleaned, abnormal = self._cleanse_records(grouped[name], rule, seen_keys)
            except Exception as exc:  # 单批次失败：记录原因并停在断点，已完成的批次不回滚
                reason = f"批次 {name} 执行失败：{exc}"
                self._record_batch(dataset, name, rule, "失败", grouped[name], 0, 0, reason)
                report["执行状态"] = "失败"
                report["失败原因"] = reason
                return report, reason, False
            self._append_rows(RESULT_TABLE, [
                self._result_row(dataset, name, rule, row) for row in cleaned
            ])
            self._append_rows(ABNORMAL_TABLE, [
                self._abnormal_row(dataset, name, rule, row, reason)
                for row, reason in abnormal
            ])
            # 跨模块契约：清洗后的数据集同步写入清洗字段台账，后续查询只读标准化记录
            self._append_rows(LEDGER_TABLE, [
                self._ledger_row(dataset, name, rule, row) for row in cleaned
            ])
            self._record_batch(dataset, name, rule, "已完成", grouped[name], len(cleaned), len(abnormal), "")
            report["完成批次"].append(name)
            report["清洗条数"] += len(cleaned)
            report["异常条数"] += len(abnormal)

        return report, (
            f"清洗完成：采用规则 {rule.get('规则编号')}({rule.get('规则版本')})，"
            f"完成 {len(report['完成批次'])} 个批次，清洗 {report['清洗条数']} 条，"
            f"异常过滤 {report['异常条数']} 条，结果已同步清洗字段台账"
        ), True

    # ---------- 结果读取：清洗结果、异常记录、批次、台账 ----------

    def list_results(self, *, dataset: str | None, page: int, size: int) -> tuple[list[dict[str, Any]], int]:
        return self._page(self._filter_dataset(RESULT_TABLE, dataset), page, size)

    def list_abnormal(self, *, dataset: str | None, page: int, size: int) -> tuple[list[dict[str, Any]], int]:
        return self._page(self._filter_dataset(ABNORMAL_TABLE, dataset), page, size)

    def list_batches(self, *, dataset: str | None, page: int, size: int) -> tuple[list[dict[str, Any]], int]:
        return self._page(self._filter_dataset(BATCH_TABLE, dataset), page, size)

    def list_ledger(self, *, dataset: str | None, page: int, size: int) -> tuple[list[dict[str, Any]], int]:
        return self._page(self._filter_dataset(LEDGER_TABLE, dataset), page, size)

    # ---------- 内部工具 ----------

    def _cleanse_records(
        self,
        records: list[dict[str, Any]],
        rule: dict[str, Any],
        seen_keys: set[Any],
    ) -> tuple[list[dict[str, Any]], list[tuple[dict[str, Any], str]]]:
        """按完整空值与格式规则清洗一个批次的源记录。

        返回 (清洗后的记录, [(异常记录, 过滤原因)])；异常记录不进清洗结果。
        """
        null_strategy = str(rule.get("空值策略") or "")
        dedupe_field = str(rule.get("去重字段") or "").strip()
        keep_abnormal = "保留" in str(rule.get("异常过滤") or "")
        checks = self._parse_checks(str(rule.get("格式校验") or ""))

        cleaned: list[dict[str, Any]] = []
        abnormal: list[tuple[dict[str, Any], str]] = []
        for source in records:
            row = {key: value for key, value in source.items() if key not in META_FIELDS}
            empties = [key for key, value in row.items() if _is_empty(value)]
            if empties and "剔除" in null_strategy:
                abnormal.append((row, f"空值记录按策略剔除（空值字段：{'、'.join(empties)}）"))
                continue
            for key in empties:
                row[key] = "0" if "替换为0" in null_strategy or not null_strategy else ""
            reason = next(
                (message for field, pattern, message in checks if not pattern.match(str(row.get(field) or ""))),
                None,
            )
            if reason is None and dedupe_field:
                key = row.get(dedupe_field)
                if key in seen_keys:
                    reason = f"去重字段「{dedupe_field}」重复：{key}"
                else:
                    seen_keys.add(key)
            if reason is not None:
                abnormal.append((row, reason))
                if keep_abnormal:
                    kept = dict(row)
                    kept["异常标记"] = True
                    cleaned.append(kept)
                continue
            if keep_abnormal:
                row["异常标记"] = False
            cleaned.append(row)
        return cleaned, abnormal

    def _parse_checks(self, text: str) -> list[tuple[str, re.Pattern[str], str]]:
        parts = [part.strip() for part in re.split(r"[、,，;；/]+", text) if part.strip()]
        return [FORMAT_CHECKS[part] for part in parts if part in FORMAT_CHECKS]

    def _seen_keys(self, dataset: str, rule: dict[str, Any]) -> set[Any]:
        dedupe_field = str(rule.get("去重字段") or "").strip()
        if not dedupe_field:
            return set()
        return {
            row.get(dedupe_field)
            for row in store.rows(RESULT_TABLE)
            if row.get("数据集") == dataset
        }

    def _result_row(self, dataset: str, batch: str, rule: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
        return {
            "数据集": dataset,
            "来源批次": batch,
            "规则编号": rule.get("规则编号"),
            "规则版本": rule.get("规则版本"),
            "清洗时间": datetime.now().isoformat(timespec="seconds"),
            **row,
        }

    def _abnormal_row(
        self,
        dataset: str,
        batch: str,
        rule: dict[str, Any],
        row: dict[str, Any],
        reason: str,
    ) -> dict[str, Any]:
        return {
            "数据集": dataset,
            "来源批次": batch,
            "规则编号": rule.get("规则编号"),
            "规则版本": rule.get("规则版本"),
            "过滤原因": reason,
            "过滤时间": datetime.now().isoformat(timespec="seconds"),
            "原始记录": dict(row),
        }

    def _ledger_row(self, dataset: str, batch: str, rule: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
        return {
            "数据集": dataset,
            "来源批次": batch,
            "规则版本": rule.get("规则版本"),
            "标准化": True,
            "同步时间": datetime.now().isoformat(timespec="seconds"),
            **row,
        }

    def _record_batch(
        self,
        dataset: str,
        batch: str,
        rule: dict[str, Any],
        status: str,
        records: list[dict[str, Any]],
        cleaned: int,
        abnormal: int,
        reason: str,
    ) -> None:
        rows = store.rows(BATCH_TABLE)
        rows.append({
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "数据集": dataset,
            "来源批次": batch,
            "规则编号": rule.get("规则编号"),
            "规则版本": rule.get("规则版本"),
            "状态": status,
            "源记录数": len(records),
            "清洗条数": cleaned,
            "异常条数": abnormal,
            "原因": reason,
            "记录时间": datetime.now().isoformat(timespec="seconds"),
        })

    def _reset_dataset(self, dataset: str) -> None:
        for table in (RESULT_TABLE, ABNORMAL_TABLE, BATCH_TABLE, LEDGER_TABLE):
            kept = [row for row in store.rows(table) if row.get("数据集") != dataset]
            store.rows(table)[:] = kept

    def _filter_dataset(self, table: str, dataset: str | None) -> list[dict[str, Any]]:
        rows = list(store.rows(table))
        if dataset:
            rows = [row for row in rows if row.get("数据集") == dataset]
        return rows

    def _page(self, rows: list[dict[str, Any]], page: int, size: int) -> tuple[list[dict[str, Any]], int]:
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def _append_rows(self, table: str, rows: list[dict[str, Any]]) -> None:
        store.rows(table).extend(rows)
