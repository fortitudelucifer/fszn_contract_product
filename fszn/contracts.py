# -*- coding: utf-8 -*-

from __future__ import annotations
from functools import wraps
from datetime import datetime, date
import os
import csv
from io import StringIO

from flask import (
    Blueprint, render_template, request,
    redirect, url_for, flash, session, send_from_directory, current_app, make_response
)

from . import db
from .auth import login_required, staff_required
from .models import (
    Contract, Company, User,
    Department, Person, ProjectDepartmentLeader,
    Task, ProcurementItem, Acceptance, Feedback,
    SalesInfo, ProjectFile
)

from .services.procurement_service import ProcurementService
from .services.production_service import ProductionService
from .services.acceptance_service import AcceptanceService
from .services.feedback_service import FeedbackService
from .services.notification_service import DummyNotificationService

from .operation_log import (
    log_operation,
    OBJECT_TYPE_TASK,
    OBJECT_TYPE_PROCUREMENT,
    OBJECT_TYPE_ACCEPTANCE,
    OBJECT_TYPE_FEEDBACK,
    OBJECT_TYPE_CONTRACT,
    OBJECT_TYPE_FILE,
    ACTION_CREATE,
    ACTION_UPDATE,
    ACTION_DELETE,
    ACTION_STATUS_CHANGE,
    ACTION_UPLOAD,
    ACTION_RESOLVE,
)


# ====== 状态计算辅助函数（重写版：只看生产 / 验收 / 反馈） ======
def get_contract_status(contract: Contract):
    """根据任务、验收、反馈情况计算项目状态（不再依赖财务模块）。

    返回:
        (text, level)
        text  : 状态文本（如 "未启动"、"生产中"、"验收中"、"已验收"、"已验收-有未解决问题"）
        level : 用于前端上色的等级（如 "gray" / "blue" / "orange" / "green"）
    """
    cid = contract.id

    # 是否有任务 / 验收 / 未解决反馈
    has_tasks = Task.query.filter_by(contract_id=cid).count() > 0
    acceptances = Acceptance.query.filter_by(contract_id=cid).all()
    has_acceptance = len(acceptances) > 0
    has_unresolved_feedback = Feedback.query.filter_by(
        contract_id=cid,
        is_resolved=False
    ).count() > 0

    # 1) 未启动：没有任务、也没有验收
    if not has_tasks and not has_acceptance:
        return "未启动", "gray"

    # 2) 生产中：有任务，但还没有任何验收记录
    if has_tasks and not has_acceptance:
        return "生产中", "blue"

    # 3) 有验收记录，区分验收中 / 已验收
    #    - 只要存在非“通过”的验收记录，就认为“验收中”
    any_not_passed = any(a.status != "通过" for a in acceptances)

    if any_not_passed:
        # 可以理解为还在验收流程中（可能部分通过、部分不通过）
        return "验收中", "orange"

    # 走到这里说明：所有验收记录的 status == "通过"
    # 再根据是否有未解决反馈，区分两种情况：

    if has_unresolved_feedback:
        # 已验收但是有遗留问题
        return "已验收-有未解决问题", "orange"

    # 默认：所有验收通过，且没有未解决反馈，视为“已验收（完成）”
    return "已验收", "green"



contracts_bp = Blueprint('contracts', __name__, url_prefix='/contracts')

ALLOWED_EXTENSIONS = {'pdf', 'png', 'jpg', 'jpeg', 'doc', 'docx', 'xls', 'xlsx'}


# 不同角色允许上传的文件类型
ROLE_ALLOWED_TYPES = {
    # 你可以根据自己 User.role 的实际值调整这些 key
    'admin': {'contract', 'tech', 'drawing', 'invoice', 'ticket'},
    'boss': {'contract', 'tech', 'drawing', 'invoice', 'ticket'},
    'software_engineer': {'drawing', 'tech'},
    'mechanical_engineer': {'drawing', 'tech'},
    'electrical_engineer': {'drawing', 'tech'},
    'sales': {'contract', 'tech', 'ticket'},
    'finance': {'invoice'},
    'procurement': {'invoice'},
    # 默认角色（找不到时）
    'default': {'contract', 'tech', 'drawing', 'invoice', 'ticket'},
}

notification_service = DummyNotificationService()
# 手工通知的事件类型列表（仅用于界面展示和日志记录）
NOTIFICATION_EVENT_CHOICES = [
    ('CONTRACT_PROGRESS', '项目进度更新'),
    ('CONTRACT_DELAY', '项目延期提醒'),
    ('CONTRACT_ACCEPTANCE', '验收/交付提醒'),
    ('PROCUREMENT_UPDATE', '采购进展通知'),
    ('OTHER', '其他自定义事件'),
]


def allowed_file(filename: str) -> bool:
    if not filename or '.' not in filename:
        return False
    ext = filename.rsplit('.', 1)[1].lower()
    return ext in ALLOWED_EXTENSIONS


def get_role_allowed_types(user: User):
    role = (user.role or '').strip().lower() if user and user.role else ''
    # 简单处理一下常见中文/英文角色映射可以在这里加
    return ROLE_ALLOWED_TYPES.get(role, ROLE_ALLOWED_TYPES['default'])


def sanitize_part(text: str) -> str:
    """用于文件名中某一段的安全处理：去掉空格和特殊字符"""
    if not text:
        return ''
    # 替换空格为下划线，去掉不适合出现在文件名中的字符
    invalid = '\\/:*?"<>|'
    for ch in invalid:
        text = text.replace(ch, '')
    text = text.replace(' ', '_')
    return text


# 文件类型在文件名中的中文展示
FILE_TYPE_NAME_MAP = {
    'contract': '合同',
    'tech': '技术文档',
    'drawing': '图纸',
    'invoice': '其它',  # 现在前端下拉里“其它”用的就是 invoice
}


def generate_file_name(contract: Contract, file_type: str, version: str, author: str, original_filename: str) -> str:
    """按照约定规则生成文件名：
    客户公司_项目编号_合同编号_合同名称_上传日期_文件类型_文件原始名_版本号_作者.扩展名
    """
    # 拆出扩展名
    if '.' in original_filename:
        name_without_ext, ext_raw = original_filename.rsplit('.', 1)
        ext = '.' + ext_raw.lower()
    else:
        name_without_ext = original_filename
        ext = ''

    company_name = sanitize_part(contract.company.name if contract.company else '')
    project_code = sanitize_part(contract.project_code or '')
    contract_number = sanitize_part(contract.contract_number or '')
    contract_name = sanitize_part(contract.name or '')
    today_str = datetime.utcnow().strftime('%Y%m%d')
    file_type_label = FILE_TYPE_NAME_MAP.get(file_type, file_type)
    file_type_part = sanitize_part(file_type_label)
    original_name_part = sanitize_part(name_without_ext or 'NoFilename')
    version_part = sanitize_part(version or 'V1')
    author_part = sanitize_part(author or 'unknown')

    parts = [
        company_name or 'NoCompany',
        project_code or 'NoProject',
        contract_number or 'NoContractNo',
        contract_name or 'NoName',
        today_str,
        file_type_part,
        original_name_part,
        version_part,
        author_part,
    ]
    base = "_".join(parts)
    # 长度太长时可以简单截断
    if len(base) > 180:
        base = base[:180]
    return base + ext



def parse_date(date_str):
    """将 'YYYY-MM-DD' 字符串转成 date 对象，失败返回 None"""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        return None


def login_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        if 'user_id' not in session:
            flash('请先登录')
            return redirect(url_for('auth.login'))
        return view(**kwargs)
    return wrapped_view

# 项目/合同列表

