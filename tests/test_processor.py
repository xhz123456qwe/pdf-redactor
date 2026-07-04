from __future__ import annotations

from pathlib import Path
from typing import List

import fitz
import pytest
from PIL import Image

from pdf_redactor.models import OCRLine, ProcessingMode, RedactionColor, RedactionRequest, parse_color
from pdf_redactor.processor import (
    PDFRedactor,
    find_ocr_targets,
    normalize_keywords,
    output_path_for,
    strict_native_matches,
)


class EmptyOCR:
    def recognize(self, _image: Image.Image) -> List[OCRLine]:
        return []


class OneLineOCR:
    def __init__(self, text: str, quad=((100.0, 100.0), (300.0, 100.0), (300.0, 130.0), (100.0, 130.0))) -> None:
        self.line = OCRLine(text=text, confidence=0.99, quad=quad)

    def recognize(self, _image: Image.Image) -> List[OCRLine]:
        return [self.line]


class CountingOCR:
    def __init__(self) -> None:
        self.calls = 0

    def recognize(self, _image: Image.Image) -> List[OCRLine]:
        self.calls += 1
        return []


class CountingOneLineOCR(OneLineOCR):
    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.calls = 0

    def recognize(self, image: Image.Image) -> List[OCRLine]:
        self.calls += 1
        return super().recognize(image)


def make_text_pdf(path: Path, text: str = "Secret secret") -> None:
    document = fitz.open()
    page = document.new_page(width=300, height=200)
    page.insert_text((72, 72), text, fontsize=16, fontname="helv")
    document.save(path)
    document.close()


def make_cross_page_text_pdf(path: Path) -> None:
    document = fitz.open()
    first = document.new_page(width=300, height=200)
    first.insert_text((72, 120), "Sec", fontsize=16, fontname="helv")
    second = document.new_page(width=300, height=200)
    second.insert_text((72, 72), "ret", fontsize=16, fontname="helv")
    document.save(path)
    document.close()


def make_wrapped_text_pdf(path: Path) -> None:
    document = fitz.open()
    page = document.new_page(width=300, height=200)
    page.insert_text((72, 72), "based", fontsize=16, fontname="helv")
    page.insert_text((72, 96), "AFSIM", fontsize=16, fontname="helv")
    document.save(path)
    document.close()


def make_wrapped_text_pdf_with_intervening_record(path: Path) -> None:
    document = fitz.open()
    page = document.new_page(width=300, height=200)
    page.insert_text((120, 72), "foo", fontsize=16, fontname="helv")
    page.insert_text((20, 82), "side", fontsize=16, fontname="helv")
    page.insert_text((72, 96), "bar", fontsize=16, fontname="helv")
    document.save(path)
    document.close()


def make_image_pdf(path: Path, background=(255, 255, 255)) -> None:
    image = Image.new("RGB", (834, 834), background)
    document = fitz.open()
    page = document.new_page(width=200, height=200)
    from io import BytesIO

    buffer = BytesIO()
    image.save(buffer, format="PNG")
    page.insert_image(page.rect, stream=buffer.getvalue())
    document.save(path)
    document.close()


def make_mixed_text_and_image_pdf(path: Path) -> None:
    image = Image.new("RGB", (834, 834), (255, 255, 255))
    document = fitz.open()
    page = document.new_page(width=200, height=200)
    from io import BytesIO

    buffer = BytesIO()
    image.save(buffer, format="PNG")
    page.insert_image(page.rect, stream=buffer.getvalue())
    page.insert_text((20, 180), "Native text", fontsize=12, fontname="helv")
    document.save(path)
    document.close()


def test_normalize_keywords_is_ordered_and_exact() -> None:
    assert normalize_keywords("\n张三\n张三\n Secret \n\n") == ["张三", "Secret"]
    assert parse_color("#4F81BD") == (79, 129, 189)


def test_ocr_match_can_cross_wrapped_lines() -> None:
    lines = [
        OCRLine("建", 0.99, ((10, 10), (30, 10), (30, 30), (10, 30))),
        OCRLine("筑工程", 0.99, ((10, 34), (90, 34), (90, 54), (10, 54))),
    ]
    targets = find_ocr_targets(lines, "建筑工程")
    assert len(targets) == 2
    assert {target.source_line for target in targets} == {"建", "筑工程"}
    assert len({target.occurrence_id for target in targets}) == 1


def test_ocr_cross_line_match_allows_adjacent_rows_with_different_indents() -> None:
    lines = [
        OCRLine("建", 0.99, ((10, 10), (30, 10), (30, 30), (10, 30))),
        OCRLine("筑工程", 0.99, ((180, 34), (260, 34), (260, 54), (180, 54))),
    ]
    assert len(find_ocr_targets(lines, "建筑工程")) == 2


