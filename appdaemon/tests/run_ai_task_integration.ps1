param(
  [string]$LocalFileCameraEntityId = "camera.detection_summary_test_image",
  [string]$LocalFileCameraPath = "/config/www/detection-summary/garage/buffer/slot_00.jpg",
  [int]$HaWsTimeoutSeconds = 180,
  [ValidateSet("0","1")][string]$RunGenerateImage = "1"
)

$ErrorActionPreference = "Stop"

# Ensure we're running from repo root
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $repoRoot

# Env vars consumed by appdaemon/tests/test_ai_task_integration.py
$env:RUN_HA_INTEGRATION_TESTS = "1"
$env:RUN_GENERATE_IMAGE = $RunGenerateImage
$env:LOCAL_FILE_CAMERA_ENTITY_ID = $LocalFileCameraEntityId
$env:LOCAL_FILE_CAMERA_PATH = $LocalFileCameraPath
$env:HA_WS_TIMEOUT_S = "$HaWsTimeoutSeconds"

Write-Host "Repo root: $repoRoot"
Write-Host "RUN_HA_INTEGRATION_TESTS=$env:RUN_HA_INTEGRATION_TESTS"
Write-Host "RUN_GENERATE_IMAGE=$env:RUN_GENERATE_IMAGE"
Write-Host "LOCAL_FILE_CAMERA_ENTITY_ID=$env:LOCAL_FILE_CAMERA_ENTITY_ID"
Write-Host "LOCAL_FILE_CAMERA_PATH=$env:LOCAL_FILE_CAMERA_PATH"
Write-Host "HA_WS_TIMEOUT_S=$env:HA_WS_TIMEOUT_S"

$py = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
  throw "Python venv not found at $py. Activate/create .venv first."
}

& $py -m pytest "appdaemon/tests/test_ai_task_integration.py" -q
