"""Secure local PDF redaction pipeline."""

from __future__ import annotations

import os
import secrets
import unicodedata
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import fitz
from PIL import Image, ImageDraw

from .models import OCRLine, OCRTarget, ProcessingMode, Quad, RedactionRequest, RedactionResult, color_to_pdf_rgb, parse_color
from .ocr import OCRProvider, PaddleOCRProvider


StatusCallback = Callable[[str], None]
Rect = fitz.Rect


class RedactionError(RuntimeError):
    """A user-facing processing error."""


@dataclass
class PagePlan:
    index: int
    native_rects: Dict[str, List[Rect]] = field(default_factory=dict)
    visual_targets: Dict[str, List[OCRTarget]] = field(default_factory=dict)
    manual_rects: List[Rect] = field(default_factory=list)

    @property
    def needs_flattening(self) -> bool:
        return bool(self.manual_rects) or any(self.visual_targets.values())


@dataclass(frozen=True)
class ReviewOverlay:
    id: str
    page_index: int
    keyword: str
    quad: Quad
    source: str


@dataclass
class ReviewSession:
    """Editable redaction plan created before an output PDF is written."""

    request: RedactionRequest
    plans: List[PagePlan]
    keyword_counts: Dict[str, int]
    page_rects: List[Tuple[float, float, float, float]]
    page_matches: Dict[int, List[str]]
    cross_page_continuations: Dict[int, List[Tuple[str, int]]]

    def overlays(self, page_index: int, scale: float) -> List[ReviewOverlay]:
        page = self.plans[page_index]
        page_rect = Rect(self.page_rects[page_index])
        overlays: List[ReviewOverlay] = []
        for keyword, rectangles in page.native_rects.items():
            for index, rect in enumerate(rectangles):
                overlays.append(
                    ReviewOverlay(
                        f"native:{keyword}:{index}", page_index, keyword,
                        ((rect.x0, rect.y0), (rect.x1, rect.y0), (rect.x1, rect.y1), (rect.x0, rect.y1)), "文字层"
                    )
                )
        for keyword, targets in page.visual_targets.items():
            for index, target in enumerate(targets):
                quad = tuple((page_rect.x0 + x / scale, page_rect.y0 + y / scale) for x, y in target.quad)
                overlays.append(ReviewOverlay(f"visual:{keyword}:{index}", page_index, keyword, quad, "图片 OCR"))
        for index, rect in enumerate(page.manual_rects):
            overlays.append(
                ReviewOverlay(
                    f"manual:手动:{index}", page_index, "手动遮盖",
                    ((rect.x0, rect.y0), (rect.x1, rect.y0), (rect.x1, rect.y1), (rect.x0, rect.y1)), "手动"
                )
            )
        return overlays

    def remove_overlay(self, overlay_id: str, page_index: int) -> bool:
        try:
            source, keyword, index_text = overlay_id.split(":", 2)
            index = int(index_text)
            page = self.plans[page_index]
            if source == "native":
                page.native_rects[keyword].pop(index)
            elif source == "visual":
                page.visual_targets[keyword].pop(index)
            elif source == "manual":
                page.manual_rects.pop(index)
            else:
                return False
            return True
        except (IndexError, KeyError, ValueError):
            return False

    def add_manual_rect(self, page_index: int, values: Tuple[float, float, float, float]) -> None:
        rect = Rect(values)
        if rect.get_area() > 0:
            self.plans[page_index].manual_rects.append(rect)


def normalize_keywords(raw_text: str) -> List[str]:
    """Remove blank/duplicate lines while preserving first-entry order exactly."""

    keywords: List[str] = []
    seen = set()
    for line in raw_text.splitlines():
        keyword = line.strip()
        if keyword and keyword not in seen:
            seen.add(keyword)
            keywords.append(keyword)
    return keywords


def output_path_for(input_path: Path, requested_path: Optional[Path] = None) -> Path:
    """Return a safe, non-existing output path for a default or user selection."""

    base = requested_path or input_path.with_name(f"{input_path.stem}_redacted.pdf")
    if base.suffix.lower() != ".pdf":
        base = base.with_suffix(".pdf")
    try:
        if base.resolve() == input_path.resolve():
            raise ValueError("输出文件不能覆盖原始 PDF，请选择其他文件名或位置。")
    except OSError:
        pass
    if not base.exists():
        return base
    number = 1
    while True:
        candidate = base.with_name(f"{base.stem}_{number}{base.suffix}")
        if not candidate.exists():
            return candidate
        number += 1


