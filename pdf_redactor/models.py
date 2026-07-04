"""Data types shared by the GUI, OCR adapter and processing pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union


Point = Tuple[float, float]
Quad = Tuple[Point, Point, Point, Point]


class RedactionColor(str, Enum):
    BLACK = "black"
    WHITE = "white"

    @property
    def rgb(self) -> Tuple[int, int, int]:
        return (0, 0, 0) if self is RedactionColor.BLACK else (255, 255, 255)

    @property
    def pdf_rgb(self) -> Tuple[float, float, float]:
        return (0.0, 0.0, 0.0) if self is RedactionColor.BLACK else (1.0, 1.0, 1.0)


class ProcessingMode(str, Enum):
    FAST = "fast"
    COMPREHENSIVE = "comprehensive"


ColorValue = Union[RedactionColor, str]


def parse_color(value: ColorValue) -> Tuple[int, int, int]:
    """Convert a legacy black/white option or a #RRGGBB value to RGB."""

    if isinstance(value, RedactionColor):
        return value.rgb
    text = value.strip()
    if len(text) != 7 or not text.startswith("#"):
        raise ValueError("颜色必须是 #RRGGBB 格式，例如 #4F81BD。")
    try:
        return tuple(int(text[index : index + 2], 16) for index in (1, 3, 5))  # type: ignore[return-value]
    except ValueError as exc:
        raise ValueError("颜色必须是 #RRGGBB 格式，例如 #4F81BD。") from exc


def color_to_pdf_rgb(value: ColorValue) -> Tuple[float, float, float]:
    red, green, blue = parse_color(value)
    return (red / 255.0, green / 255.0, blue / 255.0)


@dataclass(frozen=True)
class OCRLine:
    """A recognized text line and its four pixel-space corners."""

    text: str
    confidence: float
    quad: Quad


@dataclass(frozen=True)
class OCRTarget:
    """An estimated pixel-space quadrilateral for one matching OCR substring."""

    keyword: str
    quad: Quad
    source_line: str
    occurrence_id: str = ""


@dataclass(frozen=True)
class RedactionRequest:
    input_path: Path
    keywords: List[str]
    color: ColorValue
    password: Optional[str] = None
    output_path: Optional[Path] = None
    manual_rects: Dict[int, List[Tuple[float, float, float, float]]] = field(default_factory=dict)
    processing_mode: ProcessingMode = ProcessingMode.COMPREHENSIVE


@dataclass
class RedactionResult:
    input_path: Path
    output_path: Path
    page_count: int
    keyword_counts: Dict[str, int]
    flattened_pages: List[int] = field(default_factory=list)
    verification_failures: List[str] = field(default_factory=list)
    estimated_pages: List[int] = field(default_factory=list)
    manual_pages: List[int] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return not self.verification_failures
