$ErrorActionPreference = 'Stop'

Write-Host ''
Write-Host '================================================' -ForegroundColor Cyan
Write-Host '  dsh-paper-translate  one-click installer' -ForegroundColor Cyan
Write-Host '  Translate academic PDFs to Chinese (layout kept)' -ForegroundColor Cyan
Write-Host '================================================' -ForegroundColor Cyan
Write-Host ''

# ---- 1. locate dsh.cmd ----
$dshCmd = $null
$candidates = @(
    "$env:LOCALAPPDATA\Programs\deepseek-harness\resources\runtime\cli\bin\dsh.cmd",
    "$env:LOCALAPPDATA\@deepseek-ai\dsh\resources\runtime\cli\bin\dsh.cmd",
    "$env:APPDATA\deepseek-harness\resources\runtime\cli\bin\dsh.cmd",
    "C:/Program Files/DeepSeek Harness/resources/runtime/cli/bin/dsh.cmd",
    "D:/deepseekharness/resources/runtime/cli/bin/dsh.cmd"
)
foreach ($c in $candidates) {
    if (Test-Path $c) { $dshCmd = $c; break }
}
if (-not $dshCmd) {
    $found = Get-ChildItem -Path C:/,D:/ -Filter 'dsh.cmd' -Recurse -ErrorAction SilentlyContinue -Depth 6 |
             Where-Object { $_.FullName -match 'runtime\\cli\\bin\\dsh\.cmd$' } | Select-Object -First 1
    if ($found) { $dshCmd = $found.FullName }
}
if (-not $dshCmd) {
    Write-Host 'Cannot find DeepSeek Harness dsh.cmd.' -ForegroundColor Red
    Write-Host 'Please install DeepSeek Harness first, then run:' -ForegroundColor Red
    Write-Host '  dsh plugin --profile desktop add dsh-paper-translate' -ForegroundColor Yellow
    Write-Host ''
    Read-Host 'Press Enter to exit'
    exit 1
}
Write-Host "[1/3] Found DSH command: $dshCmd" -ForegroundColor Green

# ---- 2. install plugin ----
Write-Host '[2/3] Installing dsh-paper-translate from npm (~1 min)...' -ForegroundColor Green
$profile = 'desktop'
if ($args.Count -ge 1) { $profile = $args[0] }

& $dshCmd plugin --profile $profile add dsh-paper-translate
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Plugin install failed. Please screenshot the error above and send to the maintainer.' -ForegroundColor Red
    Read-Host 'Press Enter to exit'
    exit 1
}
Write-Host 'Plugin installed.' -ForegroundColor Green

# ---- 3. install Python deps ----
Write-Host '[3/3] Installing Python deps (pymupdf / python-docx / pillow, ~25 MB)...' -ForegroundColor Green
$profileDir = Join-Path $env:USERPROFILE ".dsh\profiles\$profile"
$pluginDir = Join-Path $profileDir 'node_modules\dsh-paper-translate'

$pythonCmd = $null
foreach ($py in @('python', 'py')) {
    $t = Get-Command $py -ErrorAction SilentlyContinue
    if ($t) { $pythonCmd = $py; break }
}
if (-not $pythonCmd) {
    Write-Host 'Python not found. Install Python 3.10+ first (check Add to PATH during install).' -ForegroundColor Yellow
    Write-Host 'Skipping dependency install; the plugin will report missing deps later.' -ForegroundColor Yellow
} else {
    if (Test-Path (Join-Path $pluginDir 'python\env_check.py')) {
        & $pythonCmd (Join-Path $pluginDir 'python\env_check.py') --root $pluginDir --venv (Join-Path $pluginDir '.venv') --bootstrap
        if ($LASTEXITCODE -ne 0) {
            Write-Host 'Dependency install incomplete, can retry later.' -ForegroundColor Yellow
        } else {
            Write-Host 'Python deps installed.' -ForegroundColor Green
        }
    } else {
        Write-Host 'Plugin python scripts not found, skipping dependency install.' -ForegroundColor Yellow
    }
}

# ---- 4. done ----
Write-Host ''
Write-Host '================================================' -ForegroundColor Cyan
Write-Host '  Done!' -ForegroundColor Green
Write-Host '================================================' -ForegroundColor Cyan
Write-Host 'Next steps:' -ForegroundColor White
Write-Host '  1. Fully quit DeepSeek Harness (not just refresh) and reopen.' -ForegroundColor White
Write-Host '  2. Say "check the paper translation environment" and confirm deps are ready.' -ForegroundColor White
Write-Host '  3. Say "translate this paper to Chinese, keep figures" to start.' -ForegroundColor White
Write-Host ''
Read-Host 'Press Enter to close'