def _line_characters(page: fitz.Page) -> Iterable[Tuple[str, List[Tuple[str, Rect]]]]:
    """Yield character strings and their rectangles, line by line.

    ``rawdict`` exposes character positions.  Matching it directly gives us
    strict case-sensitive, whitespace-sensitive semantics rather than the
    case-folding behavior of some PDF search implementations.
    """

    raw = page.get_text("rawdict")
    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            characters: List[Tuple[str, Rect]] = []
            for span in line.get("spans", []):
                for char in span.get("chars", []):
                    value = char.get("c", "")
                    bbox = char.get("bbox")
                    if isinstance(value, str) and bbox and len(bbox) == 4:
                        characters.append((value, Rect(bbox)))
            if characters:
                yield "".join(value for value, _ in characters), characters


def _native_fragment_rect(characters: Sequence[Tuple[str, Rect]]) -> Optional[Rect]:
    """Merge the character boxes belonging to one visible text fragment."""

    if not characters:
        return None
    merged = Rect(characters[0][1])
    for _character, char_rect in characters[1:]:
        merged |= char_rect
    return merged


def strict_native_match_groups(page: fitz.Page, keyword: str) -> List[List[Rect]]:
    """Find literal native-text matches, including a split over adjacent rows.

    One occurrence can contain two rectangles when its text wraps from one row
    to the immediate next row.  Keeping those rectangles separate prevents a
    large, unsafe mask spanning the whitespace between the two line fragments.
    """

    if not keyword:
        return []
    lines = sorted(
        list(_line_characters(page)),
        key=lambda item: (_native_fragment_rect(item[1]).y0, _native_fragment_rect(item[1]).x0),
    )
    line_bounds = [_native_fragment_rect(characters) for _text, characters in lines]
    groups: List[List[Rect]] = []
    for text, characters in lines:
        start = 0
        while True:
            position = text.find(keyword, start)
            if position < 0:
                break
            fragment = _native_fragment_rect(characters[position : position + len(keyword)])
            if fragment:
                groups.append([fragment])
            start = position + len(keyword)

    # Join only the immediate lower row, using the same adjacency rule as OCR.
    # This covers selectable PDF text in both comprehensive and fast modes.
    for index in range(len(lines)):
        first_text, first_characters = lines[index]
        first_bounds = line_bounds[index]
        if not first_bounds:
            continue
        for next_index in _next_visual_row_indices(line_bounds, index):
            next_text, next_characters = lines[next_index]
            next_bounds = line_bounds[next_index]
            if not next_bounds or not _bounds_are_adjacent(first_bounds, next_bounds):
                continue
            combined = first_text + next_text
            start = 0
            while True:
                position = combined.find(keyword, start)
                if position < 0:
                    break
                end = position + len(keyword)
                if position < len(first_text) and end > len(first_text):
                    first_fragment = _native_fragment_rect(first_characters[position:])
                    next_fragment = _native_fragment_rect(next_characters[: end - len(first_text)])
                    if first_fragment and next_fragment:
                        groups.append([first_fragment, next_fragment])
                start = position + len(keyword)
    return groups


def strict_native_matches(page: fitz.Page, keyword: str) -> List[Rect]:
    """Find literal native-text masks, including masks split over adjacent rows."""

    return [rect for group in strict_native_match_groups(page, keyword) for rect in group]


def _vector_length(vector: Tuple[float, float]) -> float:
    return (vector[0] ** 2 + vector[1] ** 2) ** 0.5


def _unit(vector: Tuple[float, float]) -> Tuple[float, float]:
    length = _vector_length(vector)
    return (vector[0] / length, vector[1] / length) if length else (0.0, 0.0)


def _add(point: Tuple[float, float], *vectors: Tuple[float, float]) -> Tuple[float, float]:
    return (point[0] + sum(vector[0] for vector in vectors), point[1] + sum(vector[1] for vector in vectors))


def _scale(vector: Tuple[float, float], factor: float) -> Tuple[float, float]:
    return (vector[0] * factor, vector[1] * factor)


def _character_weight(character: str) -> float:
    if unicodedata.combining(character):
        return 0.0
    return 2.0 if unicodedata.east_asian_width(character) in {"W", "F"} else 1.0


