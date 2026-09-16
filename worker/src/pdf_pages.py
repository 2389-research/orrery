# ABOUTME: Pure PDF helpers for page-by-page ingestion — rasterize pages to PNG bytes
# ABOUTME: and extract per-page text, both via pypdfium2 (no pypdf/cryptography). No DB, no LLM.

from __future__ import annotations

# Below this many non-whitespace chars a page's text is "thin" (drives vision_mode='fallback').
THIN_TEXT_CHARS = 40


def is_pdf_bytes(data: bytes) -> bool:
    """True if the bytes start with the %PDF magic marker."""
    return data[:4] == b"%PDF"


def rasterize_pdf(file_bytes: bytes, dpi: int = 150) -> list[bytes]:
    """Render each page to PNG bytes, in page order. In-memory only."""
    import io
    import pypdfium2 as pdfium

    scale = dpi / 72.0  # pdfium default user space is 72 DPI
    pdf = pdfium.PdfDocument(file_bytes)
    try:
        out: list[bytes] = []
        for i in range(len(pdf)):
            bitmap = pdf[i].render(scale=scale)
            buf = io.BytesIO()
            bitmap.to_pil().save(buf, format="PNG")
            out.append(buf.getvalue())
        return out
    finally:
        pdf.close()


def pdf_page_texts(file_bytes: bytes) -> list[str]:
    """Extract per-page text via pypdfium2 (NOT pypdf), same length/order as rasterize_pdf."""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(file_bytes)
    try:
        return [pdf[i].get_textpage().get_text_range() for i in range(len(pdf))]
    finally:
        pdf.close()
