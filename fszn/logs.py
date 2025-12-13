from __future__ import annotations

from datetime import datetime
from typing import Optional, Dict, Any

import json
from flask import Blueprint, render_template, request

from .auth import login_required, staff_required
from .models import OperationLog, User, Contract

logs_bp = Blueprint('logs', __name__, url_prefix='/logs')

# 类型 / 动作的中文显示
OBJECT_TYPE_LABELS: Dict[str, str] = {
    "task": "任务",
    "procurement": "采购",
    "acceptance": "验收",
    "feedback": "客户反馈",
    "contract": "合同",
    "file": "文件",
}

ACTION_LABELS: Dict[str, str] = {
    "create": "创建",
    "update": "修改",
    "delete": "删除",
    "status_change": "状态变更",
    "upload": "上传",
    "resolve": "标记解决",
    "notify": "发送通知",
}

# 字段中文名，用于详情展示
FIELD_LABELS: Dict[str, str] = {
    "status": "状态",
    "item_name": "物料名称",
    "quantity": "数量",
    "unit": "单位",
    "expected_date": "期望日期",
    "planned_delivery_date": "计划交付日期（项目）",
    "content": "内容",
    "handler_id": "处理人 ID",
    "result": "处理结果",
    "completion_time": "完成时间",
    "is_resolved": "是否已解决",
    "file_type": "文件类型",
    "version": "版本号",
    "original_filename": "原始文件名",
    "stored_filename": "存储文件名",
    "is_public": "是否对客户可见",
    "stage_name": "验收阶段",
    "person_id": "验收人 ID",
    "date": "验收日期",
    "remarks": "备注",
}


def _parse_date(s: Optional[str]):
    """简单的日期解析：期望格式 YYYY-MM-DD，不合法返回 None。"""
    if not s:
        return None
    try:
        return datetime.strptime(s, '%Y-%m-%d').date()
    except ValueError:
        return None


def _label(field: str) -> str:
    """把字段名转为中文名。"""
    return FIELD_LABELS.get(field, field)


def _fmt_value(v: Any) -> str:
    """统一格式化日志中的值，方便阅读。"""
    if isinstance(v, bool):
        return "是" if v else "否"
    return "" if v is None else str(v)


def _build_detail_display(log: OperationLog) -> str:
    """
    根据 detail_json 生成适合人看的说明，例如：
    “状态：未开始 → 进行中； 完成时间：None → 2025-01-01 10:00:00”
    """
    if not log.detail_json:
        return ""

    try:
        data = json.loads(log.detail_json)
    except Exception:
        # 解析失败就直接原样显示
        return log.detail_json

    old = data.get("old")
    new = data.get("new")
    pieces = []

    if old is not None and new is not None:
        keys = sorted(set(old.keys()) | set(new.keys()))
        for k in keys:
            ov = old.get(k)
            nv = new.get(k)
            if ov == nv:
                continue
            pieces.append(f"{_label(k)}：{_fmt_value(ov)} → {_fmt_value(nv)}")
    elif new is not None:
        for k, v in new.items():
            pieces.append(f"{_label(k)}：{_fmt_value(v)}")
    elif old is not None:
        for k, v in old.items():
            pieces.append(f"{_label(k)}：{_fmt_value(v)}")

    return "\n ".join(pieces)


def _enrich_logs(logs):
    """给日志结果增加中文说明和格式化详情。"""
    for log in logs:
        log.object_type_label = OBJECT_TYPE_LABELS.get(log.object_type, log.object_type)
        log.action_label = ACTION_LABELS.get(log.action, log.action)
        log.detail_display = _build_detail_display(log)
    return logs


@logs_bp.route('/', methods=['GET'])
@login_required
@staff_required
def list_logs():
    """全局操作日志列表（仅内部员工可见）。"""
    return _render_logs_page(contract=None)


@logs_bp.route('/contract/<int:contract_id>/', methods=['GET'])
@login_required
@staff_required
def contract_logs(contract_id: int):
    """单个合同的操作日志列表。"""
    contract = Contract.query.get_or_404(contract_id)
    return _render_logs_page(contract=contract)


def _render_logs_page(contract: Optional[Contract]):
    """统一渲染日志列表页，contract=None 表示全局日志。"""
    # 过滤条件从 query string 获取
    object_type = (request.args.get('object_type') or '').strip()
    action = (request.args.get('action') or '').strip()
    operator_id = (request.args.get('operator_id') or '').strip()
    object_id = (request.args.get('object_id') or '').strip()
    date_from_str = (request.args.get('date_from') or '').strip()
    date_to_str = (request.args.get('date_to') or '').strip()

    date_from = _parse_date(date_from_str)
    date_to = _parse_date(date_to_str)

    # 基础查询
    query = OperationLog.query
    if contract is not None:
        query = query.filter(OperationLog.contract_id == contract.id)

    if object_type:
        query = query.filter(OperationLog.object_type == object_type)

    if action:
        query = query.filter(OperationLog.action == action)

    if operator_id:
        try:
            op_id_int = int(operator_id)
            query = query.filter(OperationLog.operator_id == op_id_int)
        except ValueError:
            pass

    if object_id:
        try:
            obj_id_int = int(object_id)
            query = query.filter(OperationLog.object_id == obj_id_int)
        except ValueError:
            pass

    if date_from:
        query = query.filter(OperationLog.created_at >= datetime.combine(date_from, datetime.min.time()))
    if date_to:
        query = query.filter(OperationLog.created_at <= datetime.combine(date_to, datetime.max.time()))

    logs = query.order_by(OperationLog.created_at.desc()).limit(200).all()
    _enrich_logs(logs)

    operators = User.query.order_by(User.username).all()

    object_types_all = list(OBJECT_TYPE_LABELS.keys())
    actions_all = list(ACTION_LABELS.keys())

    filters = dict(
        object_type=object_type,
        action=action,
        operator_id=operator_id,
        object_id=object_id,
        date_from=date_from_str,
        date_to=date_to_str,
    )

    return render_template(
        'logs/list.html',
        logs=logs,
        operators=operators,
        object_types_all=object_types_all,
        actions_all=actions_all,
        filters=filters,
        contract=contract,
        object_type_labels=OBJECT_TYPE_LABELS,  # ★ 新增
        action_labels=ACTION_LABELS,            # ★ 新增
    )

