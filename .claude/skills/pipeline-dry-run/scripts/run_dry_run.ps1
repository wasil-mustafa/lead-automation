<#
.SYNOPSIS
  Run the fused pipeline (`python -m leadauto run --dry-run`) locally with the
  environment set up correctly.

.DESCRIPTION
  Encodes the four local-run gotchas documented in the skill (PYTHONPATH, a missing
  LeadFeeder key degrading silently, the OneDrive write lock, and
  FORCE_COLOR breaking `sf --json` parsing) so the command does not have to be
  composed by hand each time.

  --dry-run is passed unconditionally and write flags are refused: this script
  cannot make a live Salesforce write. A dry run still previews the convert sweep
  (~2 read calls) and fills the QA `Conversion` sheet. Live runs are dispatched
  deliberately (see the live-convert skill and .github/workflows/pipeline.yml).

.PARAMETER Limit
  LIMIT-scope the task scan. 0 (default) uses the config value.

.PARAMETER NoLeadfeeder
  Skip LeadFeeder enrichment entirely (no credits, no key needed). Every lead then
  matches rule `no_lf_match` and terminally skips, so this checks plumbing only,
  not matching.

.PARAMETER NoKununuFeed
  Skip the kununu profile feed (large download on a cold cache). Without it there
  are no alt names and no kununu_score values.

.PARAMETER BatchSize
  Leads per SOAP convertLead() call. 0 (default) = config pipeline.convert_batch_size
  (3, the live governor-limit ceiling). Rarely useful on a dry run.

.PARAMETER QaPath
  Where to write the QA artifact. Defaults to a timestamped file under
  $env:TEMP\leadauto-dryrun (deliberately outside OneDrive).
#>
[CmdletBinding()]
param(
  [int]$Limit = 0,
  [switch]$NoLeadfeeder,
  [switch]$NoKununuFeed,
  [string]$QaPath,
  [int]$BatchSize = 0
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)))

# --- Gotcha 1: the package lives under src/ and is not pip-installed.
$env:PYTHONPATH = Join-Path $repoRoot 'src'

# --- Gotcha 2: FORCE_COLOR makes the sf CLI emit ANSI codes even through a pipe,
#     which breaks json.loads of --json output.
Remove-Item Env:\FORCE_COLOR -ErrorAction SilentlyContinue
$env:NO_COLOR = '1'

# --- Gotcha 3: OneDrive. The repo is inside OneDrive; if a previous run's xlsx is
#     mid-sync, openpyxl can die with PermissionError partway through the save.
if (-not $QaPath) {
  $outDir = Join-Path $env:TEMP 'leadauto-dryrun'
  if (-not (Test-Path $outDir)) { New-Item -ItemType Directory -Path $outDir | Out-Null }
  $stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
  $QaPath = Join-Path $outDir "qa_dryrun_$stamp.xlsx"
}
if ($QaPath -like '*OneDrive*') {
  throw "QaPath is inside OneDrive ($QaPath). Pick a path outside it -- a sync lock can kill the xlsx save mid-write."
}

# --- Gotcha 4: a missing LeadFeeder key degrades silently to no-enrichment, which looks
#     like a matching regression rather than a missing key. Python auto-loads the repo's
#     .env (python-dotenv, config.py), so warn only when the key is in neither place.
$dotEnv = Join-Path $repoRoot '.env'
function Test-KeyAvailable([string]$name) {
  if ([Environment]::GetEnvironmentVariable($name)) { return $true }
  if (Test-Path $dotEnv) { return [bool](Select-String -Path $dotEnv -Pattern "^\s*$name\s*=\s*\S" -Quiet) }
  return $false
}
$cliArgs = @('-m', 'leadauto', 'run', '--dry-run', '--qa-path', $QaPath)
if ($Limit -gt 0)     { $cliArgs += @('--limit', "$Limit") }
if ($NoLeadfeeder)    { $cliArgs += '--no-leadfeeder' }
if ($NoKununuFeed)    { $cliArgs += '--no-kununu-feed' }
if ($BatchSize -gt 0) { $cliArgs += @('--batch-size', "$BatchSize") }

if (-not $NoLeadfeeder -and -not (Test-KeyAvailable 'LEADFEEDER_API_KEY')) {
  Write-Warning "LEADFEEDER_API_KEY is in neither the shell env nor .env. The run will proceed WITHOUT enrichment, so every lead becomes no_lf_match -> skip. Set it ad hoc for this shell (`$env:LEADFEEDER_API_KEY = '...'`) or pass -NoLeadfeeder to make the degradation explicit."
}
if (-not $NoKununuFeed -and -not (Test-KeyAvailable 'KUNUNU_API_PASSWORD')) {
  # The feed is HTTP Basic, read from KUNUNU_API_PASSWORD. Never set KUNUNU_API_TOKEN
  # locally: it is sent as a Bearer token and 401s.
  Write-Warning "KUNUNU_API_PASSWORD is in neither the shell env nor .env: the feed read degrades silently, so no alt names and no kununu_score values (the score no longer gates live scope since 2026-09-28, but name agreement loses its alt-name input). Set it ad hoc or pass -NoKununuFeed to make the degradation explicit."
}

Write-Host "Pipeline DRY run (leadauto run --dry-run)" -ForegroundColor Cyan
Write-Host "  repo:  $repoRoot"
Write-Host "  qa:    $QaPath"
Write-Host "  args:  $($cliArgs -join ' ')"
Write-Host ""

Push-Location $repoRoot
try {
  & python @cliArgs
  $code = $LASTEXITCODE
} finally {
  Pop-Location
}

Write-Host ""
Write-Host "exit=$code  qa=$QaPath"
exit $code

