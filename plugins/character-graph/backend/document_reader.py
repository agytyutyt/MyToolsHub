"""从用户上传的文档中抽取纯文本。

支持：.txt / .md / .json / .csv（纯文本直接读取）
      .docx（python-docx）
      .pdf （pypdf）
"""

from pathlib import Path

# 支持的文件类型 -> 中文说明（用于错误提示）
SUPPORTED = {
    ".txt": "文本",
    ".md": "Markdown",
    ".json": "JSON",
    ".csv": "CSV",
    ".docx": "Word 文档",
    ".pdf": "PDF",
}


def extract_text(filename: str, raw: bytes) -> str:
    """按扩展名分发解析，返回提取出的纯文本。"""
    suffix = Path(filename).suffix.lower()
    if suffix in (".txt", ".md", ".json", ".csv"):
        # 纯文本：UTF-8 解码，非法字节用替换符兜底
        return raw.decode("utf-8", errors="replace")
    if suffix == ".docx":
        return _extract_docx(raw)
    if suffix == ".pdf":
        return _extract_pdf(raw)
    raise ValueError(
        f"不支持的文件类型 {suffix or '(无扩展名)'}，仅支持："
        + "、".join(SUPPORTED.values())
    )


def _extract_docx(raw: bytes) -> str:
    """解析 .docx：逐段落拼接为纯文本。"""
    try:
        import io

        from docx import Document

        doc = Document(io.BytesIO(raw))
        return "\n".join(p.text for p in doc.paragraphs)
    except ImportError:
        raise ValueError("读取 .docx 需要安装 python-docx：pip install python-docx")
    except Exception as e:
        raise ValueError(f"解析 .docx 失败：{e}")


def _extract_pdf(raw: bytes) -> str:
    """解析 .pdf：逐页抽取文本并拼接。"""
    try:
        import io

        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(raw))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except ImportError:
        raise ValueError("读取 .pdf 需要安装 pypdf：pip install pypdf")
    except Exception as e:
        raise ValueError(f"解析 .pdf 失败：{e}")
