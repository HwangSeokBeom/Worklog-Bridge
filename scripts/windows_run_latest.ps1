[CmdletBinding()]
param(
    [string]$InputDir = "$env:USERPROFILE\Downloads",
    [Parameter(Mandatory = $true)]
    [string]$Template,
    [string]$OutputDir = "C:\worklog\filled",
    [string]$Python = "python",
    [bool]$Visible = $false
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $PSScriptRoot
$Filler = Join-Path $ProjectDir "windows_fill_hwp.py"

$Latest = Get-ChildItem -Path $InputDir -File |
    Where-Object { $_.Name -match '^(daily|weekly)_worklog_.*\.json$' } |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1

if (-not $Latest) {
    throw "입력 폴더에서 daily/weekly worklog JSON을 찾지 못했습니다: $InputDir"
}
if (-not (Test-Path -LiteralPath $Template -PathType Leaf)) {
    throw "HWP 템플릿을 찾지 못했습니다: $Template"
}

New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
$BaseName = [IO.Path]::GetFileNameWithoutExtension($Latest.Name)
if ($BaseName -match '^weekly_worklog_(\d{4})-(\d{2})$') {
    $BaseName = "weekly_worklog_$($Matches[1])_W$($Matches[2])"
}
$Output = Join-Path $OutputDir "${BaseName}_filled.hwp"
$VisibleValue = if ($Visible) { "true" } else { "false" }

Write-Host "입력 JSON: $($Latest.FullName)"
Write-Host "출력 HWP: $Output"
& $Python $Filler --json $Latest.FullName --template $Template --output $Output --visible $VisibleValue
exit $LASTEXITCODE

