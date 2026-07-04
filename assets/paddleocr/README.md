# PaddleOCR 离线模型目录

此目录在提交源码时只保留说明文件，避免将数百 MB 的二进制模型误提交到版本库。

在打包前执行：

```powershell
py -3.11 scripts/prepare_models.py
```

该脚本会下载并解压下列模型到本目录，`build.ps1` 会将它们嵌入 EXE：

- `ch_PP-OCRv4_det_infer`
- `ch_PP-OCRv4_rec_infer`
- `ch_ppocr_mobile_v2.0_cls_infer`

成品 EXE 运行时只读取内置模型，不会联网下载模型。
