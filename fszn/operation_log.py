# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from flask import Request

from . import db
from .models import OperationLog, User, Contract

# ---- 对象类型常量 ----
OBJECT_TYPE_TASK = "task"
OBJECT_TYPE_PROCUREMENT = "procurement"
OBJECT_TYPE_ACCEPTANCE = "acceptance"
OBJECT_TYPE_FEEDBACK = "feedback"
OBJECT_TYPE_CONTRACT = "contract"
OBJECT_TYPE_FILE = "file"

# ---- 动作类型常量 ----
ACTION_CREATE = "create"
ACTION_UPDATE = "update"
ACTION_DELETE = "delete"
ACTION_STATUS_CHANGE = "status_change"
ACTION_UPLOAD = "upload"
ACTION_RESOLVE = "resolve"
ACTION_DOWNLOAD = "download"  # 新增：文件下载
ACTION_RESTORE = "restore"    # 新增：恢复已删除文件


def log_operation(
    *,
    operator: Optional[User] = None,
    object_type: str,
    object_id: int,
    action: str,
    old_data: Optional[Dict[str, Any]] = None,
    new_data: Optional[Dict[str, Any]] = None,
    request: Optional[Request] = None,
    contract_id: Optional[int] = None,
) -> OperationLog:
    """
    统一操作日志记录入口。

    :param operator: 执行操作的用户对象（可为 None）
    :param object_type: 对象类型，如 "task" / "feedback"
    :param object_id: 对象主键 ID
    :param action: 动作类型，如 "create" / "status_change"
    :param old_data: 变更前的数据快照（字典）
    :param new_data: 变更后的数据快照（字典）
    :param request: Flask 的 request 对象，用于获取 IP（可选）
    :param contract_id: 所属合同 ID（用于合同维度查看日志，可选）
    """
    ip_address = None
    if request is not None:
        ip_address = request.remote_addr

    detail: Dict[str, Any] = {}
    if old_data is not None:
        detail["old"] = old_data
    if new_data is not None:
        detail["new"] = new_data

    log = OperationLog(
        operator_id=operator.id if operator is not None else None,
        contract_id=contract_id,
        object_type=object_type,
        object_id=object_id,
        action=action,
        detail_json=json.dumps(detail, ensure_ascii=False) if detail else None,
        ip_address=ip_address,
    )

    db.session.add(log)
    db.session.commit()
    return log
