$ErrorActionPreference = "Stop"

param(
  [Parameter(Mandatory = $true)]
  [string]$InputPath,

  [string]$OutputPath = "",
  [string]$Prompt = "",
  [string]$Model = "gpt-image-1.5",
  [ValidateSet("openai","gemini")][string]$Provider = "openai"
)

# Staged integration test runner (external provider only).
# Requires:
# - Either:
#   - `AI_PROVIDER_KEY` (OpenAI), OR
#   - `GEMINI_API_KEY` (Gemini), OR
#   - `appdaemon/secrets.yaml` contains `openapi_token` (pytest fallback)
# - InputPath points at a real image file, typically:
#   /media/detection-summary/<zone>/runs/<run_id>/best.jpg (as seen from AppDaemon container)
#
# Config: detection_summary apps use bundle refs (ai_provider_conf: { image: openai-default });
# this test instantiates providers directly for staged runs.

# Ensure we're running from repo root
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
Set-Location $repoRoot

$env:RUN_EXTERNAL_IMAGE_TESTS = "1"
$env:EXTERNAL_IMAGE_PROVIDER = $Provider
$env:EXTERNAL_IMAGE_INPUT_PATH = $InputPath

if ($OutputPath -ne "") {
  $env:EXTERNAL_IMAGE_OUTPUT_PATH = $OutputPath
}

if ($Prompt -ne "") {
  $env:EXTERNAL_IMAGE_PROMPT = $Prompt
}

if ($Model -ne "") {
  if ($Provider -eq "openai") {
    $env:OPENAI_IMAGE_MODEL = $Model
  } else {
    $env:GEMINI_IMAGE_MODEL = $Model
  }
}

$py = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
  throw "Python venv not found at $py. Activate/create .venv first."
}

& $py -m pytest "appdaemon/tests/integration-tests/test_external_image_integration.py" -q
