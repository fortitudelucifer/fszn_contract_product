from __future__ import annotations
import os
import re
from typing import List, Optional, Iterable
from datetime import datetime
from werkzeug.utils import secure_filename

from flask import current_app
from werkzeug.datastructures import FileStorage

from .. import db
from ..models import ProjectFile, Contract, User
from ..operation_log import (
    log_operation,
    OBJECT_TYPE_FILE,
    ACTION_UPLOAD,
    ACTION_DELETE,
    ACTION_RESTORE,
    ACTION_UPDATE,
    ACTION_DOWNLOAD,
)

# -----------------------------------------
# 常量与工具函数（模块级，避免循环引用问题）
# -----------------------------------------

ALLOWED_EXTENSIONS = {"pdf", "png", "jpg", "jpeg", "doc", "docx", "xls", "xlsx","ppt", "pptx", "mrc2"}
DRAWING_EXTENSIONS = {"jpg", "jpeg", "png", "pdf", "dwg", "dxf", "sldprt", "sldasm", "doc", "docx", "xls", "xlsx","ppt", "pptx", "mrc2"}

ROLE_ALLOWED_TYPES = {
    # 你可以根据自己 User.role 的实际值调整这些 key
    "admin": {"contract", "tech", "drawing", "invoice", "ticket"},
    "boss": {"contract", "tech", "drawing", "invoice", "ticket"},
    "software_engineer": {"contract","drawing", "tech", "invoice"},
    "mechanical_engineer": {"contract","drawing", "tech", "invoice"},
    "electrical_engineer": {"contract","drawing", "tech", "invoice"},
    "sales": {"contract", "tech", "ticket", "invoice"},
    "procurement": {"contract","drawing", "tech", "invoice"},
    # 默认角色（找不到时）
    "default": {"contract","drawing", "tech", "invoice"},
}

FILE_TYPE_NAME_MAP = {
    "contract": "合同",
    "tech": "技术文档",
    "drawing": "图纸",
    "invoice": "其它",  # 现在前端下拉里“其它”用的就是 invoice
}


def allowed_file(filename: str, file_type: str | None = None) -> bool:
    """
    校验文件扩展名是否合法

    - file_type 为 None：使用通用 ALLOWED_EXTENSIONS
    - file_type == 'drawing'：允许工程图扩展名（DRAWING_EXTENSIONS）
    - 其他 file_type：仍走通用 ALLOWED_EXTENSIONS
    """

    if not filename or "." not in filename:
        return False

    ext = filename.rsplit(".", 1)[-1].lower()

    # 图纸类文件，扩展名更宽松
    if file_type == "drawing":
        return ext in DRAWING_EXTENSIONS

    # 默认：通用文件类型
    return ext in ALLOWED_EXTENSIONS



def get_role_allowed_types(user: Optional[User]):
    """根据用户角色返回允许上传的文件类型集合"""
    role = (user.role or "").strip().lower() if user and user.role else ""
    return ROLE_ALLOWED_TYPES.get(role, ROLE_ALLOWED_TYPES["default"])


def sanitize_part(text: str) -> str:
    """用于文件名中某一段的安全处理：去掉空格和特殊字符"""
    if not text:
        return ""
    invalid = '\\/:*?"<>|'
    for ch in invalid:
        text = text.replace(ch, "")
    text = text.replace(" ", "_")
    return text


def generate_file_name(
    contract: Contract,
    file_type: str,
    version: str,
    author: str,
    original_filename: str,
) -> str:
    """
    按照约定规则生成文件名：
    客户公司_项目编号_合同编号_合同名称_上传日期_文件类型_文件原始名_版本号_作者.扩展名
    """
    if "." in original_filename:
        name_without_ext, ext_raw = original_filename.rsplit(".", 1)
        ext = "." + ext_raw.lower()
    else:
        name_without_ext = original_filename
        ext = ""

    company_name = sanitize_part(contract.company.name if contract.company else "")
    project_code = sanitize_part(contract.project_code or "")
    contract_number = sanitize_part(contract.contract_number or "")
    contract_name = sanitize_part(contract.name or "")
    today_str = datetime.utcnow().strftime("%Y%m%d")
    file_type_label = FILE_TYPE_NAME_MAP.get(file_type, file_type)
    file_type_part = sanitize_part(file_type_label)
    original_name_part = sanitize_part(name_without_ext or "NoFilename")
    version_part = sanitize_part(version or "V1")
    author_part = sanitize_part(author or "unknown")

    parts = [
        company_name or "NoCompany",
        project_code or "NoProject",
        contract_number or "NoContractNo",
        contract_name or "NoName",
        today_str,
        file_type_part,
        original_name_part,
        version_part,
        author_part,
    ]
    base = "_".join(parts)
    if len(base) > 180:
        base = base[:180]
    return base + ext


