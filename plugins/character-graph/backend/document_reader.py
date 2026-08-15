"""Extract plain text from uploaded documents.

Supports: .txt, .md, .json, .csv (plain text)
          .docx (python-docx)
          .pdf  (pypdf)
"""

from pathlib import Path

SUPPORTED = {
    ".txt": "文本",
    ".md": "Markdown",
    ".json": "JSON",
    ".csv": "CSV",
    ".docx": "Word 文档",
    ".pdf": "PDF",
}


def extract_text(filename: str, raw: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in (".txt", ".md", ".json", ".csv"):
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
    try:
        import io

        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(raw))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except ImportError:
        raise ValueError("读取 .pdf 需要安装 pypdf：pip install pypdf")
    except Exception as e:
        raise ValueError(f"解析 .pdf 失败：{e}")