@contracts_bp.route('/')
@login_required
def list_contracts():
    """项目/合同列表"""
    user = None
    user_id = session.get('user_id')
    if user_id:
        user = User.query.get(user_id)

    # 基础查询：后面在上面叠加筛选条件
    query = Contract.query.join(Company)

    # 读取筛选条件（GET 参数）
    company_name = (request.args.get('company') or '').strip()
    project_code = (request.args.get('project_code') or '').strip()
    contract_number = (request.args.get('contract_number') or '').strip()
    name = (request.args.get('name') or '').strip()
    planned_delivery_date_str = (request.args.get('planned_delivery_date') or '').strip()
    status_filter = (request.args.get('status') or '').strip()

    # 状态筛选：支持多选
    raw_status_filters = request.args.getlist('status')
    status_filters = [s.strip() for s in raw_status_filters if s.strip()]


    # 按条件过滤（全部是“包含”匹配）
    if company_name:
        query = query.filter(Company.name.contains(company_name))
    if project_code:
        query = query.filter(Contract.project_code.contains(project_code))
    if contract_number:
        query = query.filter(Contract.contract_number.contains(contract_number))
    if name:
        query = query.filter(Contract.name.contains(name))
    if planned_delivery_date_str:
        planned_delivery_date = parse_date(planned_delivery_date_str)
        if planned_delivery_date:
            query = query.filter(Contract.planned_delivery_date == planned_delivery_date)

    # 按创建时间倒序，最近的项目在前
    contracts = query.order_by(Contract.created_at.desc()).all()

    # 1）构造：每个合同的 “部门 -> [负责人列表]”
    leaders_by_contract = {}
    for c in contracts:
        dept_map = {}
        # 这里用 department_id / person_id 排序，遵守“用 id 控制顺序”的原则
        for l in sorted(c.department_leaders, key=lambda x: ((x.department_id or 0), (x.person_id or 0))):
            if not l.department or not l.person:
                continue
            dept_name = l.department.name
            dept_map.setdefault(dept_name, []).append(l.person)
        leaders_by_contract[c.id] = dept_map

    # 2）为每个合同计算状态（get_contract_status）
    status_map = {}
    for c in contracts:
        status_text, status_level = get_contract_status(c)
        status_map[c.id] = dict(text=status_text, level=status_level)

    # 如果设置了状态筛选，则在内存中按状态文本“多选过滤”
    if status_filters:
        filtered_contracts = []
        for c in contracts:
            st = status_map.get(c.id)
            if not st:
                continue
            if st.get("text") in status_filters:
                filtered_contracts.append(c)
        contracts = filtered_contracts


    # 3）准备“后续任务”数据：每个合同下若干条未完成任务
    contract_ids = [c.id for c in contracts]
    next_tasks_by_contract: dict[int, list[Task]] = {}
    if contract_ids:
        all_tasks = (
            Task.query
            .filter(Task.contract_id.in_(contract_ids))
            .order_by(Task.start_date.asc(), Task.id.asc())
            .all()
        )
        for t in all_tasks:
            # 已完成 / 已暂停的就不算“后续任务”
            if t.status in ("已完成", "已暂停"):
                continue
            next_tasks_by_contract.setdefault(t.contract_id, []).append(t)

    # 4）准备“客户反馈摘要”：总数 / 未解决数 / 最新一条内容
        feedback_summary_by_contract: dict[int, dict] = {}
    if contract_ids:
        feedbacks = (
            Feedback.query
            .filter(Feedback.contract_id.in_(contract_ids))
            .order_by(Feedback.feedback_time.desc(), Feedback.id.desc())
            .all()
        )
        for fb in feedbacks:
            cid = fb.contract_id
            summary = feedback_summary_by_contract.setdefault(
                cid,
                {"total": 0, "unresolved": 0, "records": []},
            )
            summary["total"] += 1
            if not fb.is_resolved:
                summary["unresolved"] += 1
            summary["records"].append(fb)


    return render_template(
        'contracts/list.html',
        user=user,
        contracts=contracts,
        leaders_by_contract=leaders_by_contract,
        statuses=status_map,
        next_tasks_by_contract=next_tasks_by_contract,
        feedback_summary_by_contract=feedback_summary_by_contract,
        # 把当前的筛选条件传回模板，便于回显
        company=company_name,
        project_code=project_code,
        contract_number=contract_number,
        name=name,
        planned_delivery_date=planned_delivery_date_str,
        status_filters=status_filters,
    )



@contracts_bp.route('/<int:contract_id>/status_note', methods=['POST'])
@login_required
def set_status_note(contract_id: int):
    """在项目/合同列表页中，编辑合同的手工状态描述"""
    user_id = session.get('user_id')
    user = User.query.get(user_id) if user_id else None

    contract = Contract.query.get_or_404(contract_id)

    old_note = contract.status_note
    note = (request.form.get('status_note') or '').strip() or None

    contract.status_note = note
    db.session.commit()

    log_operation(
        operator=user,
        contract_id=contract.id,
        object_type=OBJECT_TYPE_CONTRACT,
        object_id=contract.id,
        action=ACTION_UPDATE,
        old_data={"status_note": old_note},
        new_data={"status_note": note},
        request=request,
    )

    flash('当前状态已更新')
    return redirect(url_for('contracts.list_contracts'))


# 新建项目/合同

@contracts_bp.route('/new', methods=['GET', 'POST'])
@login_required
def new_contract():
    """新建项目/合同"""
    user = None
    user_id = session.get('user_id')
    if user_id:
        user = User.query.get(user_id)

    if request.method == 'POST':
        company_name = (request.form.get('company_name') or '').strip()
        project_code = (request.form.get('project_code') or '').strip()
        contract_number = (request.form.get('contract_number') or '').strip()
        name = (request.form.get('name') or '').strip()
        client_manager = (request.form.get('client_manager') or '').strip()
        client_contact = (request.form.get('client_contact') or '').strip()
        our_manager = (request.form.get('our_manager') or '').strip()
        planned_delivery_date_str = (request.form.get('planned_delivery_date') or '').strip()
        planned_delivery_date = parse_date(planned_delivery_date_str)

        if not company_name or not project_code or not contract_number or not name:
            flash('客户公司名称、项目编号、合同编号、合同名称都是必填项')
            return render_template('contracts/new.html', user=user)

        # 查找或创建公司
        company = Company.query.filter_by(name=company_name).first()
        if not company:
            company = Company(name=company_name)
            db.session.add(company)
            db.session.flush()

        # 检查项目编号全局唯一
        exists = Contract.query.filter_by(project_code=project_code).first()
        if exists:
            flash('该项目编号已存在，请更换一个唯一的项目编号')
            return render_template('contracts/new.html', user=user)

        contract = Contract(
            company_id=company.id,
            project_code=project_code,
            contract_number=contract_number,
            name=name,
            client_manager=client_manager,
            client_contact=client_contact,
            our_manager=our_manager,
            planned_delivery_date=planned_delivery_date,
            created_by_id=user_id,
        )

        db.session.add(contract)
        db.session.commit()

        # ★ 新增：记录一条合同创建的操作日志
        log_operation(
            operator=user,
            contract_id=contract.id,
            object_type=OBJECT_TYPE_CONTRACT,
            object_id=contract.id,
            action=ACTION_CREATE,
            new_data={
                "project_code": contract.project_code,
                "contract_number": contract.contract_number,
                "name": contract.name,
                "planned_delivery_date": contract.planned_delivery_date.isoformat()
                    if contract.planned_delivery_date else None,
            },
            request=request,
        )

        flash('项目/合同已创建')
        return redirect(url_for('contracts.list_contracts'))

    return render_template('contracts/new.html', user=user)

