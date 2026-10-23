# 一键打包成单文件 EXE (Windows)
#   用法:  .\build.ps1
#   产物:  .\dist\GHTrendingTop10.exe
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { throw "找不到 python, 请先安装 Python 3.10+" }

Write-Host "[1/3] 检查依赖 ..." -ForegroundColor Cyan
python -c "import PIL, PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "缺少 pillow / pyinstaller, 正在安装 ..." -ForegroundColor Yellow
    python -m pip install --upgrade pillow pyinstaller
}

# 图标和闪屏不存在时用脚本生成
if (-not (Test-Path "assets\icon.ico")) {
    Write-Host "[2/3] 生成图标 / 闪屏 ..." -ForegroundColor Cyan
    python tools\make_icon.py
    python tools\make_splash.py
}

Write-Host "[3/3] PyInstaller 打包 ..." -ForegroundColor Cyan
$icon = (Resolve-Path "assets\icon.ico").Path
$splash = (Resolve-Path "assets\splash.png").Path

python -m PyInstaller --onefile --windowed --noconfirm `
    --name GHTrendingTop10 `
    --icon "$icon" `
    --splash "$splash" `
    --distpath dist --workpath build --specpath build `
    ghtrending.py

if ($LASTEXITCODE -ne 0) { throw "打包失败" }

$exe = Get-Item "dist\GHTrendingTop10.exe"
Write-Host ("完成: {0}  ({1:N1} MB)" -f $exe.FullName, ($exe.Length / 1MB)) -ForegroundColor Green
Write-Host "提示: 把 EXE 放到一个独立目录再运行, 它会在同目录写 settings.json / trending_cache.json" -ForegroundColor DarkGray
