# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import date
from typing import Optional

from flask_sqlalchemy import SQLAlchemy

from ..models import Task, Contract


# 任务状态常量（与 models.Task.status 中的默认值兼容）
TASK_STATUS_NOT_STARTED = "未开始"
TASK_STATUS_IN_PROGRESS = "进行中"
TASK_STATUS_WAITING_QC = "待质检"
TASK_STATUS_COMPLETED = "已完成"
TASK_STATUS_PAUSED = "已暂停"


class ProductionService:
    """生产任务相关的业务服务。

    设计目标：
        - 把 Task 的创建 / 状态流转 / 简单的进度更新集中到 service 层
        - 视图层（blueprint）只做参数解析和渲染，不直接操作 db.session
        - 为后续接入验收 / 日志 / 通知等打基础
    """

    def __init__(self, db: SQLAlchemy) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # 任务创建
    # ------------------------------------------------------------------

    def create_task(
        self,
        contract: Contract,
        department_id: int,
        title: str,
        start_date: date,
        person_id: Optional[int] = None,
        end_date: Optional[date] = None,
        remarks: str = "",
    ) -> Task:
        """为给定合同创建一个生产任务。

        说明：
            - 创建任务时状态统一为“未开始”
            - 不再接收 status 参数
        """

        final_status = TASK_STATUS_NOT_STARTED

        task = Task(
            contract_id=contract.id,
            department_id=department_id,
            person_id=person_id,
            title=title.strip(),
            start_date=start_date,
            end_date=end_date,
            status=final_status,
            remarks=remarks or "",
        )


        self.db.session.add(task)
        self.db.session.commit()

        return task

    # ------------------------------------------------------------------
    # 通用状态流转
    # ------------------------------------------------------------------

    def change_status(
        self,
        task: Task,
        new_status: str,
        auto_set_end_date: bool = True,
    ) -> Task:
        """改变任务状态的通用方法。

        说明：
            - 用于封装所有状态变更逻辑
            - 视图层可以直接调用特定方法（start_task / complete_task 等）
        """

        task.status = new_status

        if auto_set_end_date and new_status == TASK_STATUS_COMPLETED:
            # 若未设置完成日期，则在标记为完成时写入当天
            if task.end_date is None:
                task.end_date = date.today()

        self.db.session.commit()
        return task

    # ------------------------------------------------------------------
    # 具体状态操作封装
    # ------------------------------------------------------------------

    def start_task(self, task: Task) -> Task:
        """开启任务：未开始 -> 进行中。"""
        return self.change_status(task, TASK_STATUS_IN_PROGRESS, auto_set_end_date=False)

    def mark_waiting_qc(self, task: Task) -> Task:
        """标记为待质检（视为进行中后的一个中间状态）。"""
        return self.change_status(task, TASK_STATUS_WAITING_QC, auto_set_end_date=False)

    def complete_task(self, task: Task) -> Task:
        """完成任务：将状态改为已完成，并根据需要设置 end_date。"""
        return self.change_status(task, TASK_STATUS_COMPLETED, auto_set_end_date=True)

    def pause_task(self, task: Task) -> Task:
        """暂停任务：用于异常中断 / 等待外部条件的情况。"""
        return self.change_status(task, TASK_STATUS_PAUSED, auto_set_end_date=False)

    def reset_to_not_started(self, task: Task) -> Task:
        """将任务状态恢复到未开始（例如误操作回滚）。"""
        return self.change_status(task, TASK_STATUS_NOT_STARTED, auto_set_end_date=False)
