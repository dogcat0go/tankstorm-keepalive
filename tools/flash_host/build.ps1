# 编 tools/flash_host（Qt WebEngine + PPAPI Flash 的抓包窗口）
param(
    [ValidateSet('release','debug')][string]$Config = 'release',
    [switch]$Qmake
)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot

function Find-Tool([string[]]$candidates, [string]$filter, [string[]]$roots) {
    foreach ($c in $candidates) {
        if ($c -and (Test-Path -LiteralPath $c)) { return $c }
    }
    foreach ($r in $roots) {
        if (-not (Test-Path -LiteralPath $r)) { continue }
        $hit = Get-ChildItem -LiteralPath $r -Filter $filter -Recurse -Depth 6 -ErrorAction SilentlyContinue |
               Where-Object { $_.FullName -match 'msvc2019_64\\bin\\|\\jom\\jom\.exe$' } |
               Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }
    return $null
}

function Find-VcVars {
    foreach ($c in @(
        $env:VCVARS, $env:VCVARSALL,
        'd:\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvarsall.bat'
    )) {
        if ($c -and (Test-Path -LiteralPath $c)) { return $c }
    }
    $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
    if (Test-Path -LiteralPath $vswhere) {
        $inst = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2>$null |
                 Select-Object -First 1
        if ($inst) {
            $bat = Join-Path $inst.Trim() 'VC\Auxiliary\Build\vcvarsall.bat'
            if (Test-Path -LiteralPath $bat) { return $bat }
        }
    }
    return $null
}

$vcvars = Find-VcVars
$qmakeExe = Find-Tool @(
    $env:QMAKE,
    'D:\Qt\5.15.2\msvc2019_64\bin\qmake.exe',
    'D:\Qt\5.15.19\msvc2019_64\bin\qmake.exe'
) 'qmake.exe' @('D:\Qt', 'C:\Qt')
$jom = Find-Tool @('D:\Qt\Tools\QtCreator\bin\jom\jom.exe') 'jom.exe' @('D:\Qt', 'C:\Qt')
if (-not $vcvars -or -not $qmakeExe -or -not $jom) {
    throw "找不到编译工具。vcvars=$vcvars qmake=$qmakeExe jom=$jom"
}

$out = Join-Path $root "build\$Config"
New-Item -ItemType Directory -Force $out | Out-Null
Push-Location $out
try {
    cmd /c "`"$vcvars`" x64 && `"$qmakeExe`" `"$root\flash_host.pro`" -spec win32-msvc `"CONFIG+=$Config`" && `"$jom`" -j4"
    if ($LASTEXITCODE -ne 0) { throw "build failed: $LASTEXITCODE" }
} finally {
    Pop-Location
}
$exe = Join-Path $out "release\flash_host.exe"
if ($Config -eq 'debug') { $exe = Join-Path $out "debug\flash_host.exe" }
if (-not (Test-Path $exe)) {
    $exe = Get-ChildItem $out -Recurse -Filter flash_host.exe | Select-Object -First 1 -ExpandProperty FullName
}
Write-Host "ok $exe"
$qtbin = Split-Path $qmakeExe -Parent
$wd = Join-Path $qtbin "windeployqt.exe"
if (Test-Path $wd) {
    Write-Host "windeployqt..."
    & $wd --release --no-translations --webenginewidgets $exe
}
$exedir = Split-Path $exe -Parent
foreach ($dll in @("libssl-1_1-x64.dll","libcrypto-1_1-x64.dll")) {
    $dst = Join-Path $exedir $dll
    if (Test-Path $dst) { continue }
    foreach ($s in @(
        (Join-Path $root "..\..\..\hjdz-automation\$dll"),
        (Join-Path (Split-Path $qmakeExe -Parent) $dll)
    )) {
        if (Test-Path $s) { Copy-Item $s $dst; break }
    }
}
