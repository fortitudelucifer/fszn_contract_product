# fszn/services/preview_service.py
import os
import subprocess
import mimetypes
from typing import Optional

from flask import current_app

from ..models import ProjectFile, Contract


# 支持通过 LibreOffice 转换预览的 Office 扩展名
OFFICE_PREVIEW_EXTS = {"doc", "docx", "xls", "xlsx", "ppt", "pptx"}


def _sanitize_part(text: str) -> str:
    """用于目录/文件名的一段清洗：去掉非法字符"""
    if not text:
        return ""
    invalid = '\\/:*?"<>|'
    for ch in invalid:
        text = text.replace(ch, "")
    text = text.replace(" ", "_")
    return text


class PreviewService:
    """
    负责文件“预览版本”的生成（目前只支持 Office -> PDF）：
    - 只对 doc/docx/xls/xlsx/ppt/pptx 做处理
    - 首次访问时调用 LibreOffice 转换
    - 后续访问命中缓存（预览文件存在且比源文件新）
    """

    def _is_office_file(self, pf: ProjectFile) -> bool:
        name = (pf.original_filename or pf.stored_filename or "").lower()
        if "." not in name:
            return False
        ext = name.rsplit(".", 1)[1]
        return ext in OFFICE_PREVIEW_EXTS

    def _get_preview_root(self) -> str:
        """
        预览文件根目录：
        优先使用 config['PREVIEW_FOLDER']，
        否则使用 UPLOAD_FOLDER/preview
        """
        root = current_app.config.get("PREVIEW_FOLDER")
        if not root:
            root = os.path.join(current_app.config["UPLOAD_FOLDER"], "preview")
        os.makedirs(root, exist_ok=True)
        return root

    def _get_contract_preview_dir(self, contract: Contract) -> str:
        """
        按项目编号分目录：
        PREVIEW_ROOT / <project_code_清洗后 或 contract.id>
        """
        root = self._get_preview_root()
        project_code = _sanitize_part(getattr(contract, "project_code", "") or "")
        if not project_code:
            project_code = str(contract.id)
        path = os.path.join(root, project_code)
        os.makedirs(path, exist_ok=True)
        return path

    def _get_preview_target_path(self, contract: Contract, pf: ProjectFile) -> str:
        """
        预览文件命名：
        <预览目录>/<原始文件名去扩展>_preview.pdf
        """
        name = pf.original_filename or pf.stored_filename or "file"
        base = os.path.splitext(os.path.basename(name))[0]
        base = _sanitize_part(base) or "file"
        return os.path.join(self._get_contract_preview_dir(contract), f"{base}_preview.pdf")

    def _run_libreoffice_convert(self, src_path: str, out_dir: str) -> bool:
        """
        调用 LibreOffice 将 src_path 转为 PDF 输出到 out_dir
        :return: True 表示尝试成功（不代表业务上文件一定存在），False 表示失败
        """
        soffice = (
            current_app.config.get("LIBREOFFICE_PATH")
            or "soffice"  # 默认走 PATH
        )

        cmd = [
            soffice,
            "--headless",
            "--nologo",
            "--invisible",
            "--convert-to",
            "pdf",
            "--outdir",
            out_dir,
            src_path,
        ]

        timeout = current_app.config.get("LIBREOFFICE_TIMEOUT", 60)

        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
            )
        except Exception as e:
            current_app.logger.exception("LibreOffice 转换调用失败: %s", e)
            return False

        if proc.returncode != 0:
            current_app.logger.error(
                "LibreOffice 转换失败，返回码=%s，stdout=%s，stderr=%s",
                proc.returncode,
                proc.stdout.decode(errors="ignore"),
                proc.stderr.decode(errors="ignore"),
            )
            return False

        return True

    def get_or_generate_office_preview(
        self,
        contract: Contract,
        pf: ProjectFile,
        src_path: str,
    ) -> Optional[str]:
        """
        获取（或生成）Office 文件的 PDF 预览路径：
        - 若不是 Office 文件 → 返回 None
        - 若预览文件已存在且比源文件新 → 直接返回
        - 否则调用 LibreOffice 转换，成功则返回预览 PDF 路径
        """
        if not self._is_office_file(pf):
            return None

        target = self._get_preview_target_path(contract, pf)

        # 有缓存且比源文件新，直接用
        if os.path.exists(target):
            try:
                if os.path.getmtime(target) >= os.path.getmtime(src_path):
                    return target
            except OSError:
                # 读取 mtime 失败则走转换逻辑
                pass

        # 执行转换
        out_dir = os.path.dirname(target)
        ok = self._run_libreoffice_convert(src_path, out_dir)
        if not ok:
            return None

        # LibreOffice 按原文件名输出为 <base>.pdf，可能不是 _preview 后缀
        # 我们优先使用预期命名，若不存在则回退按基础名找
        if os.path.exists(target):
            return target

        # 优先按 src_path 的 base 去找（LibreOffice 输出名通常就是它）
        src_base = os.path.splitext(os.path.basename(src_path))[0]
        src_base = _sanitize_part(src_base) or "file"
        candidate = os.path.join(out_dir, f"{src_base}.pdf")

        if os.path.exists(candidate):
            # 为了后续统一命名，可以重命名到 *_preview.pdf（可选）
            try:
                os.replace(candidate, target)
                return target
            except OSError:
                # 重命名失败也可以直接用 candidate
                return candidate

    # （可选兜底）再按 original_filename 的 base 尝试一次，防止某些环境输出跟 original 一致
        name = pf.original_filename or pf.stored_filename or "file"
        base = os.path.splitext(os.path.basename(name))[0]
        base = _sanitize_part(base) or "file"
        candidate2 = os.path.join(out_dir, f"{base}.pdf")
        if os.path.exists(candidate2):
            try:
                os.replace(candidate2, target)
                return target
            except OSError:
                return candidate2

            try:
                pdfs = [
                    os.path.join(out_dir, fn)
                    for fn in os.listdir(out_dir)
                    if fn.lower().endswith(".pdf")
                ]
                if pdfs:
                    newest = max(pdfs, key=lambda p: os.path.getmtime(p))
                    try:
                        os.replace(newest, target)
                        return target
                    except OSError:
                        return newest
            except OSError:
                pass
        return None


# 提供一个单例给其他模块使用
preview_service = PreviewService()
