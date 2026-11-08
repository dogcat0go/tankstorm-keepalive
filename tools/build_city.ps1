# 打出「打城」单文件窗口程序。钩子版 SWF 不进包。
# 用法：powershell -ExecutionPolicy Bypass -File tools/build_city.ps1
# 公会版不写账号、不核对有效期。先设 $env:TANKSTORM_LOCK_GUILD="公会ID" 再执行，得到 dist\打城-公会.exe。
# 不设这个变量时得到原来的账号版 dist\打城.exe。
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

py -m pip install pyinstaller
py -m PyInstaller --noconfirm --clean 打城.spec

Write-Host "完成：dist\打城.exe"
Write-Host "游戏协议再更新时，把新的 wiremap.json 放在 exe 旁边即可，不必重编。"
