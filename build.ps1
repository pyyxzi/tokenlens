[CmdletBinding()]
param(
    [string]$Python = 'python',
    [string]$WorkDir = (Join-Path ([IO.Path]::GetTempPath()) 'tokenlens-build'),
    [switch]$Dev,
    [switch]$UseExistingPython
)
$ErrorActionPreference = 'Stop'
$repo = $PSScriptRoot
$work = [IO.Path]::GetFullPath($WorkDir)
$sourceRoot = [IO.Path]::GetFullPath($repo).TrimEnd('\')
if ($work.Equals($sourceRoot,[StringComparison]::OrdinalIgnoreCase) -or $work.StartsWith($sourceRoot+'\',[StringComparison]::OrdinalIgnoreCase)) {
    throw 'WorkDir must be outside the source folder.'
}
New-Item -ItemType Directory -Path $work -Force | Out-Null
function Run-Checked([string]$Command,[string[]]$Arguments) {
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Build command failed: $Command" }
}
$version = & $Python -c 'import sys;print(".".join(map(str,sys.version_info[:3])))'
if ($LASTEXITCODE -ne 0 -or [version]$version -lt [version]'3.14.8' -or [version]$version -ge [version]'3.15.0') {
    throw 'Use Python 3.14.8 or a newer security patch in the 3.14 series.'
}
if ($UseExistingPython) {
    $buildPython = $Python
    Run-Checked $buildPython @('-c','import PyInstaller,zstandard;print(PyInstaller.__version__,zstandard.__version__)')
} else {
$venv = Join-Path $work 'venv'
if (!(Test-Path -LiteralPath (Join-Path $venv 'Scripts\python.exe'))) {
    Run-Checked $Python @('-m','venv',$venv)
}
$buildPython = Join-Path $venv 'Scripts\python.exe'
Run-Checked $buildPython @('-m','pip','install','--disable-pip-version-check','--cache-dir',(Join-Path $work 'pip-cache'),'-r',(Join-Path $repo 'backend\requirements-build.txt'))

}
$env:PYTHONDONTWRITEBYTECODE='1'
$env:PYINSTALLER_CONFIG_DIR=Join-Path $work 'pyinstaller-cache'
$env:TEMP=Join-Path $work 'temp'
$env:TMP=$env:TEMP
New-Item -ItemType Directory -Path $env:TEMP -Force | Out-Null
$binaryDir = Join-Path $work 'sidecar'
Run-Checked $buildPython @('-m','PyInstaller','--noconfirm','--clean','--onefile','--console','--name','tokenlens-service-x86_64-pc-windows-msvc','--distpath',$binaryDir,'--workpath',(Join-Path $work 'python-build'),'--specpath',(Join-Path $work 'python-build'),'--paths',(Join-Path $repo 'backend'),'--icon',(Join-Path $repo 'src-tauri\icons\icon.ico'),'--hidden-import','zstandard',(Join-Path $repo 'backend\service.py'))
$nodeDir = Join-Path $work 'node'
New-Item -ItemType Directory -Path $nodeDir -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $repo 'package.json'),(Join-Path $repo 'package-lock.json') -Destination $nodeDir
Run-Checked 'npm.cmd' @('ci','--prefix',$nodeDir,'--cache',(Join-Path $work 'npm-cache'),'--ignore-scripts','--no-fund','--no-audit')
$env:CARGO_TARGET_DIR=Join-Path $work 'target'
$sidecarTarget = Join-Path $repo 'src-tauri\binaries\tokenlens-service-x86_64-pc-windows-msvc.exe'
New-Item -ItemType Directory -Path (Split-Path -Parent $sidecarTarget) -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $binaryDir 'tokenlens-service-x86_64-pc-windows-msvc.exe') -Destination $sidecarTarget -Force
$cli=Join-Path $nodeDir 'node_modules\.bin\tauri.cmd'
Push-Location $repo
try {
    if($Dev) { Run-Checked $cli @('dev','--target','x86_64-pc-windows-msvc') }
    else { Run-Checked $cli @('build','--target','x86_64-pc-windows-msvc','--','--locked') }
} finally {
    Pop-Location
    if (Test-Path -LiteralPath $sidecarTarget) { Remove-Item -LiteralPath $sidecarTarget -Force }
}
if (!$Dev) {
    $release=Join-Path $env:CARGO_TARGET_DIR 'x86_64-pc-windows-msvc\release'
    $portable=Join-Path $work 'portable'
    New-Item -ItemType Directory -Path $portable -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $release 'TokenLens.exe'),(Join-Path $release 'tokenlens-service.exe'),(Join-Path $repo 'LICENSE'),(Join-Path $repo 'THIRD_PARTY_NOTICES.md') -Destination $portable
    Copy-Item -LiteralPath (Join-Path $repo 'licenses') -Destination $portable -Recurse -Force
    Write-Host "Installer: $(Join-Path $release 'bundle\nsis')"
    Write-Host "Portable: $portable"
}