def test_cross_line_ocr_ignores_intervening_same_row_record() -> None:
    lines = [
        OCRLine("幕", 0.99, ((100, 10), (120, 10), (120, 30), (100, 30))),
        OCRLine("旁注", 0.99, ((10, 20), (50, 20), (50, 40), (10, 40))),
        OCRLine("墙工程", 0.99, ((10, 34), (70, 34), (70, 54), (10, 54))),
    ]
    targets = find_ocr_targets(lines, "幕墙工程")
    assert [target.source_line for target in targets] == ["幕", "墙工程"]


def test_native_match_can_cross_adjacent_rows_in_fast_mode(tmp_path: Path) -> None:
    source = tmp_path / "wrapped.pdf"
    make_wrapped_text_pdf(source)
    ocr = CountingOCR()
    processor = PDFRedactor(ocr_provider=ocr)
    session = processor.prepare_review(
        RedactionRequest(source, ["basedAFSIM"], RedactionColor.BLACK, processing_mode=ProcessingMode.FAST)
    )
    assert ocr.calls == 0
    assert session.keyword_counts == {"basedAFSIM": 1}
    assert len([overlay for overlay in session.overlays(0, PDFRedactor.SCALE) if overlay.keyword == "basedAFSIM"]) == 2
    result = processor.finalize_review(session)
    output = fitz.open(result.output_path)
    assert "based" not in output[0].get_text()
    assert "AFSIM" not in output[0].get_text()
    output.close()


def test_native_cross_line_match_ignores_intervening_same_row_record(tmp_path: Path) -> None:
    source = tmp_path / "wrapped_with_side_record.pdf"
    make_wrapped_text_pdf_with_intervening_record(source)
    processor = PDFRedactor(ocr_provider=CountingOCR())
    session = processor.prepare_review(
        RedactionRequest(source, ["foobar"], RedactionColor.BLACK, processing_mode=ProcessingMode.FAST)
    )
    assert session.keyword_counts == {"foobar": 1}
    assert len([overlay for overlay in session.overlays(0, PDFRedactor.SCALE) if overlay.keyword == "foobar"]) == 2


def test_native_match_can_cross_a_page_boundary(tmp_path: Path) -> None:
    source = tmp_path / "cross_page.pdf"
    make_cross_page_text_pdf(source)
    processor = PDFRedactor(ocr_provider=EmptyOCR())
    session = processor.prepare_review(RedactionRequest(source, ["Secret"], RedactionColor.BLACK))
    assert session.keyword_counts == {"Secret": 1}
    assert session.page_matches == {0: ["Secret"]}
    assert session.cross_page_continuations == {0: [("Secret", 2)]}
    assert any(overlay.keyword == "Secret" for overlay in session.overlays(0, PDFRedactor.SCALE))
    assert any(overlay.keyword == "Secret" for overlay in session.overlays(1, PDFRedactor.SCALE))
    result = processor.finalize_review(session)
    output = fitz.open(result.output_path)
    assert "Sec" not in output[0].get_text()
    assert "ret" not in output[1].get_text()
    output.close()


def test_output_path_never_overwrites_and_supports_chinese_names(tmp_path: Path) -> None:
    source = tmp_path / "合同张三.pdf"
    source.touch()
    assert output_path_for(source).name == "合同张三_redacted.pdf"
    (tmp_path / "合同张三_redacted.pdf").touch()
    assert output_path_for(source).name == "合同张三_redacted_1.pdf"
    requested = tmp_path / "结果" / "人工命名.pdf"
    requested.parent.mkdir()
    assert output_path_for(source, requested) == requested
    requested.touch()
    assert output_path_for(source, requested).name == "人工命名_1.pdf"
    with pytest.raises(ValueError):
        output_path_for(source, source)


def test_fast_mode_skips_ocr_for_native_text_pages(tmp_path: Path) -> None:
    source = tmp_path / "fast_mode.pdf"
    make_text_pdf(source)
    fast_ocr = CountingOCR()
    PDFRedactor(ocr_provider=fast_ocr).prepare_review(
        RedactionRequest(source, ["Secret"], RedactionColor.BLACK, processing_mode=ProcessingMode.FAST)
    )
    assert fast_ocr.calls == 0
    comprehensive_ocr = CountingOCR()
    PDFRedactor(ocr_provider=comprehensive_ocr).prepare_review(
        RedactionRequest(source, ["Secret"], RedactionColor.BLACK, processing_mode=ProcessingMode.COMPREHENSIVE)
    )
    assert comprehensive_ocr.calls == 1


def test_fast_mode_runs_ocr_for_mixed_text_and_image_page(tmp_path: Path) -> None:
    source = tmp_path / "mixed.pdf"
    make_mixed_text_and_image_pdf(source)
    ocr = CountingOneLineOCR("SECRET")
    processor = PDFRedactor(ocr_provider=ocr)
    session = processor.prepare_review(
        RedactionRequest(source, ["SECRET"], RedactionColor.BLACK, processing_mode=ProcessingMode.FAST)
    )
    assert ocr.calls == 1
    assert session.keyword_counts == {"SECRET": 1}
    assert session.plans[0].needs_flattening
    result = processor.finalize_review(session)
    assert result.flattened_pages == [1]


