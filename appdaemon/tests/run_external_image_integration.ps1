$ErrorActionPreference = "Stop"

param(
  [Parameter(Mandatory = $true)]
  [string]$InputPath,

  [string]$OutputPath = "",
  [string]$Prompt = "",
  [string]$Model = "gpt-image-1.5"
)

# Staged integration test runner (external provider only).
# Requires:
# - Either:
#   - `AI_PROVIDER_KEY`, OR
#   - `appdaemon/secrets.yaml` contains `openapi_token: "..."` (pytest loads it as a fallback)
# - InputPath points at a real image file, typically:
#   /media/detection-summary/<zone>/runs/<run_id>/best.jpg (as seen from AppDaemon container)

$env:RUN_EXTERNAL_IMAGE_TESTS = "1"
$env:EXTERNAL_IMAGE_INPUT_PATH = $InputPath

if ($OutputPath -ne "") {
  $env:EXTERNAL_IMAGE_OUTPUT_PATH = $OutputPath
}

if ($Prompt -ne "") {
  $env:EXTERNAL_IMAGE_PROMPT = $Prompt
}

if ($Model -ne "") {
  $env:OPENAI_IMAGE_MODEL = $Model
}

python -m pytest -q .\tests\test_external_image_integration.py

