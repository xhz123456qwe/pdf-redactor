"""Download PaddleOCR model files once for an offline Windows build.

This build-time helper deliberately does not run from the desktop application.
"""

from __future__ import annotations

import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DESTINATION = ROOT / "assets" / "paddleocr"
MODELS = {
    "ch_PP-OCRv4_det_infer": "https://paddleocr.bj.bcebos.com/PP-OCRv4/chinese/ch_PP-OCRv4_det_infer.tar",
    "ch_PP-OCRv4_rec_infer": "https://paddleocr.bj.bcebos.com/PP-OCRv4/chinese/ch_PP-OCRv4_rec_infer.tar",
    "ch_ppocr_mobile_v2.0_cls_infer": "https://paddleocr.bj.bcebos.com/dygraph_v2.0/ch/ch_ppocr_mobile_v2.0_cls_infer.tar",
}


def extract_model(name: str, url: str) -> None:
    target = DESTINATION / name
    if (target / "inference.pdiparams").is_file():
        print(f"已存在：{name}")
        return

    DESTINATION.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pdf_redactor_models_") as temp_dir:
        archive = Path(temp_dir) / f"{name}.tar"
        print(f"下载：{name}")
        urllib.request.urlretrieve(url, archive)  # noqa: S310 - fixed PaddleOCR model URLs
        with tarfile.open(archive) as package:
            members = package.getmembers()
            root = Path(name)
            for member in members:
                member_path = Path(member.name)
                if member_path.is_absolute() or ".." in member_path.parts:
                    raise RuntimeError(f"模型压缩包包含不安全路径：{member.name}")
            package.extractall(temp_dir)
        extracted = Path(temp_dir) / root
        if not extracted.is_dir():
            raise RuntimeError(f"模型压缩包结构异常：{name}")
        if target.exists():
            shutil.rmtree(target)
        shutil.move(str(extracted), str(target))
        print(f"完成：{target}")


def main() -> None:
    for name, url in MODELS.items():
        extract_model(name, url)


if __name__ == "__main__":
    main()
