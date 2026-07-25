<#
.SYNOPSIS
  Run Stage 1 locally in DRY mode with the environment set up correctly.

.DESCRIPTION
  Encodes the four local-run gotchas documented in the skill (PYTHONPATH, the
  LeadFeeder key living only in GitHub Secrets, the OneDrive write lock, and
  FORCE_COLOR breaking `sf --json` parsing) so the command does not have to be
  composed by hand each time.

  --dry-run is passed unconditionally and write flags are refused: this script
  cannot make a live Salesforce write. Live runs are dispatched deliberately (see
  the /live-convert skill).

.PARAMETER Limit
  LIMIT-scope the task scan. 0 (default) uses the config value.

.PARAMETER NoLeadfeeder
  Skip LeadFeeder enrichment entirely (no credits, no key needed). Every lead then
  matches rule `no_lf_match` and terminally skips, so this checks plumbing only,
  not matching.

.PARAMETER NoKununuFeed
  Skip the kununu profile feed (~551 MB download on a cold cache). Without it there
  are no alt names AND no profile scores.

.PARAMETER QaPath
  Where to write the QA artifact. Defaults to a timestamped file under
  $env:TEMP\leadauto-dryrun (deliberately outside OneDrive).
#>
[CmdletBinding()]
param(
  [int]$Limit = 0,
  [switch]$NoLeadfeeder,
  [switch]$NoKununuFeed,
  [string]$QaPath
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

# --- Gotcha 4: the LeadFeeder key lives ONLY in GitHub Secrets (all local plaintext
#     copies were deleted 2026-07-05). Without it the run degrades silently to
#     no-enrichment, which looks like a matching regression rather than a missing key.
$cliArgs = @('-m', 'leadauto', 'stage1', '--dry-run', '--qa-path', $QaPath)
if ($Limit -gt 0)     { $cliArgs += @('--limit', "$Limit") }
if ($NoLeadfeeder)    { $cliArgs += '--no-leadfeeder' }
if ($NoKununuFeed)    { $cliArgs += '--no-kununu-feed' }

if (-not $NoLeadfeeder -and -not $env:LEADFEEDER_API_KEY) {
  Write-Warning "LEADFEEDER_API_KEY is not set. The run will proceed WITHOUT enrichment, so every lead becomes no_lf_match -> skip. Set it ad hoc for this shell (`$env:LEADFEEDER_API_KEY = '...'`) or pass -NoLeadfeeder to make the degradation explicit."
}
if (-not $NoKununuFeed -and -not $env:KUNUNU_API_TOKEN) {
  Write-Warning "KUNUNU_API_TOKEN is not set: no alt names and NO profile scores. The live-scope gate fails closed on a missing score, so live_scope_pass will read 0."
}

Write-Host "Stage 1 DRY run" -ForegroundColor Cyan
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

