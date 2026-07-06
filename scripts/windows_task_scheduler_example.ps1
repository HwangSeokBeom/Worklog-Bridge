[CmdletBinding()]
param(
    [switch]$Register,
    [string]$TaskName = "Worklog Bridge - Fill Latest HWP",
    [string]$RunLatestScript = (Join-Path $PSScriptRoot "windows_run_latest.ps1"),
    [Parameter(Mandatory = $true)]
    [string]$Template,
    [string]$InputDir = "$env:USERPROFILE\Downloads",
    [string]$OutputDir = "C:\worklog\filled"
)

if (-not $Register) {
    Write-Host "안전상 작업 스케줄러를 등록하지 않았습니다."
    Write-Host "회사 정책을 확인한 뒤 -Register를 명시해 다시 실행하세요."
    exit 0
}

$Arguments = "-NoProfile -File `"$RunLatestScript`" -Template `"$Template`" -InputDir `"$InputDir`" -OutputDir `"$OutputDir`""
$Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $Arguments
$Trigger = New-ScheduledTaskTrigger -Daily -At "5:30PM"
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Principal $Principal -Description "수동 전달된 최신 Worklog Bridge JSON을 HWP에 입력" -Force
Write-Host "작업 스케줄러 등록 완료: $TaskName"
