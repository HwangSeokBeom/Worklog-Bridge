$ProjectDir = Split-Path -Parent $PSScriptRoot

python (Join-Path $ProjectDir "windows_fill_hwp.py") `
  --json (Join-Path $PSScriptRoot "sample_daily_worklog.json") `
  --template "C:\worklog\template.hwp" `
  --output "C:\worklog\filled\daily_worklog_filled.hwp" `
  --visible true

