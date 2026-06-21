# 백테스트 무인 영속 실행 래퍼
# run_backtest.py 가 어떤 이유로든 종료되면 자동 재시작 (resume — 완료 종목 스킵).
# 세션과 독립적으로 실행:
#   Start-Process powershell -ArgumentList '-ExecutionPolicy','Bypass','-File','run_backtest_forever.ps1' -WindowStyle Hidden

$ErrorActionPreference = 'Continue'
Set-Location -Path $PSScriptRoot
$log = Join-Path $PSScriptRoot 'backtest_forever.log'

while ($true) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') [wrapper] 백테스트 시작" | Out-File -FilePath $log -Append -Encoding utf8
    try {
        & python run_backtest.py --mode ALL *>> (Join-Path $PSScriptRoot 'backtest_standalone.log')
    } catch {
        "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') [wrapper] 예외: $_" | Out-File -FilePath $log -Append -Encoding utf8
    }
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') [wrapper] 종료됨 — 30초 후 재시작" | Out-File -FilePath $log -Append -Encoding utf8
    Start-Sleep -Seconds 30
}
