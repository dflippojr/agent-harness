# Owner-run optional image components. Does not run on daemon start.
#   powershell -ExecutionPolicy Bypass -File ops/images-models.ps1 status flux-fast
#   powershell -ExecutionPolicy Bypass -File ops/images-models.ps1 install flux-fast
#   powershell -ExecutionPolicy Bypass -File ops/images-models.ps1 remove flux-fast
#   powershell -ExecutionPolicy Bypass -File ops/images-models.ps1 stage-comfyui
#   powershell -ExecutionPolicy Bypass -File ops/images-models.ps1 validate-comfyui
#   powershell -ExecutionPolicy Bypass -File ops/images-models.ps1 promote-comfyui
#   powershell -ExecutionPolicy Bypass -File ops/images-models.ps1 rollback-comfyui
#
# install/remove/status are idempotent. remove never deletes the Z-Image qwen_3_4b encoder.
# stage/validate/promote never change C:\AI\ComfyUI until promote; rollback restores the previous tree.
param(
    [Parameter(Position = 0, Mandatory = $true)]
    [ValidateSet('install', 'remove', 'status', 'stage-comfyui', 'validate-comfyui', 'promote-comfyui', 'rollback-comfyui')]
    [string]$Action,
    [Parameter(Position = 1)]
    [string]$Component = 'flux-fast',
    [string]$ExtractComfyUI = ''
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { $python = 'python' }

$argsList = @('-m', 'harness.images_models', $Action, $Component)
if ($ExtractComfyUI) { $argsList += @('--extract-comfyui', $ExtractComfyUI) }

Write-Host "agent-harness images-models $Action $Component"
& $python @argsList
exit $LASTEXITCODE