class FileService:
    """
    文件系统核心业务逻辑：
    - 目录分层
    - 上传文件
    - 多文件上传
    - 列表过滤（基础版）
    - 下载前权限校验
    - 软删除、恢复、公开控制
    """

    # -----------------------------
    # 路径与目录
    # -----------------------------
    def get_project_dir_name(self, contract: Contract) -> str:
        """
        目录名优先使用项目编号（project_code），为空则回退 contract.id
        """
        project_code = getattr(contract, "project_code", None) or ""
        project_code = sanitize_part(str(project_code))

        if project_code:
            return project_code

        # 兜底：确保永远有目录名
        return str(contract.id)

    def get_contract_dir(self, contract: Contract) -> str:
        """返回 uploads/<project_code>（优先），若目录不存在自动创建"""
        root = current_app.config["UPLOAD_FOLDER"]
        dir_name = self.get_project_dir_name(contract)
        project_dir = os.path.join(root, dir_name)
        os.makedirs(project_dir, exist_ok=True)
        return project_dir

    def get_file_path(self, contract: Contract, pf: ProjectFile) -> str:
        """
        兼容历史数据的查找顺序：
        1) 新规则：uploads/<project_code>/<stored_filename>
        2) 旧规则：uploads/<contract_id>/<stored_filename>
        3) 最老规则：uploads/<stored_filename>
        """
        root = current_app.config["UPLOAD_FOLDER"]

        # 1) 新规则：项目编号目录
        dir_name = self.get_project_dir_name(contract)
        new_path = os.path.join(root, dir_name, pf.stored_filename)
        if os.path.exists(new_path):
            return new_path

        # 2) 旧规则：合同 id 目录
        old_contract_dir_path = os.path.join(root, str(contract.id), pf.stored_filename)
        if os.path.exists(old_contract_dir_path):
            return old_contract_dir_path

        # 3) 最老：平铺
        flat_path = os.path.join(root, pf.stored_filename)
        return flat_path

    # -----------------------------
    # 列表过滤（基础 + 最新版本）
    # -----------------------------
    def list_files_for_user(
        self,
        contract: Contract,
        user: Optional[User],
        *,
        file_type: Optional[str] = None,
        is_public: Optional[bool] = None,
        include_deleted: bool = False,
        latest_only: bool = False,
    ) -> List[ProjectFile]:
        """
        :param latest_only: True 时，只返回每个 (file_type, original_filename) 分组中的最新一条
        """
        query = ProjectFile.query.filter_by(contract_id=contract.id)

        if not include_deleted:
            query = query.filter_by(is_deleted=False)

        if file_type:
            query = query.filter_by(file_type=file_type)

        # 客户只能看公开文件 & 限定类型
        if user and user.role == "customer":
            query = query.filter(
                ProjectFile.is_public.is_(True),
                ProjectFile.file_type.in_(["contract", "tech"]),
            )
        else:
            # 内部角色可按 is_public 筛选
            if is_public is not None:
                query = query.filter_by(is_public=is_public)

        # 先按时间倒序取出
        files = query.order_by(ProjectFile.created_at.desc()).all()

        if not latest_only:
            return files

        # latest_only=True：按照 (file_type, 原始文件名) 分组，取每组第一条（最新）
        seen_keys = set()
        latest_files: List[ProjectFile] = []

        for f in files:
            key = (f.file_type, f.original_filename or f.stored_filename)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            latest_files.append(f)

        return latest_files

    # -----------------------------
    # 上传（单文件）
    # -----------------------------
    def save_uploaded_file(
        self,
        contract: Contract,
        user: User,
        uploaded_file: FileStorage,
        file_type: str,
        version: str,
        is_public: bool,
        author: str,
    ) -> ProjectFile:
        # 扩展名校验
        if not allowed_file(uploaded_file.filename, file_type):
            raise ValueError("不允许的文件类型或扩展名")

        # 角色权限校验
        role_types = get_role_allowed_types(user)
        if file_type not in role_types:
            raise PermissionError("当前角色无权上传该类别文件")

        # 生成文件名
        stored_filename = generate_file_name(
            contract, file_type, version, author, uploaded_file.filename
        )

        # 目标路径（按合同分目录）
        contract_dir = self.get_contract_dir(contract)
        file_path = os.path.join(contract_dir, stored_filename)

        # 保存文件
        uploaded_file.save(file_path)

        pf = ProjectFile(
            contract_id=contract.id,
            uploader_id=user.id,
            file_type=file_type,
            version=version,
            author=author,
            original_filename=uploaded_file.filename,
            stored_filename=stored_filename,
            content_type=uploaded_file.mimetype,
            file_size=os.path.getsize(file_path),
            is_public=is_public,
            owner_role=user.role,
        )

        db.session.add(pf)
        db.session.commit()

        # 统一采用和 contracts.py 一样的日志参数风格
        log_operation(
            operator=user,
            contract_id=contract.id,
            object_type=OBJECT_TYPE_FILE,
            object_id=pf.id,
            action=ACTION_UPLOAD,
            old_data=None,
            new_data={
                "file_type": pf.file_type,
                "version": pf.version,
                "original_filename": pf.original_filename,
                "stored_filename": pf.stored_filename,
                "is_public": pf.is_public,
            },
            request=None,  # 在 service 层没有 request，先传 None
        )

        return pf

    # -----------------------------
    # 多文件上传
    # -----------------------------
    def save_multiple_files(
        self,
        contract: Contract,
        user: User,
        files: Iterable[FileStorage],
        file_type: str,
        version: str,
        is_public: bool,
        author: str,
    ) -> List[ProjectFile]:
        saved: List[ProjectFile] = []
        for file in files:
            if not file or not file.filename:
                continue
            pf = self.save_uploaded_file(
                contract, user, file, file_type, version, is_public, author
            )
            saved.append(pf)
        return saved

    # -----------------------------
    # 下载（只做校验 + 日志）
    # -----------------------------
    def get_file_for_download(
        self,
        contract: Contract,
        user: User,
        file_id: int,
    ) -> ProjectFile:
        pf = ProjectFile.query.filter_by(id=file_id, contract_id=contract.id).first()
        if not pf or pf.is_deleted:
            raise FileNotFoundError("文件不存在或已删除")

        if user.role == "customer":
            if not pf.is_public or pf.file_type not in ["contract", "tech"]:
                raise PermissionError("无权下载该文件")

        if user.role not in ("admin", "boss", "software_engineer"):
            if pf.owner_role and pf.owner_role != user.role:
                raise PermissionError("无权限下载该部门文件")

        log_operation(
            operator=user,
            contract_id=contract.id,
            object_type=OBJECT_TYPE_FILE,
            object_id=pf.id,
            action=ACTION_DOWNLOAD,
            old_data=None,
            new_data={
                "download": True,
                "stored_filename": pf.stored_filename,
                "original_filename": pf.original_filename,
            },
            request=None,
        )

        return pf

    # -----------------------------
    # 删除 & 恢复
    # -----------------------------
    def soft_delete_file(
        self, contract: Contract, user: User, file_id: int
    ) -> ProjectFile:
        pf = ProjectFile.query.filter_by(id=file_id, contract_id=contract.id).first()
        if not pf:
            raise FileNotFoundError("文件不存在")

        pf.is_deleted = True
        db.session.commit()

        log_operation(
            operator=user,
            contract_id=contract.id,
            object_type=OBJECT_TYPE_FILE,
            object_id=pf.id,
            action=ACTION_DELETE,
            old_data=None,
            new_data={"is_deleted": True},
            request=None,
        )

        return pf

    def restore_file(
        self, contract: Contract, user: User, file_id: int
    ) -> ProjectFile:
        pf = ProjectFile.query.filter_by(id=file_id, contract_id=contract.id).first()
        if not pf:
            raise FileNotFoundError("文件不存在")

        pf.is_deleted = False
        db.session.commit()

        log_operation(
            operator=user,
            contract_id=contract.id,
            object_type=OBJECT_TYPE_FILE,
            object_id=pf.id,
            action=ACTION_RESTORE,
            old_data=None,
            new_data={"is_deleted": False},
            request=None,
        )

        return pf

    # -----------------------------
    # 设置公开 / 取消公开
    # -----------------------------
    def set_public(
        self, contract: Contract, user: User, file_id: int, is_public: bool
    ) -> ProjectFile:
        if user.role not in ("admin", "boss", "sales"):
            raise PermissionError("无权更改公开状态")

        pf = ProjectFile.query.filter_by(id=file_id, contract_id=contract.id).first()
        if not pf:
            raise FileNotFoundError("文件不存在")

        old = pf.is_public
        pf.is_public = is_public
        db.session.commit()

        old = pf.is_public
        pf.is_public = is_public
        db.session.commit()

        log_operation(
            operator=user,
            contract_id=contract.id,
            object_type=OBJECT_TYPE_FILE,
            object_id=pf.id,
            action=ACTION_UPDATE,
            old_data={"is_public": old},
            new_data={"is_public": is_public},
            request=None,
        )

        return pf