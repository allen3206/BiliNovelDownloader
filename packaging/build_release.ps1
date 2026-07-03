param(
    [string]$Python = 'python'
)
# 打包發行版：建置 exe、組裝 release 資料夾、壓 zip、核對必備檔案。
# 用法見 RELEASING.md。前置需求：tools\ 內恰有一顆 bili_novel_packer exe、
# 環境裝齊 requirements.txt 與 pyinstaller。
$ErrorActionPreference = 'Stop'

$repo = Split-Path -Parent $PSScriptRoot

$version = (Get-Content (Join-Path $repo 'version.txt') -TotalCount 1).Trim()
if ($version -notmatch '^v\d+\.\d+\.\d+$') { throw "version.txt 版號格式不符：'$version'" }

$name  = "BiliNovelDownloader-$version-windows-x64"
$stage = Join-Path $repo $name
$zip   = "$stage.zip"

$packer = @(Get-ChildItem (Join-Path $repo 'tools') -Filter 'bili_novel_packer*.exe')
if ($packer.Count -ne 1) { throw "tools\ 內應恰有一顆 bili_novel_packer exe，目前有 $($packer.Count) 顆" }

Write-Host "== 清理舊產物 =="
foreach ($p in @((Join-Path $repo 'build'), (Join-Path $repo 'dist'), $stage, $zip)) {
    if (Test-Path $p) { Remove-Item $p -Recurse -Force }
}

Write-Host "== PyInstaller 建置（$version）=="
Push-Location $repo
& $Python -m PyInstaller 'BiliNovelDownloader.spec' --noconfirm
$code = $LASTEXITCODE
Pop-Location
if ($code -ne 0) { throw "PyInstaller 失敗（exit $code）" }
$exe = Join-Path $repo 'dist\BiliNovelDownloader.exe'
if (-not (Test-Path $exe)) { throw "找不到建置產物 $exe" }

Write-Host "== 組裝 $name =="
New-Item -ItemType Directory -Path (Join-Path $stage 'tools') -Force | Out-Null
Copy-Item $exe $stage
Copy-Item (Join-Path $repo 'LICENSE') (Join-Path $stage 'LICENSE.txt')
Copy-Item (Join-Path $repo 'NOTICES.txt') $stage
Copy-Item (Join-Path $repo 'THIRD_PARTY_LICENSES.txt') $stage
Copy-Item (Join-Path $PSScriptRoot 'readme.txt') $stage
Copy-Item $packer[0].FullName (Join-Path $stage 'tools')
Copy-Item (Join-Path $repo 'tools\LICENSE-bili_novel_packer.txt') (Join-Path $stage 'tools')
Copy-Item (Join-Path $repo 'tools\README.md') (Join-Path $stage 'tools')

Write-Host "== 壓縮 =="
Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $zip -Force

Write-Host "== 核對必備檔案 =="
$required = @(
    'BiliNovelDownloader.exe', 'LICENSE.txt', 'NOTICES.txt',
    'THIRD_PARTY_LICENSES.txt', 'readme.txt',
    ('tools\' + $packer[0].Name), 'tools\LICENSE-bili_novel_packer.txt', 'tools\README.md'
)
$missing = @()
foreach ($f in $required) {
    if (Test-Path (Join-Path $stage $f)) { Write-Host "  OK  $f" }
    else { Write-Host "  缺  $f"; $missing += $f }
}
if ($missing.Count -gt 0) { throw "缺少必備檔案：$($missing -join '、')" }

$hash = (Get-FileHash $zip -Algorithm SHA256).Hash
Write-Host ''
Write-Host "完成：$zip"
Write-Host "SHA256：$hash"
