# PDF 自动打码工具

[![Tests](https://github.com/xhz123456qwe/pdf-redactor/actions/workflows/tests.yml/badge.svg)](https://github.com/xhz123456qwe/pdf-redactor/actions/workflows/tests.yml)

面向 Windows 普通用户的离线 PDF 隐私信息打码工具。它不上传文件、不修改原文件，并为普通文字 PDF 执行真正的 PDF redaction（删除底层文字对象），而非仅绘制遮盖矩形。

扫描件和嵌入图片中的文字通过本地 PaddleOCR 识别。出现这类命中时，程序会将整页重新生成为已打码的位图 PDF 页，因此输出中不包含原始扫描底图。

> 扫描件 OCR 只提供行级坐标。当前产品选择按字符宽度估算关键词区域并外扩遮盖边距，以保留更多相邻内容；完成日志会标示这些页面，必须人工目视复核。

当前版本：**v1.1.0**。版本变化见 [CHANGELOG.md](CHANGELOG.md)。

## 功能

- 单个 PDF 文件处理，支持中文路径、中文文件名和中文关键词。
- 多行固定关键词，按严格连续字面匹配：区分英文大小写，不支持正则，不忽略空白。
- 可设置任意 `#RRGGBB` 打码颜色，默认 `#4F81BD`；界面展示实时色块，并提供系统颜色选择器。
- 内置 PDF 页面预览和上一页/下一页切换；加密文件输入密码后可刷新预览。
- 处理采用“生成遮盖预览 → 确认生成 PDF”两阶段：预览中可点击选中某处遮盖后删除，或在空白处拖动新增遮盖；仅在确认后写出最终文件。
- 输出文件名和保存位置可直接编辑或通过“另存为”选择；若目标名称已存在，会自动追加编号以避免覆盖。
- 每页同时检查 PDF 文字层和本地 OCR，处理混合文字/图片页面。
- 支持 OCR 识别行之间的连续关键词，例如上一行“建”和下一行“筑工程”可匹配“建筑工程”。
- 支持跨页边界匹配：前一页末尾与后一页开头组成关键词时，会在两页分别遮盖对应片段，并按一次命中统计。
- 提供“快速模式”和“全面模式”：快速模式 OCR 含嵌入图片或无文字层的页面，跳过纯文字页；全面模式逐页 OCR。
- 可在预览中鼠标拖动框选任意目标文字或区域；框选区域会以安全位图方式遮盖，不影响页面其他部分。
- 支持用户已获授权的加密 PDF；输出沿用输入的打开密码。
- 自动生成 `原文件名_redacted.pdf`，从不覆盖原文件；若同名文件存在则追加编号。
- 日志报告页数、每个关键词命中数、未命中项、被重建页面、输出路径及搜索校验结果。
- 清理输出中的文档元数据、附件、链接、批注和表单，避免不相关的隐藏内容被带出。

## 使用源码运行

构建环境固定为 **CPython 3.11 x64**。PaddlePaddle 的 Windows 轮子与 PyInstaller 的组合依赖这个版本；终端用户只需要 EXE，不需要安装 Python。

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
py -3.11 scripts/prepare_models.py
python main.py
```

`prepare_models.py` 仅在开发/打包阶段下载 OCR 模型。模型被放入 `assets/paddleocr` 后，应用运行不再联网。

在 Windows 上，若项目所在路径含中文，程序会在首次 OCR 启动时自动把这三组模型暂存到 `C:\Users\Public\Documents\PDFRedactorRuntime\models` 的纯英文路径，再由 Paddle 加载；PDF 本身仍可位于任意中文路径。

## 打包为单文件 EXE

在已安装 CPython 3.11 x64 的 Windows 机器上运行：

```powershell
.\build.ps1
```

脚本会安装依赖、准备三个离线 PaddleOCR 模型，并在 `dist\PDF自动打码工具.exe` 生成可分发的单文件程序。首次启动需要解压自身和模型，可能比后续启动慢；这是单文件打包的正常行为。

## 安全说明

- 原始 PDF 始终保留在原位置；请自行安全管理源文件。
- 原生文字命中使用 PyMuPDF 的 redaction API 删除对象后才写入输出文件。
- 输出完成后，程序会重新打开文件并针对每个关键词执行与处理阶段相同的严格全文文本校验。若仍有匹配，文件会被保留以便排查，但界面会明确显示“安全校验失败”，不应作为安全成品使用。
- 扫描/图片页重建后没有可搜索文字层；但 OCR 定位为估算，务必复核日志指出的页面。
- 密码仅在当前进程内用于打开和重新加密输出，不写入日志或配置。

## 测试

安装开发依赖后运行：

```powershell
py -3.11 -m pytest
```

测试覆盖真实文字删除、图片页重建、黑白打码、中文文件名、输出重名、关键词统计、密码输出和搜索校验等核心行为。OCR 模型本身的识别效果需在目标文档上进行人工验收。

## 项目结构

```text
pdf_redactor/       核心处理、OCR 适配器和桌面界面
scripts/            开发和构建辅助脚本
tests/              自动化测试
assets/paddleocr/   OCR 模型说明（模型文件不进入版本库）
main.py             桌面程序入口
build.ps1           Windows 单文件构建脚本
```

## 参与开发

提交代码前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。发现安全问题时，请避免在公开 Issue 中附带真实敏感 PDF，可通过 GitHub 私密渠道联系维护者。

发布用的 EXE 和 ZIP 属于构建产物，不进入源码版本库；正式版本应通过 [GitHub Releases](https://github.com/xhz123456qwe/pdf-redactor/releases) 分发。