def _estimate_ocr_target(
    line: OCRLine, start: int, end: int, keyword: str, occurrence_id: str
) -> OCRTarget:
    """Estimate a quadrilateral for a known character slice in an OCR line."""

    weights = [_character_weight(char) for char in line.text]
    total = sum(weights)
    if total <= 0:
        raise ValueError("OCR line has no measurable character width")
    top_left, top_right, _bottom_right, bottom_left = line.quad
    horizontal = (top_right[0] - top_left[0], top_right[1] - top_left[1])
    vertical = (bottom_left[0] - top_left[0], bottom_left[1] - top_left[1])
    average_advance = _vector_length(horizontal) / total
    horizontal_padding = max(6.0, average_advance * 0.5)
    vertical_padding = _vector_length(vertical) * 0.15
    horizontal_unit = _unit(horizontal)
    vertical_unit = _unit(vertical)
    before = sum(weights[:start]) / total
    through = sum(weights[:end]) / total
    origin = _add(top_left, _scale(horizontal, before))
    end_origin = _add(top_left, _scale(horizontal, through))
    top_left_target = _add(
        origin, _scale(horizontal_unit, -horizontal_padding), _scale(vertical_unit, -vertical_padding)
    )
    top_right_target = _add(
        end_origin, _scale(horizontal_unit, horizontal_padding), _scale(vertical_unit, -vertical_padding)
    )
    bottom_right_target = _add(
        end_origin,
        vertical,
        _scale(horizontal_unit, horizontal_padding),
        _scale(vertical_unit, vertical_padding),
    )
    bottom_left_target = _add(
        origin,
        vertical,
        _scale(horizontal_unit, -horizontal_padding),
        _scale(vertical_unit, vertical_padding),
    )
    return OCRTarget(
        keyword=keyword,
        quad=(top_left_target, top_right_target, bottom_right_target, bottom_left_target),
        source_line=line.text,
        occurrence_id=occurrence_id,
    )


def estimate_ocr_targets(line: OCRLine, keyword: str, line_id: str = "line") -> List[OCRTarget]:
    """Estimate substring boxes from a line-level OCR quadrilateral.

    PaddleOCR reports a box for a full line, not each character.  The chosen
    product policy is a conservative character-width estimate with padding;
    callers must flag these results for manual visual review.
    """

    if not keyword or len(line.quad) != 4:
        return []
    targets: List[OCRTarget] = []

    start = 0
    while True:
        position = line.text.find(keyword, start)
        if position < 0:
            break
        end = position + len(keyword)
        try:
            targets.append(_estimate_ocr_target(line, position, end, keyword, f"{line_id}:{position}"))
        except ValueError:
            return targets
        start = end
    return targets


def _line_bounds(line: OCRLine) -> Rect:
    return quad_bounds(line.quad)


def _bounds_are_adjacent(previous_bounds: Rect, following_bounds: Rect) -> bool:
    """Whether two rectangles occupy immediately adjacent text rows."""

    if following_bounds.y0 < previous_bounds.y0:
        return False
    line_height = max(previous_bounds.height, following_bounds.height, 1.0)
    vertical_gap = following_bounds.y0 - previous_bounds.y1
    return -line_height * 0.50 <= vertical_gap <= line_height * 1.50


def _next_visual_row_indices(bounds: Sequence[Rect], current_index: int) -> List[int]:
    """Return text records on the immediate lower visual row.

    PDF text and OCR results can contain side labels or fragmented records that
    sort between two wrapped body rows.  Selecting by the next visual row, not
    by the next list item, preserves a genuine line wrap such as ``幕`` +
    ``墙工程``.
    """

    current = bounds[current_index]
    current_height = max(current.height, 1.0)
    same_row_tolerance = current_height * 0.55
    lower_indices = [
        index
        for index, candidate in enumerate(bounds)
        if candidate.y0 > current.y0 + same_row_tolerance
    ]
    if not lower_indices:
        return []
    nearest_y = min(bounds[index].y0 for index in lower_indices)
    row_height = max([current_height] + [bounds[index].height for index in lower_indices])
    row_tolerance = max(1.0, row_height * 0.55)
    return [index for index in lower_indices if abs(bounds[index].y0 - nearest_y) <= row_tolerance]


def _likely_wrapped_line(previous: OCRLine, following: OCRLine) -> bool:
    """Whether two OCR lines are vertically adjacent rows."""

    return _bounds_are_adjacent(_line_bounds(previous), _line_bounds(following))