def test_fast_mode_runs_ocr_for_scan_without_text_layer(tmp_path: Path) -> None:
    source = tmp_path / "scan_fast.pdf"
    make_image_pdf(source)
    ocr = CountingOCR()
    PDFRedactor(ocr_provider=ocr).prepare_review(
        RedactionRequest(source, ["SECRET"], RedactionColor.BLACK, processing_mode=ProcessingMode.FAST)
    )
    assert ocr.calls == 1


def test_native_text_is_truly_removed_with_case_sensitive_matching(tmp_path: Path) -> None:
    source = tmp_path / "native.pdf"
    make_text_pdf(source)
    original = fitz.open(source)
    assert len(strict_native_matches(original[0], "Secret")) == 1
    original.close()

    result = PDFRedactor(ocr_provider=EmptyOCR()).redact(
        RedactionRequest(source, ["Secret"], RedactionColor.BLACK)
    )

    output = fitz.open(result.output_path)
    text = output[0].get_text()
    assert "Secret" not in text
    assert "secret" in text
    assert not strict_native_matches(output[0], "Secret")
    output.close()
    assert result.keyword_counts == {"Secret": 1}
    assert result.succeeded


def test_ocr_only_hit_rebuilds_page_and_applies_black_mask(tmp_path: Path) -> None:
    source = tmp_path / "scan.pdf"
    make_image_pdf(source)
    source_document = fitz.open(source)
    source_xref = source_document[0].get_images(full=True)[0][0]
    source_image = source_document.extract_image(source_xref)["image"]
    source_document.close()
    result = PDFRedactor(ocr_provider=OneLineOCR("SECRET")).redact(
        RedactionRequest(source, ["SECRET"], RedactionColor.BLACK)
    )

    output = fitz.open(result.output_path)
    assert output[0].get_text() == ""
    output_xref = output[0].get_images(full=True)[0][0]
    assert output.extract_image(output_xref)["image"] != source_image
    pixmap = output[0].get_pixmap(matrix=fitz.Matrix(PDFRedactor.SCALE, PDFRedactor.SCALE), alpha=False)
    image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
    assert image.getpixel((200, 115)) == (0, 0, 0)
    output.close()
    assert result.flattened_pages == [1]
    assert result.estimated_pages == [1]
    assert result.keyword_counts == {"SECRET": 1}


def test_ocr_only_hit_applies_white_mask(tmp_path: Path) -> None:
    source = tmp_path / "dark_scan.pdf"
    make_image_pdf(source, background=(0, 0, 0))
    result = PDFRedactor(ocr_provider=OneLineOCR("SECRET")).redact(
        RedactionRequest(source, ["SECRET"], RedactionColor.WHITE)
    )

    output = fitz.open(result.output_path)
    pixmap = output[0].get_pixmap(matrix=fitz.Matrix(PDFRedactor.SCALE, PDFRedactor.SCALE), alpha=False)
    image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
    assert image.getpixel((200, 115)) == (255, 255, 255)
    output.close()


def test_ocr_only_hit_accepts_custom_hex_color(tmp_path: Path) -> None:
    source = tmp_path / "custom_color_scan.pdf"
    make_image_pdf(source)
    result = PDFRedactor(ocr_provider=OneLineOCR("SECRET")).redact(
        RedactionRequest(source, ["SECRET"], "#4F81BD")
    )

    output = fitz.open(result.output_path)
    pixmap = output[0].get_pixmap(matrix=fitz.Matrix(PDFRedactor.SCALE, PDFRedactor.SCALE), alpha=False)
    image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
    assert image.getpixel((200, 115)) == (79, 129, 189)
    output.close()


def test_manual_selection_flattens_and_masks_only_selected_area(tmp_path: Path) -> None:
    source = tmp_path / "manual_selection.pdf"
    make_image_pdf(source)
    result = PDFRedactor(ocr_provider=EmptyOCR()).redact(
        RedactionRequest(source, ["unmatched"], RedactionColor.BLACK, manual_rects={0: [(30.0, 30.0, 70.0, 70.0)]})
    )
    output = fitz.open(result.output_path)
    pixmap = output[0].get_pixmap(matrix=fitz.Matrix(PDFRedactor.SCALE, PDFRedactor.SCALE), alpha=False)
    image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
    assert image.getpixel((200, 200)) == (0, 0, 0)
    output.close()
    assert result.manual_pages == [1]


def test_encrypted_input_preserves_open_password(tmp_path: Path) -> None:
    source = tmp_path / "protected.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "TOP SECRET", fontsize=16)
    document.save(
        source,
        encryption=fitz.PDF_ENCRYPT_AES_256,
        user_pw="openme",
        owner_pw="owner-password",
        permissions=fitz.PDF_PERM_ACCESSIBILITY,
    )
    document.close()

    result = PDFRedactor(ocr_provider=EmptyOCR()).redact(
        RedactionRequest(source, ["SECRET"], RedactionColor.BLACK, password="openme")
    )
    output = fitz.open(result.output_path)
    assert output.needs_pass
    assert output.authenticate("openme")
    assert not output[0].search_for("SECRET")
    output.close()
