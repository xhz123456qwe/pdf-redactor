# 参与开发

感谢参与 PDF 自动打码工具的改进。

## 开发环境

- Windows 10/11
- CPython 3.11 x64
- Git

```powershell
git clone https://github.com/xhz123456qwe/pdf-redactor.git
cd pdf-redactor
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements-dev.txt
py -3.11 scripts/prepare_models.py
```

## 提交流程

1. 从 `main` 创建用途明确的分支，例如 `feat/manual-redaction` 或 `fix/encrypted-preview`。
2. 保持改动聚焦；复杂逻辑应补充说明性注释，公开函数应使用简洁 docstring。
3. 运行 `py -3.11 -m pytest`，并对 OCR 定位和最终 PDF 进行人工目视复核。
4. 使用清晰的提交信息，建议采用 `feat:`、`fix:`、`docs:`、`test:`、`refactor:` 等前缀。
5. 发起 Pull Request，说明动机、验证方式和任何安全影响。

请勿提交原始敏感 PDF、OCR 模型、虚拟环境、构建目录、EXE 或 ZIP 发布包。

## 问题报告

Issue 应包含复现步骤、预期行为、实际行为和运行环境。请使用脱敏或人工生成的示例文件，切勿公开真实隐私数据及 PDF 密码。
