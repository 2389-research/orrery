from pathlib import Path
from src.pdf_pages import rasterize_pdf, pdf_page_texts, THIN_TEXT_CHARS, is_pdf_bytes

FIXTURE = Path(__file__).parent / "fixtures" / "sample.pdf"
def _bytes(): return FIXTURE.read_bytes()

def test_rasterize_returns_one_png_per_page():
    pngs = rasterize_pdf(_bytes())
    assert len(pngs) == 2
    for p in pngs:
        assert p[:4] == b"\x89PNG"

def test_page_texts_are_ordered_and_match_pages():
    texts = pdf_page_texts(_bytes())
    assert len(texts) == 2
    assert "ALPHA" in texts[0].upper()
    assert "BRAVO" in texts[1].upper()

def test_rasterize_and_text_agree_on_page_count():
    assert len(rasterize_pdf(_bytes())) == len(pdf_page_texts(_bytes()))

def test_is_pdf_bytes():
    assert is_pdf_bytes(_bytes()) is True
    assert is_pdf_bytes(b"not a pdf") is False

def test_thin_text_threshold_is_sane():
    assert 0 < THIN_TEXT_CHARS < 200
