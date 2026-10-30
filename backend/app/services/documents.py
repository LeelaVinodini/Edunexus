from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi import HTTPException, UploadFile
from pypdf import PdfReader
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.models import Chunk, Document
from app.services.embeddings import embed_text
from app.services.storage import resolve_storage_path

ALLOWED_EXTENSIONS = {"pdf", "txt", "md", "docx", "pptx"}


async def read_upload_limited(upload: UploadFile, max_bytes: int, message: str) -> bytes:
    """Read at most max_bytes plus one byte so an oversized upload never has to be buffered whole."""
    if upload.size is not None and upload.size > max_bytes:
        raise HTTPException(status_code=400, detail=message)
    data = await upload.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(status_code=400, detail=message)
    return data


def file_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_filename(name: str) -> str:
    cleaned = Path(name).name.replace("\x00", "").strip()
    return cleaned[:180] or "upload"


def extension_of(name: str) -> str:
    return safe_filename(name).rsplit(".", 1)[-1].lower() if "." in name else ""


def extract_pages(path: Path, extension: str) -> tuple[list[tuple[int | None, str]], str | None]:
    if extension in {"txt", "md"}:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        return ([(None, text)] if text else []), None
    if extension == "pdf":
        header = path.read_bytes()[:5]
        if header != b"%PDF-":
            return [], "The file does not look like a PDF."
        reader = PdfReader(str(path))
        pages: list[tuple[int | None, str]] = []
        for index, page in enumerate(reader.pages, start=1):
            text = (page.extract_text() or "").strip()
            if text:
                pages.append((index, text))
        return pages, None
    if extension == "docx":
        text = _extract_docx(path).strip()
        return ([(None, text)] if text else []), None
    if extension == "pptx":
        text = _extract_pptx(path).strip()
        return ([(None, text)] if text else []), None
    return [], f"'.{extension or 'unknown'}' files are not supported. Upload PDF, PPTX, DOCX, TXT, or Markdown."


def _extract_docx(path: Path) -> str:
    import zipfile
    from xml.etree import ElementTree

    with zipfile.ZipFile(path) as archive:
        xml = archive.read("word/document.xml")
    root = ElementTree.fromstring(xml)
    parts = [node.text or "" for node in root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t")]
    return " ".join(part for part in parts if part)


def _extract_pptx(path: Path) -> str:
    import re
    import zipfile
    from xml.etree import ElementTree

    namespace = "{http://schemas.openxmlformats.org/drawingml/2006/main}t"
    with zipfile.ZipFile(path) as archive:
        slides = sorted(
            (name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
            key=lambda name: int(re.search(r"slide(\d+)", name).group(1)),
        )
        slide_text = []
        for slide in slides:
            root = ElementTree.fromstring(archive.read(slide))
            values = [node.text or "" for node in root.iter(namespace)]
            if values:
                slide_text.append(" ".join(values))
    return "\n".join(slide_text)


def chunk_pages(pages: list[tuple[int | None, str]], size: int = 800, overlap: int = 100) -> list[tuple[int | None, str]]:
    chunks: list[tuple[int | None, str]] = []
    step = max(size - overlap, 1)
    for page, text in pages:
        start = 0
        while start < len(text):
            piece = text[start : start + size].strip()
            if piece:
                chunks.append((page, piece))
            if start + size >= len(text):
                break
            start += step
    return chunks


def process_document(db: Session, document: Document, upload_dir: Path) -> Document:
    path = resolve_storage_path(upload_dir, document.storage_path)
    extension = extension_of(document.filename)
    try:
        pages, error = extract_pages(path, extension)
        chunks = chunk_pages(pages) if not error else []
        if error:
            document.status = "failed"
            document.error = error
        elif not chunks:
            document.status = "failed"
            document.error = "No text could be extracted from this file."
        else:
            db.execute(delete(Chunk).where(Chunk.document_id == document.id))
            for ordinal, (page, text) in enumerate(chunks):
                db.add(Chunk(
                    document_id=document.id,
                    ordinal=ordinal,
                    page=page,
                    text=text,
                    embedding=json.dumps(embed_text(text)),
                ))
            document.status = "ready"
            document.error = None
    except Exception:
        document.status = "failed"
        document.error = "The file could not be processed."
    db.add(document)
    db.commit()
    db.refresh(document)
    return document