def find_ocr_targets(lines: Sequence[OCRLine], keyword: str) -> List[OCRTarget]:
    """Find literal OCR matches within a line and across up to four wrapped lines."""

    ordered = sorted(lines, key=lambda line: (_line_bounds(line).y0, _line_bounds(line).x0))
    line_bounds = [_line_bounds(line) for line in ordered]
    targets: List[OCRTarget] = []
    for index, line in enumerate(ordered):
        targets.extend(estimate_ocr_targets(line, keyword, f"line:{index}"))

    # OCR often returns a long printed phrase as several physical lines. For a
    # match that crosses a line boundary, create one mask for each participating
    # fragment but share an occurrence ID so logs count it as one match.
    for first_index in range(len(ordered)):
        # Combine only the immediate lower visual row. A raw list can include
        # an unrelated label or OCR fragment between the two body rows.
        for last_index in _next_visual_row_indices(line_bounds, first_index):
            previous = ordered[first_index]
            current = ordered[last_index]
            if not _likely_wrapped_line(previous, current):
                continue
            combined = previous.text + current.text
            start = 0
            while True:
                position = combined.find(keyword, start)
                if position < 0:
                    break
                end = position + len(keyword)
                # Record a match only once: it must start in the first line of
                # this run and end in the newly appended final line.
                if position < len(previous.text) and end > len(previous.text):
                    occurrence_id = f"cross:{first_index}:{last_index}:{position}"
                    first_end = min(end, len(previous.text))
                    targets.append(
                        _estimate_ocr_target(previous, position, first_end, keyword, occurrence_id)
                    )
                    targets.append(
                        _estimate_ocr_target(
                            current,
                            0,
                            end - len(previous.text),
                            keyword,
                            occurrence_id,
                        )
                    )
                start = position + len(keyword)
    return targets


def quad_bounds(quad: Quad) -> Rect:
    return Rect(min(point[0] for point in quad), min(point[1] for point in quad), max(point[0] for point in quad), max(point[1] for point in quad))


def _rect_iou(left: Rect, right: Rect) -> float:
    overlap = left & right
    if overlap.is_empty or overlap.get_area() <= 0:
        return 0.0
    union = left.get_area() + right.get_area() - overlap.get_area()
    return overlap.get_area() / union if union else 0.0


def _rects_correspond(ocr_rect: Rect, native_rect: Rect) -> bool:
    # OCR positioning is approximate.  A modest IoU covers different font
    # metrics while avoiding mistaking a neighbouring image line for native text.
    return _rect_iou(ocr_rect, native_rect) >= 0.30


def _unique_targets(targets: Sequence[OCRTarget]) -> List[OCRTarget]:
    unique: List[OCRTarget] = []
    for target in targets:
        bounds = quad_bounds(target.quad)
        if not any(_rect_iou(bounds, quad_bounds(previous.quad)) >= 0.85 for previous in unique):
            unique.append(target)
    return unique


def _merge_character_rects(characters: Sequence[Tuple[str, Rect]]) -> Optional[Rect]:
    if not characters:
        return None
    merged = Rect(characters[0][1])
    for _character, rect in characters[1:]:
        merged |= rect
    return merged


def _cross_page_native_parts(previous_page: fitz.Page, next_page: fitz.Page, keyword: str) -> Optional[Tuple[Rect, Rect]]:
    """Return rects when a literal keyword straddles the two page edge lines."""

    previous_lines = list(_line_characters(previous_page))
    next_lines = list(_line_characters(next_page))
    if not previous_lines or not next_lines:
        return None
    previous_text, previous_chars = previous_lines[-1]
    next_text, next_chars = next_lines[0]
    boundary = len(previous_text)
    combined = previous_text + next_text
    start = combined.find(keyword)
    if start < 0 or start >= boundary or start + len(keyword) <= boundary:
        return None
    previous_part = _merge_character_rects(previous_chars[start:])
    next_length = start + len(keyword) - boundary
    next_part = _merge_character_rects(next_chars[:next_length])
    return (previous_part, next_part) if previous_part and next_part else None


def _cross_page_ocr_parts(
    previous_lines: Sequence[OCRLine], next_lines: Sequence[OCRLine], keyword: str, occurrence_id: str
) -> Optional[Tuple[OCRTarget, OCRTarget]]:
    """Estimate two OCR masks when a keyword is split at a page boundary."""

    if not previous_lines or not next_lines:
        return None
    previous = sorted(previous_lines, key=lambda line: (_line_bounds(line).y0, _line_bounds(line).x0))[-1]
    following = sorted(next_lines, key=lambda line: (_line_bounds(line).y0, _line_bounds(line).x0))[0]
    boundary = len(previous.text)
    combined = previous.text + following.text
    start = combined.find(keyword)
    if start < 0 or start >= boundary or start + len(keyword) <= boundary:
        return None
    previous_target = _estimate_ocr_target(previous, start, boundary, keyword, occurrence_id)
    next_target = _estimate_ocr_target(following, 0, start + len(keyword) - boundary, keyword, occurrence_id)
    return previous_target, next_target


