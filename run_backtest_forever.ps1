# 백테스트 무인 영속 실행 래퍼
# run_backtest.py 가 어떤 이유로든 종료되면 자동 재시작 (resume — 완료 종목 스킵).
# 세션과 독립적으로 실행:
#   Start-Process powershell -ArgumentList '-ExecutionPolicy','Bypass','-File','run_backtest_forever.ps1' -WindowStyle Hidden

$ErrorActionPreference = 'Continue'
Set-Location -Path $PSScriptRoot
$log = Join-Path $PSScriptRoot 'backtest_forever.log'

# 단일 인스턴스 가드: run_backtest.py python 이 이미 돌면 중복 실행 방지
# (powershell 명령은 체크 안 함 — 관리/모니터링 명령과 오탐 충돌 방지)
try {
    $pys = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -like '*run_backtest.py*' }
    if ($pys) {
        "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') [wrapper] run_backtest.py 이미 실행 중 — 중복 방지로 종료" | Out-File -FilePath $log -Append -Encoding utf8
        exit 0
    }
} catch {}

# 절전/화면잠금으로 인한 프로세스 종료 방지 (실행 중에만 깨어있게 요청, 종료 시 자동 해제)
try {
    Add-Type -Name Power -Namespace Win32 -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("kernel32.dll", CharSet=System.Runtime.InteropServices.CharSet.Auto, SetLastError=true)]
public static extern uint SetThreadExecutionState(uint esFlags);
'@
    # ES_CONTINUOUS(0x80000000) | ES_SYSTEM_REQUIRED(0x1) | ES_AWAYMODE_REQUIRED(0x40)
    [Win32.Power]::SetThreadExecutionState([uint32]'0x80000041') | Out-Null
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') [wrapper] 절전 방지 활성" | Out-File -FilePath $log -Append -Encoding utf8
} catch {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') [wrapper] 절전방지 설정 실패: $_" | Out-File -FilePath $log -Append -Encoding utf8
}

while ($true) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') [wrapper] 백테스트 시작" | Out-File -FilePath $log -Append -Encoding utf8
    try {
        # run_backtest.py 가 backtest_standalone.log 를 FileHandler로 직접 사용하므로
        # 콘솔/예외 출력만 별도 파일로 (동일 파일 동시 쓰기 충돌 방지)
        & python run_backtest.py --mode ALL *>> (Join-Path $PSScriptRoot 'backtest_console.log')
    } catch {
        "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') [wrapper] 예외: $_" | Out-File -FilePath $log -Append -Encoding utf8
    }
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') [wrapper] 종료됨 — 30초 후 재시작" | Out-File -FilePath $log -Append -Encoding utf8
    Start-Sleep -Seconds 30
}
