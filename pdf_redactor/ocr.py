"""Offline PaddleOCR adapter.

The application never asks PaddleOCR to resolve a model name.  Explicit local
model paths are passed instead, so a missing model is an actionable error
rather than an unexpected network request at a user's desk.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional, Protocol, Sequence

import numpy as np
from PIL import Image

from .models import OCRLine
from .resources import paddle_model_root, paddle_model_runtime_root, paddle_runtime_home


class OCRUnavailableError(RuntimeError):
    """Raised when packaged OCR dependencies or model files are unavailable."""


class OCRProvider(Protocol):
    def recognize(self, image: Image.Image) -> List[OCRLine]:
        """Recognize text from an RGB page image without performing network I/O."""


class PaddleOCRProvider:
    """PaddleOCR configured for bundled Chinese detection and recognition models."""

    MIN_CONFIDENCE = 0.50

    def __init__(self, model_root: Optional[Path] = None) -> None:
        root = model_root or paddle_model_root()
        source_paths = {
            "det_model_dir": root / "ch_PP-OCRv4_det_infer",
            "rec_model_dir": root / "ch_PP-OCRv4_rec_infer",
            "cls_model_dir": root / "ch_ppocr_mobile_v2.0_cls_infer",
        }
        missing = [str(path) for path in source_paths.values() if not (path / "inference.pdiparams").is_file()]
        if missing:
            raise OCRUnavailableError(
                "离线 OCR 模型缺失。请让发布者重新构建程序，或在源码目录执行 "
                "py -3.11 scripts/prepare_models.py。"
            )

        try:
            runtime_root = paddle_model_runtime_root()
        except RuntimeError as exc:
            raise OCRUnavailableError(str(exc)) from exc
        paths = {
            "det_model_dir": runtime_root / "ch_PP-OCRv4_det_infer",
            "rec_model_dir": runtime_root / "ch_PP-OCRv4_rec_infer",
            "cls_model_dir": runtime_root / "ch_ppocr_mobile_v2.0_cls_infer",
        }

        try:
            # Some Windows installations prohibit writes below the user profile
            # cache. Paddle otherwise creates ~/.cache/paddle during import.
            os.environ.setdefault("PADDLE_HOME", str(paddle_runtime_home()))
            from paddleocr import PaddleOCR
        except ImportError as exc:  # pragma: no cover - depends on local optional installation
            raise OCRUnavailableError("OCR 运行库未安装。请按 requirements.txt 安装依赖。") from exc

        try:
            self._engine = PaddleOCR(
                use_angle_cls=True,
                lang="ch",
                use_gpu=False,
                show_log=False,
                **{key: str(value) for key, value in paths.items()},
            )
        except Exception as exc:  # pragma: no cover - hardware/package dependent
            raise OCRUnavailableError(f"离线 OCR 初始化失败：{exc}") from exc

    def recognize(self, image: Image.Image) -> List[OCRLine]:
        try:
            raw_result = self._engine.ocr(np.asarray(image.convert("RGB")), cls=True)
        except Exception as exc:  # pragma: no cover - Paddle runtime dependent
            raise OCRUnavailableError(f"OCR 识别失败：{exc}") from exc

        lines: List[OCRLine] = []
        for item in self._iter_items(raw_result):
            try:
                points, recognition = item[0], item[1]
                text, confidence = recognition[0], float(recognition[1])
                if not isinstance(text, str) or confidence < self.MIN_CONFIDENCE or len(points) != 4:
                    continue
                quad = tuple((float(point[0]), float(point[1])) for point in points)
                lines.append(OCRLine(text=text, confidence=confidence, quad=quad))
            except (IndexError, TypeError, ValueError):
                # One malformed engine result should not discard a whole PDF.
                continue
        return lines

    @staticmethod
    def _is_item(value: object) -> bool:
        if not isinstance(value, (list, tuple)) or len(value) < 2:
            return False
        points, recognition = value[0], value[1]
        if not isinstance(points, (list, tuple, np.ndarray)) or len(points) != 4:
            return False
        if not isinstance(recognition, (list, tuple)) or len(recognition) < 2:
            return False
        # PaddleOCR 2.x returns: [four-point box, (recognized text, score)].
        # A page result is also a list, so checking the text slot is essential
        # to avoid mistaking its outer list for one recognition item.
        return isinstance(recognition[0], str)

    @classmethod
    def _iter_items(cls, result: object) -> Sequence[object]:
        if not isinstance(result, (list, tuple)):
            return []
        if result and cls._is_item(result[0]):
            return result
        items: List[object] = []
        for page_result in result:
            if isinstance(page_result, (list, tuple)):
                items.extend(page_result)
        return items
