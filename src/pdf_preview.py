"""Optional LibreOffice-backed PDF preview conversion."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

DEFAULT_PDF_TIMEOUT_SECONDS = 45


class PdfPreviewError(RuntimeError):
    """Raised when an optional PDF preview cannot be produced."""


class PdfPreviewService:
    """Convert a DOCX snapshot to PDF without involving the LLM or a shell."""

    def __init__(self, executable: str | None = None, timeout: int = DEFAULT_PDF_TIMEOUT_SECONDS):
        if timeout <= 0:
            raise ValueError("PDF 预览超时时间必须大于 0。")
        self.executable = executable or shutil.which("soffice") or shutil.which("libreoffice")
        self.timeout = timeout

    @classmethod
    def from_environment(cls) -> PdfPreviewService:
        """Build a converter from optional environment configuration."""
        executable = os.environ.get("LIBREOFFICE_PATH", "").strip() or None
        try:
            timeout = int(os.environ.get("PDF_PREVIEW_TIMEOUT", DEFAULT_PDF_TIMEOUT_SECONDS))
        except ValueError as exc:
            raise ValueError("PDF_PREVIEW_TIMEOUT 必须是整数。") from exc
        return cls(executable, timeout)

    @property
    def available(self) -> bool:
        """Return whether a configured LibreOffice executable is available."""
        if not self.executable:
            return False
        candidate = Path(self.executable)
        return (
            candidate.is_file()
            if candidate.is_absolute()
            else shutil.which(self.executable) is not None
        )

    def convert(self, docx_path: Path, output_path: Path) -> Path:
        """Convert one DOCX into a validated PDF through an isolated user profile."""
        if not self.available:
            raise PdfPreviewError("当前环境未安装 LibreOffice，PDF 版式预览不可用。")
        if not docx_path.is_file():
            raise PdfPreviewError("当前 DOCX 快照不存在，请重新生成预览。")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="weekly-report-pdf-") as directory:
            workdir = Path(directory)
            converted_dir = workdir / "output"
            profile_dir = workdir / "profile"
            converted_dir.mkdir()
            profile_dir.mkdir()
            command = [
                str(self.executable),
                "--headless",
                f"-env:UserInstallation={profile_dir.resolve().as_uri()}",
                "--convert-to",
                "pdf",
                "--outdir",
                str(converted_dir),
                str(docx_path.resolve()),
            ]
            try:
                result = subprocess.run(
                    command,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    check=False,
                    timeout=self.timeout,
                )
            except subprocess.TimeoutExpired as exc:
                raise PdfPreviewError("PDF 版式预览生成超时，请稍后重试。") from exc
            except OSError as exc:
                raise PdfPreviewError("无法启动 LibreOffice，PDF 版式预览不可用。") from exc

            converted = converted_dir / f"{docx_path.stem}.pdf"
            if result.returncode != 0 or not converted.is_file():
                raise PdfPreviewError("LibreOffice 未能生成 PDF，DOCX 下载不受影响。")
            content = converted.read_bytes()
            if len(content) < 5 or not content.startswith(b"%PDF-"):
                raise PdfPreviewError("LibreOffice 返回了无效 PDF，DOCX 下载不受影响。")
            temporary = output_path.with_suffix(".tmp")
            temporary.write_bytes(content)
            temporary.chmod(0o600)
            os.replace(temporary, output_path)
        return output_path