class PDFRedactor:
    """Process one PDF entirely on the local machine."""

    DPI = 300
    SCALE = DPI / 72.0

    def __init__(self, ocr_provider: Optional[OCRProvider] = None) -> None:
        self._ocr_provider = ocr_provider

    def redact(self, request: RedactionRequest, status: Optional[StatusCallback] = None) -> RedactionResult:
        """Compatibility one-step workflow used by non-GUI callers."""

        session = self.prepare_review(request, status)
        return self.finalize_review(session, status)

    def prepare_review(self, request: RedactionRequest, status: Optional[StatusCallback] = None) -> ReviewSession:
        notify = status or (lambda _message: None)
        keywords = list(dict.fromkeys(keyword for keyword in request.keywords if keyword))
        if not keywords:
            raise RedactionError("请至少输入一个需要打码的关键词。")
        if not request.input_path.is_file():
            raise RedactionError("找不到所选 PDF 文件。")
        if request.input_path.suffix.lower() != ".pdf":
            raise RedactionError("请选择 PDF 文件。")
        try:
            parse_color(request.color)
        except ValueError as exc:
            raise RedactionError(str(exc)) from exc
        if not isinstance(request.processing_mode, ProcessingMode):
            raise RedactionError("处理模式无效。")

        document, was_encrypted = self._open_document(request)
        try:
            notify(f"已打开文件，共 {document.page_count} 页。正在初始化离线 OCR……")
            ocr = self._ocr_provider or PaddleOCRProvider()
            plans, keyword_counts, page_matches, cross_page_continuations = self._analyse_document(
                document, keywords, ocr, notify, request.manual_rects, request.processing_mode
            )
            return ReviewSession(
                request=request,
                plans=plans,
                keyword_counts=keyword_counts,
                page_rects=[tuple(page.rect) for page in document],
                page_matches=page_matches,
                cross_page_continuations=cross_page_continuations,
            )
        finally:
            document.close()

    def finalize_review(self, session: ReviewSession, status: Optional[StatusCallback] = None) -> RedactionResult:
        """Write the final PDF using the user-reviewed, editable plan."""

        notify = status or (lambda _message: None)
        request = session.request
        keywords = list(dict.fromkeys(keyword for keyword in request.keywords if keyword))
        document, was_encrypted = self._open_document(request)
        temporary_path: Optional[Path] = None
        try:
            try:
                output_path = output_path_for(request.input_path, request.output_path)
            except ValueError as exc:
                raise RedactionError(str(exc)) from exc
            if not output_path.parent.is_dir():
                raise RedactionError("输出文件夹不存在，请选择一个已存在的位置。")
            notify("已确认遮盖预览，正在生成最终 PDF……")
            output = self._build_output(document, session.plans, request, notify)
            try:
                temporary_path = output_path.with_name(f".{output_path.stem}.{uuid.uuid4().hex}.tmp.pdf")
                self._save_output(output, temporary_path, request.password if was_encrypted else None)
            finally:
                output.close()
            os.replace(temporary_path, output_path)
            temporary_path = None
            notify("正在重新打开输出文件进行全文搜索校验……")
            verification_failures = self._verify(output_path, keywords, request.password if was_encrypted else None)
            result = RedactionResult(
                input_path=request.input_path,
                output_path=output_path,
                page_count=document.page_count,
                keyword_counts=session.keyword_counts,
                flattened_pages=[plan.index + 1 for plan in session.plans if plan.needs_flattening],
                estimated_pages=[plan.index + 1 for plan in session.plans if any(plan.visual_targets.values())],
                verification_failures=verification_failures,
                manual_pages=[plan.index + 1 for plan in session.plans if plan.manual_rects],
            )
            self._report_result(result, notify)
            return result
        finally:
            document.close()
            if temporary_path and temporary_path.exists():
                temporary_path.unlink(missing_ok=True)

    def _open_document(self, request: RedactionRequest) -> Tuple[fitz.Document, bool]:
        try:
            document = fitz.open(str(request.input_path))
        except Exception as exc:
            raise RedactionError(f"无法打开 PDF，文件可能损坏或格式不受支持：{exc}") from exc

        was_encrypted = bool(document.needs_pass)
        if was_encrypted:
            if not request.password:
                document.close()
                raise RedactionError("该 PDF 受密码保护，请输入打开密码。")
            try:
                authenticated = document.authenticate(request.password)
            except Exception as exc:
                document.close()
                raise RedactionError("无法验证 PDF 密码。") from exc
            if not authenticated:
                document.close()
                raise RedactionError("PDF 密码不正确。")
        return document, was_encrypted

    def _analyse_document(
        self,
        document: fitz.Document,
        keywords: Sequence[str],
        ocr: OCRProvider,
        notify: StatusCallback,
        manual_rects: Optional[Dict[int, List[Tuple[float, float, float, float]]]] = None,
        processing_mode: ProcessingMode = ProcessingMode.COMPREHENSIVE,
    ) -> Tuple[List[PagePlan], Dict[str, int], Dict[int, List[str]], Dict[int, List[Tuple[str, int]]]]:
        plans: List[PagePlan] = []
        counts = {keyword: 0 for keyword in keywords}
        page_matches: Dict[int, List[str]] = {}
        cross_page_continuations: Dict[int, List[Tuple[str, int]]] = {}
        ocr_lines_by_page: List[List[OCRLine]] = []
        image_rects_by_page: List[List[Rect]] = []
        for index, page in enumerate(document):
            notify(f"正在分析第 {index + 1}/{document.page_count} 页（文字层 + 离线 OCR）……")
            native_match_groups = {keyword: strict_native_match_groups(page, keyword) for keyword in keywords}
            native_rects = {
                keyword: [rect for group in native_match_groups[keyword] for rect in group]
                for keyword in keywords
            }
            image_rects = self._embedded_image_rects(page)
            image_rects_by_page.append(image_rects)
            selected_rects: List[Rect] = []
            for values in (manual_rects or {}).get(index, []):
                try:
                    rect = Rect(values)
                    if rect.get_area() > 0:
                        selected_rects.append(rect)
                except (TypeError, ValueError):
                    continue
            has_text_layer = bool(page.get_text("text").strip())
            should_ocr = processing_mode is ProcessingMode.COMPREHENSIVE or bool(image_rects) or not has_text_layer
            if should_ocr:
                page_image = self._render_page(page)
                lines = ocr.recognize(page_image)
            else:
                notify(f"第 {index + 1} 页仅含可搜索文字层：快速模式跳过 OCR。")
                lines = []
            ocr_lines_by_page.append(lines)
            visual_targets: Dict[str, List[OCRTarget]] = {}
            for keyword in keywords:
                unique = _unique_targets(find_ocr_targets(lines, keyword))
                targets_without_native = set()
                masks: List[OCRTarget] = []
                for target in unique:
                    target_rect = self._pixel_rect_to_page(target, page)
                    has_native_match = any(
                        _rects_correspond(target_rect, native) for native in native_rects[keyword]
                    )
                    # A scanned page may contain a hidden OCR text layer. In
                    # that case deleting the text object alone leaves the same
                    # characters in the full-page image. OCR hits that sit on
                    # an embedded image must therefore be painted into a
                    # rebuilt raster page even when native text also matches.
                    is_on_image = any(self._rect_overlaps_image(target_rect, image) for image in image_rects)
                    if is_on_image or not has_native_match:
                        masks.append(target)
                    if not has_native_match:
                        targets_without_native.add(target.occurrence_id)
                visual_targets[keyword] = masks
                counts[keyword] += len(native_match_groups[keyword]) + len(targets_without_native)
                if native_rects[keyword] or masks:
                    page_matches.setdefault(index, []).append(keyword)
            plans.append(
                PagePlan(index=index, native_rects=native_rects, visual_targets=visual_targets, manual_rects=selected_rects)
            )
        self._add_cross_page_matches(
            document,
            plans,
            keywords,
            ocr_lines_by_page,
            image_rects_by_page,
            counts,
            page_matches,
            cross_page_continuations,
        )
        return plans, counts, page_matches, cross_page_continuations

    def _add_cross_page_matches(
        self,
        document: fitz.Document,
        plans: Sequence[PagePlan],
        keywords: Sequence[str],
        ocr_lines_by_page: Sequence[Sequence[OCRLine]],
        image_rects_by_page: Sequence[Sequence[Rect]],
        counts: Dict[str, int],
        page_matches: Dict[int, List[str]],
        cross_page_continuations: Dict[int, List[Tuple[str, int]]],
    ) -> None:
        """Add one logical match for keywords divided exactly at a page edge."""

        for page_index in range(document.page_count - 1):
            previous_page = document[page_index]
            next_page = document[page_index + 1]
            for keyword in keywords:
                native_parts = _cross_page_native_parts(previous_page, next_page, keyword)
                ocr_parts = _cross_page_ocr_parts(
                    ocr_lines_by_page[page_index],
                    ocr_lines_by_page[page_index + 1],
                    keyword,
                    f"pagecross:{page_index}:{keyword}",
                )
                if not native_parts and not ocr_parts:
                    continue

                if native_parts:
                    plans[page_index].native_rects[keyword].append(native_parts[0])
                    plans[page_index + 1].native_rects[keyword].append(native_parts[1])

                if ocr_parts:
                    previous_rect = self._pixel_rect_to_page(ocr_parts[0], previous_page)
                    next_rect = self._pixel_rect_to_page(ocr_parts[1], next_page)
                    previous_needs_raster = (not native_parts) or any(
                        self._rect_overlaps_image(previous_rect, image) for image in image_rects_by_page[page_index]
                    )
                    next_needs_raster = (not native_parts) or any(
                        self._rect_overlaps_image(next_rect, image) for image in image_rects_by_page[page_index + 1]
                    )
                    if previous_needs_raster:
                        plans[page_index].visual_targets[keyword].append(ocr_parts[0])
                    if next_needs_raster:
                        plans[page_index + 1].visual_targets[keyword].append(ocr_parts[1])

                # The two page fragments are one occurrence from the user's
                # perspective, regardless of whether it came from text, OCR,
                # or a scanned page with both layers.
                counts[keyword] += 1
                if keyword not in page_matches.setdefault(page_index, []):
                    page_matches[page_index].append(keyword)
                cross_page_continuations.setdefault(page_index, []).append((keyword, page_index + 2))

    @staticmethod
    def _embedded_image_rects(page: fitz.Page) -> List[Rect]:
        rectangles: List[Rect] = []
        for image in page.get_images(full=True):
            try:
                rectangles.extend(page.get_image_rects(image[0]))
            except Exception:
                continue
        return rectangles

    @staticmethod
    def _rect_overlaps_image(target: Rect, image: Rect) -> bool:
        overlap = target & image
        if overlap.is_empty or overlap.get_area() <= 0 or target.get_area() <= 0:
            return False
        return overlap.get_area() / target.get_area() >= 0.50

    def _render_page(self, page: fitz.Page) -> Image.Image:
        pixmap = page.get_pixmap(matrix=fitz.Matrix(self.SCALE, self.SCALE), alpha=False)
        mode = "RGB" if pixmap.n == 3 else "RGBA"
        return Image.frombytes(mode, (pixmap.width, pixmap.height), pixmap.samples).convert("RGB")

    def _pixel_rect_to_page(self, target: OCRTarget, page: fitz.Page) -> Rect:
        bounds = quad_bounds(target.quad)
        return Rect(
            page.rect.x0 + bounds.x0 / self.SCALE,
            page.rect.y0 + bounds.y0 / self.SCALE,
            page.rect.x0 + bounds.x1 / self.SCALE,
            page.rect.y0 + bounds.y1 / self.SCALE,
        )

    def _page_rect_to_pixel_quad(self, rect: Rect, page: fitz.Page) -> Quad:
        x0 = (rect.x0 - page.rect.x0) * self.SCALE
        y0 = (rect.y0 - page.rect.y0) * self.SCALE
        x1 = (rect.x1 - page.rect.x0) * self.SCALE
        y1 = (rect.y1 - page.rect.y0) * self.SCALE
        return ((x0, y0), (x1, y0), (x1, y1), (x0, y1))

    def _build_output(
        self, document: fitz.Document, plans: Sequence[PagePlan], request: RedactionRequest, notify: StatusCallback
    ) -> fitz.Document:
        # Apply native redactions first.  The actual redaction API removes text
        # objects; drawing a rectangle alone would be insufficient.
        for plan in plans:
            if plan.needs_flattening:
                continue
            page = document[plan.index]
            rectangles = [rect for rects in plan.native_rects.values() for rect in rects]
            if rectangles:
                for rect in rectangles:
                    page.add_redact_annot(rect, fill=color_to_pdf_rgb(request.color), cross_out=False)
                page.apply_redactions()

        output = fitz.open()
        for plan in plans:
            page = document[plan.index]
            if plan.needs_flattening:
                notify(f"第 {plan.index + 1} 页包含图像 OCR 命中，正在安全重建为位图页。")
                image = self._render_page(page)
                painter = ImageDraw.Draw(image)
                for rects in plan.native_rects.values():
                    for rect in rects:
                        painter.polygon(self._page_rect_to_pixel_quad(rect, page), fill=parse_color(request.color))
                for targets in plan.visual_targets.values():
                    for target in targets:
                        painter.polygon(target.quad, fill=parse_color(request.color))
                for manual_rect in plan.manual_rects:
                    painter.polygon(self._page_rect_to_pixel_quad(manual_rect, page), fill=parse_color(request.color))
                new_page = output.new_page(width=page.rect.width, height=page.rect.height)
                new_page.insert_image(new_page.rect, stream=self._image_to_png(image))
            else:
                # A new document avoids copying document-level metadata,
                # embedded files and scripts from the input file.
                output.insert_pdf(document, from_page=plan.index, to_page=plan.index, links=0, annots=0)

        self._strip_page_annotations(output)
        self._strip_document_metadata(output)
        return output

    @staticmethod
    def _image_to_png(image: Image.Image) -> bytes:
        from io import BytesIO

        buffer = BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()

    @staticmethod
    def _strip_page_annotations(document: fitz.Document) -> None:
        for page in document:
            try:
                for link in page.get_links():
                    page.delete_link(link)
            except Exception:
                pass
            try:
                while page.first_annot:
                    page.delete_annot(page.first_annot)
            except Exception:
                pass
            try:
                for widget in list(page.widgets() or []):
                    page.delete_widget(widget)
            except Exception:
                pass

    @staticmethod
    def _strip_document_metadata(document: fitz.Document) -> None:
        try:
            document.set_metadata({})
        except Exception:
            metadata = {key: "" for key in document.metadata if key != "format"}
            document.set_metadata(metadata)
        try:
            document.set_xml_metadata("")
        except Exception:
            pass

    @staticmethod
    def _save_output(document: fitz.Document, path: Path, password: Optional[str]) -> None:
        options = {"garbage": 4, "deflate": True, "clean": True}
        if password:
            permission_names = (
                "PDF_PERM_ACCESSIBILITY",
                "PDF_PERM_PRINT",
                "PDF_PERM_COPY",
                "PDF_PERM_ANNOTATE",
                "PDF_PERM_FORM",
                "PDF_PERM_ASSEMBLE",
                "PDF_PERM_MODIFY",
            )
            permissions = 0
            for name in permission_names:
                permissions |= getattr(fitz, name, 0)
            options.update(
                {
                    "encryption": fitz.PDF_ENCRYPT_AES_256,
                    "user_pw": password,
                    # PyMuPDF limits each PDF password to 40 characters.
                    "owner_pw": secrets.token_hex(20),
                    "permissions": permissions,
                }
            )
        try:
            document.save(str(path), **options)
        except Exception as exc:
            raise RedactionError(f"无法保存输出文件：{exc}") from exc

    def _verify(self, output_path: Path, keywords: Sequence[str], password: Optional[str]) -> List[str]:
        try:
            document = fitz.open(str(output_path))
        except Exception as exc:
            raise RedactionError(f"输出文件无法重新打开，已保留以供排查：{exc}") from exc
        try:
            if document.needs_pass and not document.authenticate(password or ""):
                raise RedactionError("输出文件密码校验失败，已保留以供排查。")
            failures: List[str] = []
            for keyword in keywords:
                # Use the exact same case- and whitespace-sensitive matcher as
                # processing. PyMuPDF's built-in search case-folds ASCII, which
                # would incorrectly report an untouched "secret" for an input
                # keyword of "Secret".
                if any(strict_native_matches(page, keyword) for page in document):
                    failures.append(keyword)
            return failures
        finally:
            document.close()

    @staticmethod
    def _report_result(result: RedactionResult, notify: StatusCallback) -> None:
        notify(f"处理完成：共处理 {result.page_count} 页。")
        for keyword, count in result.keyword_counts.items():
            notify(f"关键词“{keyword}”：找到 {count} 处。")
        missing = [keyword for keyword, count in result.keyword_counts.items() if count == 0]
        if missing:
            notify("未找到关键词：" + "、".join(f"“{keyword}”" for keyword in missing))
        if result.flattened_pages:
            pages = "、".join(str(page) for page in result.flattened_pages)
            notify(f"已安全重建扫描/图像页：{pages}。这些页面采用 OCR 位置估算，请人工目视复核。")
        if result.manual_pages:
            notify("已应用手动框选遮盖页：" + "、".join(str(page) for page in result.manual_pages) + "。")
        notify(f"输出文件：{result.output_path}")
        if result.verification_failures:
            notify("安全校验失败：输出 PDF 仍可搜索到 " + "、".join(f"“{item}”" for item in result.verification_failures))
        else:
            notify("安全校验通过：输出 PDF 的文本层中无法再搜索到所给关键词。")
