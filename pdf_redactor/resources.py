"""Locate project resources both from source and from a PyInstaller bundle."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Optional


def application_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parents[1]


def paddle_model_root() -> Path:
    return application_root() / "assets" / "paddleocr"


def paddle_runtime_home() -> Path:
    """Return a writable ASCII Paddle cache location outside a bundled executable."""

    location = paddle_model_runtime_root().parent / "paddle_cache"
    location.mkdir(parents=True, exist_ok=True)
    return location


def paddle_model_runtime_root() -> Path:
    """Stage bundled models in an ASCII Windows path for Paddle's native DLL.

    PaddlePaddle 2.x can fail to open otherwise valid model files when their
    source path contains Chinese characters. Source PDFs remain untouched and
    may use any Unicode path; only the three bundled OCR models are copied.
    """

    source = paddle_model_root()
    model_names = (
        "ch_PP-OCRv4_det_infer",
        "ch_PP-OCRv4_rec_infer",
        "ch_ppocr_mobile_v2.0_cls_infer",
    )
    candidates = (
        Path(r"C:\Users\Public\Documents\PDFRedactorRuntime\models"),
        Path(r"C:\ProgramData\PDFRedactorRuntime\models"),
    )
    last_error: Optional[Exception] = None
    for destination in candidates:
        try:
            destination.mkdir(parents=True, exist_ok=True)
            for name in model_names:
                source_model = source / name
                destination_model = destination / name
                source_marker = source_model / "inference.pdmodel"
                destination_marker = destination_model / "inference.pdmodel"
                if not source_marker.is_file():
                    continue
                if not destination_marker.is_file() or destination_marker.stat().st_size != source_marker.stat().st_size:
                    if destination_model.exists():
                        shutil.rmtree(destination_model)
                    shutil.copytree(source_model, destination_model)
            return destination
        except OSError as exc:
            last_error = exc
    raise RuntimeError(
        "无法在英文路径创建 OCR 运行模型目录。请确认 C:\\Users\\Public\\Documents 可写。"
    ) from last_error