# 编辑项目/合同基础信息

@contracts_bp.route('/<int:contract_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_contract(contract_id: int):
    """编辑项目/合同基础信息"""
    user_id = session.get('user_id')
    user = User.query.get(user_id) if user_id else None

    contract = Contract.query.get_or_404(contract_id)
    company = contract.company  # 只读展示客户公司

    if request.method == 'POST':
        # 记录旧值，用于操作日志
        old_data = {
            "project_code": contract.project_code,
            "contract_number": contract.contract_number,
            "name": contract.name,
            "client_manager": contract.client_manager,
            "client_contact": contract.client_contact,
            "our_manager": contract.our_manager,
            "remark": getattr(contract, "remark", None),
        }

        project_code = (request.form.get('project_code') or '').strip()
        contract_number = (request.form.get('contract_number') or '').strip()
        name = (request.form.get('name') or '').strip()
        client_manager = (request.form.get('client_manager') or '').strip()
        client_contact = (request.form.get('client_contact') or '').strip()
        our_manager = (request.form.get('our_manager') or '').strip()
        remark = (request.form.get('remark') or '').strip() or None

        if not project_code or not contract_number or not name:
            flash('项目编号、合同编号、合同名称都是必填项')
            return render_template('contracts/edit.html', user=user, contract=contract, company=company)

        # 如果项目编号修改了，需要检查唯一性
        if project_code != contract.project_code:
            exists = Contract.query.filter_by(project_code=project_code).first()
            if exists and exists.id != contract.id:
                flash('该项目编号已存在，请更换一个唯一的项目编号')
                return render_template('contracts/edit.html', user=user, contract=contract, company=company)

        # 写回新值
        contract.project_code = project_code
        contract.contract_number = contract_number
        contract.name = name
        contract.client_manager = client_manager
        contract.client_contact = client_contact
        contract.our_manager = our_manager
        if hasattr(contract, "remark"):
            contract.remark = remark

        db.session.commit()

        new_data = {
            "project_code": contract.project_code,
            "contract_number": contract.contract_number,
            "name": contract.name,
            "client_manager": contract.client_manager,
            "client_contact": contract.client_contact,
            "our_manager": contract.our_manager,
            "remark": getattr(contract, "remark", None),
        }

        # ✅ 记录操作日志（合同编辑）
        log_operation(
            operator=user,
            contract_id=contract.id,
            object_type=OBJECT_TYPE_CONTRACT,
            object_id=contract.id,
            action=ACTION_UPDATE,
            old_data=old_data,
            new_data=new_data,
            request=request,
        )

        flash('合同信息已更新')
        return redirect(url_for('contracts.list_contracts'))

    # GET：展示编辑表单
    return render_template('contracts/edit.html', user=user, contract=contract, company=company)

# 发送通知

@contracts_bp.route('/<int:contract_id>/notify', methods=['GET', 'POST'])
@login_required
def notify_contract(contract_id: int):
    """针对单个项目/合同发送通知（手动选择事件和接收用户）。"""
    user_id = session.get('user_id')
    user = User.query.get(user_id) if user_id else None

    contract = Contract.query.get_or_404(contract_id)

    # 所有已注册用户，用于在前端下拉框中选择接收人
    users = User.query.order_by(User.username.asc()).all()

    if request.method == 'POST':
        channel = (request.form.get('channel') or 'wechat').strip()
        target = (request.form.get('target') or '').strip()
        template_code = (request.form.get('template_code') or 'CONTRACT_EVENT').strip()
        message = (request.form.get('message') or '').strip()
        event_code = (request.form.get('event_code') or 'OTHER').strip()
        target_user_id_raw = (request.form.get('target_user_id') or '').strip()

        target_user = None
        if target_user_id_raw:
            try:
                target_user_id = int(target_user_id_raw)
            except ValueError:
                target_user_id = None
            if target_user_id:
                target_user = User.query.get(target_user_id)

        # 如果选择了系统内用户，但未手动输入 target，则根据通道自动取对应字段
        if target_user and not target:
            if channel == 'email':
                target = (target_user.email or '').strip()
            elif channel == 'sms':
                target = (target_user.phone or '').strip()
            elif channel == 'wechat':
                target = (target_user.wechat or '').strip()

        if not target:
            flash('请选择接收用户或填写接收人联系方式（邮箱 / 手机 / 微信ID 等）')
            return render_template(
                'contracts/notify.html',
                user=user,
                contract=contract,
                default_channel=channel,
                default_template_code=template_code,
                default_target=target,
                default_message=message,
                notification_event_choices=NOTIFICATION_EVENT_CHOICES,
                default_event_code=event_code,
                users=users,
            )

        # 组织模板参数（后续可以扩展更多字段）
        params = {
            "contract_id": contract.id,
            "project_code": contract.project_code,
            "contract_number": contract.contract_number,
            "contract_name": contract.name,
            "message": message,
            "event_code": event_code,
            "target_user_id": target_user.id if target_user else None,
        }

        # 通过通知服务发送（当前仍为 Dummy 实现，只打印日志）
        notification_service.send(
            channel=channel,
            target=target,
            template_code=template_code,
            params=params,
        )

        # 写入操作日志，方便追踪谁发过什么通知
        log_operation(
            operator=user,
            contract_id=contract.id,
            object_type=OBJECT_TYPE_CONTRACT,
            object_id=contract.id,
            action='notify',
            old_data=None,
            new_data={
                "channel": channel,
                "target": target,
                "template_code": template_code,
                "message": message,
                "event_code": event_code,
                "target_user_id": target_user.id if target_user else None,
            },
            request=request,
        )

        flash('通知已发送（当前为测试模式，仅记录在服务器日志和操作日志中）')
        return redirect(url_for('contracts.list_contracts'))

    # GET：展示发送通知表单
    return render_template(
        'contracts/notify.html',
        user=user,
        contract=contract,
        default_channel='wechat',
        default_template_code='CONTRACT_EVENT',
        default_target='',
        default_message=f'项目 {contract.project_code} - {contract.name} 的进度提醒',
        notification_event_choices=NOTIFICATION_EVENT_CHOICES,
        default_event_code='CONTRACT_PROGRESS',
        users=users,
    )




@contracts_bp.route('/<int:contract_id>/planned_delivery', methods=['POST'])
@login_required
def set_planned_delivery(contract_id: int):
    """在项目/合同列表页中，更新单个合同的计划交付日期"""
    user_id = session.get('user_id')
    user = User.query.get(user_id) if user_id else None

    contract = Contract.query.get_or_404(contract_id)

    # 旧值用于写入操作日志
    old_date = contract.planned_delivery_date

    # 从表单获取日期字符串
    date_str = (request.form.get('planned_delivery_date') or '').strip()
    # 复用你已有的 parse_date 工具函数
    new_date = parse_date(date_str)

    contract.planned_delivery_date = new_date
    db.session.commit()

    # 写一条操作日志
    log_operation(
        operator=user,
        contract_id=contract.id,
        object_type=OBJECT_TYPE_CONTRACT,
        object_id=contract.id,
        action=ACTION_UPDATE,
        old_data={
            "planned_delivery_date": old_date.isoformat() if old_date else None,
        },
        new_data={
            "planned_delivery_date": new_date.isoformat() if new_date else None,
        },
        request=request,
    )

    flash('计划交付日期已更新')
    # 从列表页来的，保存后也回列表页
    return redirect(url_for('contracts.list_contracts'))



@contracts_bp.route('/<int:contract_id>/leaders', methods=['GET', 'POST'])
@login_required
def manage_leaders(contract_id):
    """管理某个项目/合同的部门负责人（可多名）"""
    user_id = session.get('user_id')
    user = User.query.get(user_id) if user_id else None

    contract = Contract.query.get_or_404(contract_id)

    # 处理新增负责人
    if request.method == 'POST':
        department_id_raw = request.form.get('department_id')
        person_id_raw = request.form.get('person_id')

        if not department_id_raw or not person_id_raw:
            flash('请选择部门和负责人')
        else:
            try:
                department_id = int(department_id_raw)
                person_id = int(person_id_raw)
            except ValueError:
                flash('部门或负责人选择无效')
            else:
                # 检查是否已存在同一记录
                exists = ProjectDepartmentLeader.query.filter_by(
                    contract_id=contract.id,
                    department_id=department_id,
                    person_id=person_id
                ).first()
                if exists:
                    flash('该负责人在本项目此部门下已存在')
                else:
                    leader = ProjectDepartmentLeader(
                        contract_id=contract.id,
                        department_id=department_id,
                        person_id=person_id,
                    )
                    db.session.add(leader)
                    db.session.commit()
                    flash('已添加部门负责人')

        return redirect(url_for('contracts.manage_leaders', contract_id=contract.id))

    # GET 请求：展示当前负责人列表 + 添加表单
    # 为了让你可以用 id 控制顺序，我这里按照 Department.id / Person.id 排序
    leaders = (
        ProjectDepartmentLeader.query
        .filter_by(contract_id=contract.id)
        .join(Department, ProjectDepartmentLeader.department_id == Department.id)
        .join(Person, ProjectDepartmentLeader.person_id == Person.id)
        .order_by(Department.id.asc(), Person.id.asc())
        .all()
    )

    departments = Department.query.order_by(Department.id.asc()).all()
    persons = Person.query.order_by(Person.id.asc()).all()

    return render_template(
        'contracts/leaders.html',
        user=user,
        contract=contract,
        leaders=leaders,
        departments=departments,
        persons=persons,
    )


@contracts_bp.route('/<int:contract_id>/leaders/<int:leader_id>/delete', methods=['POST'])
@login_required
def delete_leader(contract_id, leader_id):
    """删除某条部门负责人记录"""
    contract = Contract.query.get_or_404(contract_id)

    leader = ProjectDepartmentLeader.query.filter_by(
        id=leader_id,
        contract_id=contract.id
    ).first_or_404()

    db.session.delete(leader)
    db.session.commit()
    flash('该负责人已移除')

    return redirect(url_for('contracts.manage_leaders', contract_id=contract.id))

@contracts_bp.route('/<int:contract_id>/tasks', methods=['GET', 'POST'])
@login_required
def manage_tasks(contract_id):
    """管理某个项目的任务/生产进度"""
    user_id = session.get('user_id')
    user = User.query.get(user_id) if user_id else None

    contract = Contract.query.get_or_404(contract_id)

    # 使用 ProductionService 封装任务创建与状态流转
    prod_service = ProductionService(db)

    if request.method == 'POST':
        department_id_raw = request.form.get('department_id')
        person_id_raw = request.form.get('person_id')
        title = (request.form.get('title') or '').strip()
        start_date_str = (request.form.get('start_date') or '').strip()
        end_date_str = (request.form.get('end_date') or '').strip()
        remarks = (request.form.get('remarks') or '').strip()

        if not department_id_raw or not title or not start_date_str:
            flash('部门、任务名称、开始日期为必填')
            return redirect(url_for('contracts.manage_tasks', contract_id=contract.id))

        start_date = parse_date(start_date_str)
        end_date = parse_date(end_date_str)

        try:
            department_id = int(department_id_raw)
        except ValueError:
            flash('部门选择无效')
            return redirect(url_for('contracts.manage_tasks', contract_id=contract.id))

        person_id = None
        if person_id_raw:
            try:
                person_id = int(person_id_raw)
            except ValueError:
                person_id = None

        # 使用 ProductionService 创建任务，统一封装业务逻辑
        # 状态不再从表单获取，使用服务默认的“未开始”
        prod_service.create_task(
            contract=contract,
            department_id=department_id,
            title=title,
            start_date=start_date,
            end_date=end_date,
            person_id=person_id,
            remarks=remarks,
        )


        flash('任务已创建')
        return redirect(url_for('contracts.manage_tasks', contract_id=contract.id))


    # GET: 展示任务列表和新增表单
    tasks = (
        Task.query
        .filter_by(contract_id=contract.id)
        .join(Department, Task.department_id == Department.id)
        .order_by(Department.id.asc(), Task.start_date.asc(), Task.id.asc())
        .all()
    )
    departments = Department.query.order_by(Department.id.asc()).all()
    persons = Person.query.order_by(Person.id.asc()).all()

    return render_template(
        'contracts/tasks.html',
        user=user,
        contract=contract,
        tasks=tasks,
        departments=departments,
        persons=persons,
    )


# ----------------------------------------------------------------------
# 任务视图增强：按部门查看所有项目的任务总览
# URL: /contracts/tasks/by_department
# ----------------------------------------------------------------------
@contracts_bp.route('/tasks/by_department')
@login_required
def tasks_by_department():
    """按部门查看全项目任务列表（含简单筛选和统计）"""
    # 当前登录用户（主要用于模板中显示用户名/权限控制）
    user_id = session.get('user_id')
    user = User.query.get(user_id) if user_id else None

    # ---- 1. 解析筛选条件（GET 参数） ----
    status_filter = (request.args.get('status') or '').strip()
    only_today = (request.args.get('only_today') or '').strip() == 'y'

    # ---- 2. 基础数据：部门列表 ----
    departments = Department.query.order_by(Department.name.asc()).all()

    # ---- 3. 按筛选条件获取任务列表 ----
    query = Task.query.order_by(Task.start_date.asc(), Task.id.asc())
    if status_filter:
        query = query.filter(Task.status == status_filter)

    all_tasks = query.all()

    today = date.today()

    # 根据“只看今天”筛选出要展示的任务
    display_tasks = []
    for t in all_tasks:
        if only_today:
            # 这里简单按“开始日期 == 今天”来定义“今天的任务”
            if not (t.start_date == today):
                continue
        display_tasks.append(t)

    # ---- 4. 统计卡片数据（基于展示任务） ----
    total_count = len(display_tasks)
    today_count = sum(1 for t in display_tasks if t.start_date == today)
    todo_count = sum(
        1
        for t in display_tasks
        if t.status in ("未开始", "进行中", "待质检")
    )
    done_count = sum(1 for t in display_tasks if t.status == "已完成")

    stats = {
        "total": total_count,
        "today": today_count,
        "todo": todo_count,
        "done": done_count,
    }

    # ---- 5. 按部门分组任务：dept_id -> [Task, Task, ...] ----
    tasks_by_dept = {}
    for t in display_tasks:
        dept_id = t.department_id
        tasks_by_dept.setdefault(dept_id, []).append(t)

    # 用于下拉框的状态选项
    status_choices = ["", "未开始", "进行中", "待质检", "已完成", "已暂停"]

    return render_template(
        'contracts/tasks_by_department.html',
        user=user,
        departments=departments,
        tasks_by_dept=tasks_by_dept,
        stats=stats,
        status_choices=status_choices,
        current_status=status_filter,
        only_today=only_today,
        today=today,
    )



# ----------------------------------------------------------------------
# 任务视图增强：按人员查看所有项目的任务总览
# URL: /contracts/tasks/by_person
# ----------------------------------------------------------------------
@contracts_bp.route('/tasks/by_person')
@login_required
def tasks_by_person():
    """按人员查看全项目任务列表（含简单筛选和统计）"""
    user_id = session.get('user_id')
    user = User.query.get(user_id) if user_id else None

    # ---- 1. 解析筛选条件 ----
    status_filter = (request.args.get('status') or '').strip()
    only_today = (request.args.get('only_today') or '').strip() == 'y'

    # 所有人（未来可以按角色/部门筛选，这里先简单全部）
    persons = Person.query.order_by(Person.name.asc()).all()

    # ---- 2. 获取任务并按筛选条件过滤 ----
    query = Task.query.order_by(Task.start_date.asc(), Task.id.asc())
    if status_filter:
        query = query.filter(Task.status == status_filter)

    all_tasks = query.all()
    today = date.today()

    display_tasks = []
    for t in all_tasks:
        if only_today:
            if not (t.start_date == today):
                continue
        display_tasks.append(t)

    # ---- 3. 统计卡片数据 ----
    total_count = len(display_tasks)
    today_count = sum(1 for t in display_tasks if t.start_date == today)
    todo_count = sum(
        1
        for t in display_tasks
        if t.status in ("未开始", "进行中", "待质检")
    )
    done_count = sum(1 for t in display_tasks if t.status == "已完成")

    stats = {
        "total": total_count,
        "today": today_count,
        "todo": todo_count,
        "done": done_count,
    }

    # ---- 4. 按人员分组任务：person_id -> [Task, Task, ...] ----
    tasks_by_person = {}
    for t in display_tasks:
        pid = t.person_id  # 允许为 None，模板里单独显示“未指派”
        tasks_by_person.setdefault(pid, []).append(t)

    status_choices = ["", "未开始", "进行中", "待质检", "已完成", "已暂停"]

    return render_template(
        'contracts/tasks_by_person.html',
        user=user,
        persons=persons,
        tasks_by_person=tasks_by_person,
        stats=stats,
        status_choices=status_choices,
        current_status=status_filter,
        only_today=only_today,
        today=today,
    )



@contracts_bp.route('/<int:contract_id>/tasks/<int:task_id>/delete', methods=['POST'])
@login_required
def delete_task(contract_id, task_id):
    contract = Contract.query.get_or_404(contract_id)
    task = Task.query.filter_by(id=task_id, contract_id=contract.id).first_or_404()
    db.session.delete(task)
    db.session.commit()
    flash('任务已删除')
    return redirect(url_for('contracts.manage_tasks', contract_id=contract.id))

# 任务状态变更

@contracts_bp.route('/<int:contract_id>/tasks/<int:task_id>/status', methods=['POST'])
@login_required
def change_task_status(contract_id, task_id):
    """变更单个任务的状态（开始 / 待质检 / 完成 / 暂停）。"""
    user_id = session.get('user_id')
    user = User.query.get(user_id) if user_id else None

    contract = Contract.query.get_or_404(contract_id)
    task = Task.query.filter_by(id=task_id, contract_id=contract.id).first_or_404()

    action = (request.form.get('action') or '').strip()
    service = ProductionService(db)

    old_status = task.status
    msg = None

    if action == 'start':
        service.start_task(task)
        msg = '任务已开始'
    elif action == 'wait_qc':
        service.mark_waiting_qc(task)
        msg = '任务已标记为待质检'
    elif action == 'complete':
        service.complete_task(task)
        msg = '任务已完成'
    elif action == 'pause':
        service.pause_task(task)
        msg = '任务已暂停'
    else:
        flash('无效的任务状态操作', 'error')
        return redirect(url_for('contracts.manage_tasks', contract_id=contract.id))

    # 统一写一条状态变更日志
    log_operation(
        operator=user,
        contract_id=contract.id,  # 建议这里也带上合同维度
        object_type=OBJECT_TYPE_TASK,
        object_id=task.id,
        action=ACTION_STATUS_CHANGE,
        old_data={"status": old_status},
        new_data={"status": task.status},
        request=request,
    )

    if msg:
        flash(msg)

    return redirect(url_for('contracts.manage_tasks', contract_id=contract.id))



# 采购

@contracts_bp.route('/<int:contract_id>/procurements', methods=['GET', 'POST'])
@login_required
def manage_procurements(contract_id):
    """管理某个项目的采购清单"""
    user_id = session.get('user_id')
    user = User.query.get(user_id) if user_id else None

    contract = Contract.query.get_or_404(contract_id)

    # 使用业务 service 封装采购逻辑（含未来通知）
    service = ProcurementService(db)

    if request.method == 'POST':
        item_name = (request.form.get('item_name') or '').strip()
        quantity_raw = (request.form.get('quantity') or '').strip()
        unit = (request.form.get('unit') or '').strip()
        expected_date_str = (request.form.get('expected_date') or '').strip()
        status = (request.form.get('status') or '').strip() or '未采购'
        remarks = (request.form.get('remarks') or '').strip()

        if not item_name:
            flash('物料名称为必填')
            return redirect(url_for('contracts.manage_procurements', contract_id=contract.id))

        try:
            quantity = int(quantity_raw) if quantity_raw else 0
        except ValueError:
            quantity = 0

        expected_date = parse_date(expected_date_str)

        # 组装业务数据字典，交由 ProcurementService 处理
        data = {
            "item_name": item_name,
            "quantity": quantity,
            "unit": unit,
            "expected_date": expected_date,
            "remarks": remarks,
            "status": status,  # 注意：当前 service 中未直接使用 status，如需持久化可后续同步调整
        }

        # 预留通知目标：
        # 这里先用当前登录用户邮箱作为示例，将来可以改为项目负责人 / 采购专员等
        # 通知策略：采购模块不再自动发送通知，统一通过“发送通知”页面手工触发
        notify_target = None

        item = service.create_item(
            contract=contract,
            data=data,
            notify_target=notify_target,
            notify_channel="email",
        )


        # 写一条“创建采购项”的日志
        if item is not None:
            log_operation(
                operator=user,
                contract_id=contract.id,
                object_type=OBJECT_TYPE_PROCUREMENT,
                object_id=item.id,
                action=ACTION_CREATE,
                new_data={
                    "item_name": item.item_name,
                    "quantity": item.quantity,
                    "unit": item.unit,
                    "expected_date": item.expected_date.isoformat() if item.expected_date else None,
                    "status": item.status,
                },
                request=request,
            )

        flash('采购项已添加')
        return redirect(url_for('contracts.manage_procurements', contract_id=contract.id))


    items = ProcurementItem.query.filter_by(contract_id=contract.id).order_by(
        ProcurementItem.id.asc()
    ).all()

    return render_template(
        'contracts/procurements.html',
        user=user,
        contract=contract,
        items=items,
    )


@contracts_bp.route('/<int:contract_id>/procurements/<int:item_id>/delete', methods=['POST'])
@login_required
def delete_procurement(contract_id, item_id):
    user_id = session.get('user_id')
    user = User.query.get(user_id) if user_id else None

    contract = Contract.query.get_or_404(contract_id)
    item = ProcurementItem.query.filter_by(id=item_id, contract_id=contract.id).first_or_404()

    # 先记录一份被删除前的数据快照
    old_data = {
        "item_name": item.item_name,
        "quantity": item.quantity,
        "unit": item.unit,
        "expected_date": item.expected_date.isoformat() if item.expected_date else None,
        "status": item.status,
    }

    db.session.delete(item)
    db.session.commit()

    # 删除之后写一条日志
    log_operation(
        operator=user,
        contract_id=contract.id,
        object_type=OBJECT_TYPE_PROCUREMENT,
        object_id=item.id,
        action=ACTION_DELETE,
        old_data=old_data,
        request=request,
    )

    flash('采购项已删除')
    return redirect(url_for('contracts.manage_procurements', contract_id=contract.id))


# 验收
@contracts_bp.route('/<int:contract_id>/acceptances', methods=['GET', 'POST'])
@login_required
def manage_acceptances(contract_id):
    """管理某个项目的验收记录"""
    user_id = session.get('user_id')
    user = User.query.get(user_id) if user_id else None

    contract = Contract.query.get_or_404(contract_id)

    if request.method == 'POST':
        stage_name = (request.form.get('stage_name') or '').strip()
        person_id_raw = (request.form.get('person_id') or '').strip()
        date_str = (request.form.get('date') or '').strip()
        status = (request.form.get('status') or '').strip() or '进行中'
        remarks = (request.form.get('remarks') or '').strip()

        if not stage_name or not date_str:
            flash('阶段名称和日期为必填')
            return redirect(url_for('contracts.manage_acceptances', contract_id=contract.id))

        d = parse_date(date_str)
        if not d:
            flash('日期格式错误')
            return redirect(url_for('contracts.manage_acceptances', contract_id=contract.id))

        person_id = None
        if person_id_raw:
            try:
                person_id = int(person_id_raw)
            except ValueError:
                person_id = None

        # 如果备注为空，且存在最近一个“已完成”的任务，则自动在备注中关联该任务
        if not remarks:
            last_task = (
                Task.query
                .filter_by(contract_id=contract.id, status="已完成")
                # SQL Server 不支持 NULLS LAST，这里简单按完成日期倒序、ID 倒序
                .order_by(Task.end_date.desc(), Task.id.desc())
                .first()
            )
            if last_task:
                remarks = f"关联任务：{last_task.title}"


        acc = Acceptance(
            contract_id=contract.id,
            stage_name=stage_name,
            person_id=person_id,
            date=d,
            status=status,
            remarks=remarks,
        )
        db.session.add(acc)
        db.session.commit()

        # 操作日志：创建验收记录
        log_operation(
            operator=user,
            contract_id=contract.id,
            object_type=OBJECT_TYPE_ACCEPTANCE,
            object_id=acc.id,
            action=ACTION_CREATE,
            new_data={
                "stage_name": acc.stage_name,
                "person_id": acc.person_id,
                "date": acc.date.isoformat() if acc.date else None,
                "status": acc.status,
                "remarks": acc.remarks,
            },
            request=request,
        )

        flash('验收记录已添加')
        return redirect(url_for('contracts.manage_acceptances', contract_id=contract.id))


    records = (
        Acceptance.query.filter_by(contract_id=contract.id)
        .order_by(Acceptance.date.asc(), Acceptance.id.asc())
        .all()
    )
    persons = Person.query.order_by(Person.id.asc()).all()

    return render_template(
        'contracts/acceptances.html',
        user=user,
        contract=contract,
        records=records,
        persons=persons,
    )


@contracts_bp.route('/<int:contract_id>/acceptances/<int:acc_id>/delete', methods=['POST'])
@login_required
def delete_acceptance(contract_id, acc_id):
    user_id = session.get('user_id')
    user = User.query.get(user_id) if user_id else None

    contract = Contract.query.get_or_404(contract_id)
    acc = Acceptance.query.filter_by(id=acc_id, contract_id=contract.id).first_or_404()

    # 先留一份快照
    old_data = {
        "stage_name": acc.stage_name,
        "person_id": acc.person_id,
        "date": acc.date.isoformat() if acc.date else None,
        "status": acc.status,
        "remarks": acc.remarks,
    }

    db.session.delete(acc)
    db.session.commit()

    # 操作日志：删除验收记录
    log_operation(
        operator=user,
        contract_id=contract.id,
        object_type=OBJECT_TYPE_ACCEPTANCE,
        object_id=acc.id,
        action=ACTION_DELETE,
        old_data=old_data,
        request=request,
    )

    flash('验收记录已删除')
    return redirect(url_for('contracts.manage_acceptances', contract_id=contract.id))


# 销售管理

@contracts_bp.route('/<int:contract_id>/sales', methods=['GET', 'POST'])
@login_required
def manage_sales(contract_id):
    """管理某个项目的销售信息（报价、成交日期、销售负责人）"""
    user_id = session.get('user_id')
    user = User.query.get(user_id) if user_id else None

    contract = Contract.query.get_or_404(contract_id)

    # 查询当前已有的销售记录（0 或 1 条）
    sales = SalesInfo.query.filter_by(contract_id=contract.id).first()

    if request.method == 'POST':
        quote_amount_raw = (request.form.get('quote_amount') or '').strip()
        quote_date_str = (request.form.get('quote_date') or '').strip()
        deal_date_str = (request.form.get('deal_date') or '').strip()
        sales_person_id_raw = (request.form.get('sales_person_id') or '').strip()
        remarks = (request.form.get('remarks') or '').strip()

        # 金额可以为空，为空代表尚未确定
        quote_amount = None
        if quote_amount_raw:
            try:
                quote_amount = float(quote_amount_raw)
            except ValueError:
                flash('报价金额格式错误')
                return redirect(url_for('contracts.manage_sales', contract_id=contract.id))

        quote_date = parse_date(quote_date_str) if quote_date_str else None
        if quote_date_str and not quote_date:
            flash('报价日期格式错误')
            return redirect(url_for('contracts.manage_sales', contract_id=contract.id))

        deal_date = parse_date(deal_date_str) if deal_date_str else None
        if deal_date_str and not deal_date:
            flash('成交日期格式错误')
            return redirect(url_for('contracts.manage_sales', contract_id=contract.id))

        sales_person_id = None
        if sales_person_id_raw:
            try:
                sales_person_id = int(sales_person_id_raw)
            except ValueError:
                sales_person_id = None

        if sales:
            # 更新
            sales.quote_amount = quote_amount
            sales.quote_date = quote_date
            sales.deal_date = deal_date
            sales.sales_person_id = sales_person_id
            sales.remarks = remarks or None
            flash('销售信息已更新')
        else:
            # 创建
            sales = SalesInfo(
                contract_id=contract.id,
                quote_amount=quote_amount,
                quote_date=quote_date,
                deal_date=deal_date,
                sales_person_id=sales_person_id,
                remarks=remarks or None,
            )
            db.session.add(sales)
            flash('销售信息已创建')

        db.session.commit()
        return redirect(url_for('contracts.manage_sales', contract_id=contract.id))

    # GET：展示现有销售信息 + 编辑表单
    persons = Person.query.order_by(Person.id.asc()).all()

    return render_template(
        'contracts/sales.html',
        user=user,
        contract=contract,
        sales=sales,
        persons=persons,
    )


@contracts_bp.route('/<int:contract_id>/sales/delete', methods=['POST'])
@login_required
def delete_sales(contract_id):
    """删除某项目的销售信息记录"""
    contract = Contract.query.get_or_404(contract_id)
    sales = SalesInfo.query.filter_by(contract_id=contract.id).first()
    if not sales:
        flash('当前项目没有销售信息可删除')
        return redirect(url_for('contracts.manage_sales', contract_id=contract.id))

    db.session.delete(sales)
    db.session.commit()
    flash('销售信息已删除')
    return redirect(url_for('contracts.manage_sales', contract_id=contract.id))

# 项目总览
@contracts_bp.route('/<int:contract_id>/overview')
@login_required
def contract_overview(contract_id):
    """项目 / 合同总览页面"""
    user_id = session.get('user_id')
    user = User.query.get(user_id) if user_id else None

    contract = Contract.query.get_or_404(contract_id)

    # 部门负责人列表
    leaders = (
        ProjectDepartmentLeader.query
        .filter_by(contract_id=contract.id)
        .order_by(ProjectDepartmentLeader.id.asc())
        .all()
    )

    # 销售信息（可能没有）
    sales = SalesInfo.query.filter_by(contract_id=contract.id).first()

    # === 使用新的生产视角状态计算函数 ===
    status_text, status_level = get_contract_status(contract)

    # 验收与反馈统计（使用 service 封装）
    acc_service = AcceptanceService(db)
    fb_service = FeedbackService(db)

    acc_summary = acc_service.get_summary_for_contract(contract)
    fb_summary = fb_service.get_summary_for_contract(contract)

    # 各模块计数（暂时保留财务相关计数，后续可以逐步去掉对应视图）
    tasks_count = Task.query.filter_by(contract_id=contract.id).count()
    proc_count = ProcurementItem.query.filter_by(contract_id=contract.id).count()
    acc_count = Acceptance.query.filter_by(contract_id=contract.id).count()
    fb_count = Feedback.query.filter_by(contract_id=contract.id).count()
    files_count = ProjectFile.query.filter_by(contract_id=contract.id, is_deleted=False).count()

    return render_template(
        'contracts/overview.html',
        user=user,
        contract=contract,
        leaders=leaders,
        sales=sales,
        stats=dict(
            tasks=tasks_count,
            proc=proc_count,
            acc=acc_count,
            fb=fb_count,
            files=files_count,
        ),
        status_text=status_text,
        status_level=status_level,
        acc_summary=acc_summary,
        fb_summary=fb_summary,
    )


# 客户反馈
@contracts_bp.route('/<int:contract_id>/feedbacks', methods=['GET', 'POST'])
@login_required
def manage_feedbacks(contract_id):
    """管理某个项目的客户反馈及处理情况"""
    user_id = session.get('user_id')
    user = User.query.get(user_id) if user_id else None

    contract = Contract.query.get_or_404(contract_id)

    if request.method == 'POST':
        content = (request.form.get('content') or '').strip()
        handler_id_raw = (request.form.get('handler_id') or '').strip()
        result = (request.form.get('result') or '').strip()
        completion_date_str = (request.form.get('completion_date') or '').strip()

        if not content:
            flash('反馈内容为必填')
            return redirect(url_for('contracts.manage_feedbacks', contract_id=contract.id))

        handler_id = None
        if handler_id_raw:
            try:
                handler_id = int(handler_id_raw)
            except ValueError:
                handler_id = None

        completion_time = None
        if completion_date_str:
            d = parse_date(completion_date_str)
            if d:
                completion_time = datetime.combine(d, datetime.min.time())

        fb = Feedback(
            contract_id=contract.id,
            content=content,
            handler_id=handler_id,
            result=result or None,
            completion_time=completion_time,
        )
        db.session.add(fb)
        db.session.commit()

        # 写一条“创建反馈”的日志
        log_operation(
            operator=user,
            contract_id=contract.id,
            object_type=OBJECT_TYPE_FEEDBACK,
            object_id=fb.id,
            action=ACTION_CREATE,
            new_data={
                "content": fb.content,
                "handler_id": fb.handler_id,
                "result": fb.result,
                "completion_time": fb.completion_time.isoformat() if fb.completion_time else None,
            },
            request=request,
        )

        flash('反馈记录已添加')
        return redirect(url_for('contracts.manage_feedbacks', contract_id=contract.id))


    records = Feedback.query.filter_by(contract_id=contract.id).order_by(
        Feedback.feedback_time.asc(), Feedback.id.asc()
    ).all()
    persons = Person.query.order_by(Person.id.asc()).all()

    return render_template(
        'contracts/feedbacks.html',
        user=user,
        contract=contract,
        records=records,
        persons=persons,
       # feedbacks=feedbacks,
    )


@contracts_bp.route('/<int:contract_id>/feedbacks/<int:feedback_id>/delete', methods=['POST'])
@login_required
def delete_feedback(contract_id, feedback_id):
    user_id = session.get('user_id')
    user = User.query.get(user_id) if user_id else None

    contract = Contract.query.get_or_404(contract_id)
    fb = Feedback.query.filter_by(id=feedback_id, contract_id=contract.id).first_or_404()

    old_data = {
        "content": fb.content,
        "handler_id": fb.handler_id,
        "result": fb.result,
        "completion_time": fb.completion_time.isoformat() if fb.completion_time else None,
        "is_resolved": fb.is_resolved,
    }

    db.session.delete(fb)
    db.session.commit()

    log_operation(
        operator=user,
        contract_id=contract.id,
        object_type=OBJECT_TYPE_FEEDBACK,
        object_id=fb.id,
        action=ACTION_DELETE,
        old_data=old_data,
        request=request,
    )

    flash('反馈记录已删除')
    return redirect(url_for('contracts.manage_feedbacks', contract_id=contract.id))


# ----------------------------------------------------------------------
# 全局售后问题看板：未解决反馈总览 + 筛选 + CSV 导出
# URL: /contracts/feedbacks/overview
# ----------------------------------------------------------------------
@contracts_bp.route('/feedbacks/overview')
@login_required
@staff_required
def feedbacks_overview():
    """未解决客户反馈总览（按公司/项目编号/负责人筛选，支持导出 CSV）"""
    user_id = session.get('user_id')
    user = User.query.get(user_id) if user_id else None

    # 基础查询：只看未解决的反馈
    query = (
        Feedback.query
        .join(Contract, Feedback.contract_id == Contract.id)
        .join(Company, Contract.company_id == Company.id)
        .outerjoin(Person, Feedback.handler_id == Person.id)
        .filter(Feedback.is_resolved == False)
    )

    # ---- 筛选条件 ----
    company_filter = (request.args.get('company') or '').strip()
    project_code_filter = (request.args.get('project_code') or '').strip()
    handler_filter = (request.args.get('handler_id') or '').strip()

    if company_filter:
        # 模糊匹配公司名称
        query = query.filter(Company.name.contains(company_filter))

    if project_code_filter:
        # 模糊匹配项目编号
        query = query.filter(Contract.project_code.contains(project_code_filter))

    if handler_filter:
        try:
            handler_id = int(handler_filter)
            query = query.filter(Feedback.handler_id == handler_id)
        except ValueError:
            # 非法 id 直接忽略这个条件
            handler_filter = ""

    # 按反馈时间倒序
    feedbacks = (
        query
        .order_by(Feedback.feedback_time.desc(), Feedback.id.desc())
        .all()
    )

    # 下拉列表数据
    companies = Company.query.order_by(Company.name.asc()).all()
    persons = Person.query.order_by(Person.name.asc()).all()


    # 普通页面渲染
    return render_template(
        'contracts/feedbacks_overview.html',
        user=user,
        feedbacks=feedbacks,
        companies=companies,
        persons=persons,
        company_filter=company_filter,
        project_code_filter=project_code_filter,
        handler_filter=handler_filter,
    )


# 标记反馈为已解决 / 未解决

@contracts_bp.route('/<int:contract_id>/feedbacks/<int:feedback_id>/resolve', methods=['POST'])
@login_required
def resolve_feedback(contract_id, feedback_id):
    """标记反馈为已解决"""
    user_id = session.get('user_id')
    user = User.query.get(user_id) if user_id else None

    contract = Contract.query.get_or_404(contract_id)
    fb = Feedback.query.filter_by(id=feedback_id, contract_id=contract.id).first_or_404()

    old_data = {
        "is_resolved": fb.is_resolved,
        "completion_time": fb.completion_time.isoformat() if fb.completion_time else None,
    }

    fb.is_resolved = True
    fb.completion_time = datetime.utcnow()
    db.session.commit()

    log_operation(
        operator=user,
        contract_id=contract.id,
        object_type=OBJECT_TYPE_FEEDBACK,
        object_id=fb.id,
        action=ACTION_RESOLVE,
        old_data=old_data,
        new_data={
            "is_resolved": fb.is_resolved,
            "completion_time": fb.completion_time.isoformat(),
        },
        request=request,
    )

    flash('该反馈已标记为“已解决”。')
    return redirect(url_for('contracts.manage_feedbacks', contract_id=contract.id))



@contracts_bp.route('/<int:contract_id>/feedbacks/<int:feedback_id>/unresolve', methods=['POST'])
@login_required
def unresolve_feedback(contract_id, feedback_id):
    """标记反馈为未解决"""
    user_id = session.get('user_id')
    user = User.query.get(user_id) if user_id else None

    contract = Contract.query.get_or_404(contract_id)
    fb = Feedback.query.filter_by(id=feedback_id, contract_id=contract.id).first_or_404()

    fb.is_resolved = False
    fb.completion_time = None
    db.session.commit()

    flash('该反馈已标记为“未解决”。')
    return redirect(url_for('contracts.manage_feedbacks', contract_id=contract.id))



# 管理页面（列表+上传）

@contracts_bp.route('/<int:contract_id>/files', methods=['GET', 'POST'])
@login_required
def manage_files(contract_id):
    """管理某个项目的文件：上传 / 列表 / 删除"""
    user_id = session.get('user_id')
    user = User.query.get(user_id) if user_id else None

    contract = Contract.query.get_or_404(contract_id)

    # 只显示未删除的文件
    files = (
        ProjectFile.query
        .filter_by(contract_id=contract.id, is_deleted=False)
        .order_by(ProjectFile.created_at.asc(), ProjectFile.id.asc())
        .all()
    )

    if request.method == 'POST':
        if not user:
            flash('请先登录')
            return redirect(url_for('auth.login'))

        uploaded_file = request.files.get('file')
        file_type = (request.form.get('file_type') or '').strip()
        version = (request.form.get('version') or '').strip() or 'V1'
        is_public_raw = request.form.get('is_public')

        if not uploaded_file or uploaded_file.filename == '':
            flash('请选择要上传的文件')
            return redirect(url_for('contracts.manage_files', contract_id=contract.id))

        # 对图纸 file_type='drawing' 放宽限制，不检查扩展名
        if file_type != 'drawing' and not allowed_file(uploaded_file.filename):
            flash('不支持的文件类型（非图纸文件请使用常见文档/图片格式）')
            return redirect(url_for('contracts.manage_files', contract_id=contract.id))

        # 校验角色是否允许上传这种类型
        allowed_types = get_role_allowed_types(user)
        if file_type not in allowed_types:
            flash('当前角色不允许上传此类型文件')
            return redirect(url_for('contracts.manage_files', contract_id=contract.id))

        # 文件是否公开：只允许合同/技术文档可公开
        is_public = False
        if is_public_raw == 'y' and file_type in ('contract', 'tech'):
            is_public = True

        original_filename = uploaded_file.filename
        author = user.username  # 如果你实际字段叫 name，就改成 user.name
        stored_filename = generate_file_name(
            contract, file_type, version, author, original_filename
        )

        upload_folder = current_app.config['UPLOAD_FOLDER']
        os.makedirs(upload_folder, exist_ok=True)
        filepath = os.path.join(upload_folder, stored_filename)

        uploaded_file.save(filepath)

        file_size = os.path.getsize(filepath)
        content_type = uploaded_file.mimetype

        pf = ProjectFile(
            contract_id=contract.id,
            uploader_id=user.id,
            file_type=file_type,
            version=version,
            author=author,
            original_filename=original_filename,
            stored_filename=stored_filename,
            content_type=content_type,
            file_size=file_size,
            is_public=is_public,
            owner_role=user.role,
        )

        db.session.add(pf)
        db.session.commit()

        # 操作日志：上传文件
        log_operation(
            operator=user,
            contract_id=contract.id,
            object_type=OBJECT_TYPE_FILE,
            object_id=pf.id,
            action=ACTION_UPLOAD,
            new_data={
                "file_type": pf.file_type,
                "version": pf.version,
                "original_filename": pf.original_filename,
                "stored_filename": pf.stored_filename,
                "is_public": pf.is_public,
            },
            request=request,
        )

        flash('文件上传成功')
        return redirect(url_for('contracts.manage_files', contract_id=contract.id))

    # GET：展示列表 & 上传表单
    return render_template(
        'contracts/files.html',
        user=user,
        contract=contract,
        files=files,
    )


# 下载文件（权限检查）

@contracts_bp.route('/<int:contract_id>/files/<int:file_id>/download')
@login_required
def download_file(contract_id, file_id):
    user_id = session.get('user_id')
    user = User.query.get(user_id) if user_id else None

    contract = Contract.query.get_or_404(contract_id)
    pf = ProjectFile.query.filter_by(
        id=file_id,
        contract_id=contract.id,
        is_deleted=False
    ).first_or_404()

    # 权限：简单版
    # - 管理员 / 老板 / 软件工程师：可以下载所有
    # - 其它员工：只能下载 owner_role == 自己 role 的文件
    # - 客户角色：只能下载 is_public=True 且 file_type in ('contract', 'tech')
    role = (user.role or '').strip().lower() if user and user.role else ''

    if role in ('admin', 'boss', 'software_engineer'):
        pass  # 全部允许
    elif role == 'customer':
        if not (pf.is_public and pf.file_type in ('contract', 'tech')):
            flash('你没有权限下载此文件')
            return redirect(url_for('contracts.manage_files', contract_id=contract.id))
    else:
        # 内部普通员工
        if pf.owner_role and pf.owner_role != user.role:
            flash('你只能下载自己部门上传的文件')
            return redirect(url_for('contracts.manage_files', contract_id=contract.id))

    upload_folder = current_app.config['UPLOAD_FOLDER']
    return send_from_directory(
        upload_folder,
        pf.stored_filename,
        as_attachment=True,
        download_name=pf.stored_filename #  pf.original_filename 用原始文件名下载
    )


# 删除文件（软删除+风险提示）

@contracts_bp.route('/<int:contract_id>/files/<int:file_id>/delete', methods=['POST'])
@login_required
def delete_file(contract_id, file_id):
    user_id = session.get('user_id')
    user = User.query.get(user_id) if user_id else None

    contract = Contract.query.get_or_404(contract_id)
    pf = ProjectFile.query.filter_by(
        id=file_id,
        contract_id=contract.id,
        is_deleted=False
    ).first_or_404()

    # 权限控制：上传者 / 管理员 / 老板 可以删
    role = (user.role or '').strip().lower() if user and user.role else ''
    if not user or (user.id != pf.uploader_id and role not in ('admin', 'boss')):
        flash('你没有权限删除此文件')
        return redirect(url_for('contracts.manage_files', contract_id=contract.id))

    old_data = {
        "file_type": pf.file_type,
        "version": pf.version,
        "original_filename": pf.original_filename,
        "stored_filename": pf.stored_filename,
        "is_public": pf.is_public,
        "is_deleted": pf.is_deleted,
    }

    pf.is_deleted = True
    db.session.commit()

    # 操作日志：删除文件（标记为删除）
    log_operation(
        operator=user,
        contract_id=contract.id,
        object_type=OBJECT_TYPE_FILE,
        object_id=pf.id,
        action=ACTION_DELETE,
        old_data=old_data,
        new_data={"is_deleted": True},
        request=request,
    )

    flash('文件已标记为删除（普通用户将无法再访问）')
    return redirect(url_for('contracts.manage_files', contract_id=contract.id))
