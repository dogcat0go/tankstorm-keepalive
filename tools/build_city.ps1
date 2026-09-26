# 打出「打城」单文件窗口程序。钩子版 SWF 不进包。
# 用法：powershell -ExecutionPolicy Bypass -File tools/build_city.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

py -m pip install pyinstaller
py -m PyInstaller --noconfirm --clean --onefile --windowed --name 打城 `
    --add-data "config.json;." `
    --add-data "protocol.json;." `
    --add-data "endpoints.json;." `
    --add-data "tankstorm/schema.json;tankstorm" `
    city_gui.py

Write-Host "完成：dist\打城.exe"
Write-Host "协议表更新时，把新的 schema.json 放在 exe 旁边即可，不必重编。"
