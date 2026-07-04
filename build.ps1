[CmdletBinding()]
param(
    [string]$PythonExecutable = "py",
    [string]$PythonVersion = "-3.11"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

& $PythonExecutable $PythonVersion --version
if ($LASTEXITCODE -ne 0) {
    throw "需要安装 CPython 3.11 x64。"
}

& $PythonExecutable $PythonVersion -m pip install -r requirements.txt
& $PythonExecutable $PythonVersion scripts/prepare_models.py

$required = @(
    "assets/paddleocr/ch_PP-OCRv4_det_infer/inference.pdiparams",
    "assets/paddleocr/ch_PP-OCRv4_rec_infer/inference.pdiparams",
    "assets/paddleocr/ch_ppocr_mobile_v2.0_cls_infer/inference.pdiparams"
)
foreach ($file in $required) {
    if (-not (Test-Path $file)) { throw "离线模型缺失：$file" }
}

& $PythonExecutable $PythonVersion -m PyInstaller --noconfirm --clean --onefile --windowed --name "PDF自动打码工具" --add-data "assets/paddleocr;assets/paddleocr" --hidden-import paddle --hidden-import paddleocr --hidden-import paddleocr.paddleocr --hidden-import cv2 --hidden-import numpy --collect-data paddle --collect-binaries paddle --collect-all paddleocr --collect-data Cython --collect-binaries cv2 --collect-binaries numpy main.py

Copy-Item -LiteralPath "$root\用户使用手册.md" -Destination "$root\dist\用户使用手册.md" -Force
Write-Host "构建完成：$root\dist\PDF自动打码工具.exe"
