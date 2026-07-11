# AutoraceOddsPrerace タスク登録スクリプト (2026-07-03)
#
# 直前オッズ常駐デーモン odds_prerace_daemon.py を
#   1. 毎朝 07:05 (AutoraceDynamicScheduler 07:00 の直後、Program/Print 公開済み)
#   2. ログオン時 (日中の再起動からの復帰用。過去 event はスキップして残りを収集)
# に pythonw.exe 直接で起動する (vbs は使わない — banei の cmd /c 二重クォート事故の教訓)。
# 二重起動はデーモン側の named mutex ガード + IgnoreNew の 2 段で防止
# (旧 localhost:58620 bind 方式は WinNAT/Hyper-V の動的除外ポート帯で
#  WinError 10013 誤検知 → 収集停止のため 2026-07-12 に mutex 化)。
#
# 使い方 (再実行可・冪等):
#   powershell -ExecutionPolicy Bypass -File scripts\register_odds_prerace_task.ps1

$ErrorActionPreference = "Stop"

$taskName = "AutoraceOddsPrerace"
$projDir  = "C:\Users\no28a\Claude-project\Auto_racing_AI"
$pythonw  = "C:\Python313\pythonw.exe"
$script   = Join-Path $projDir "odds_prerace_daemon.py"

if (-not (Test-Path $pythonw)) { throw "pythonw.exe が見つかりません: $pythonw" }
if (-not (Test-Path $script))  { throw "デーモン本体が見つかりません: $script" }

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$script`"" -WorkingDirectory $projDir

$trigDaily = New-ScheduledTaskTrigger -Daily -At 07:05
$trigLogon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

# ExecutionTimeLimit 20h: 07:05 起動 → ミッドナイト最終レース (〜翌0時台) まで許容。
# MultipleInstances IgnoreNew: 常駐中に ONLOGON/翌朝 Daily が来ても新規起動しない
# (デーモン側の named mutex ガードとの 2 段防御)。
$settingsParams = @{
    AllowStartIfOnBatteries    = $true
    DontStopIfGoingOnBatteries = $true
    StartWhenAvailable         = $true
    ExecutionTimeLimit         = (New-TimeSpan -Hours 20)
    MultipleInstances          = "IgnoreNew"
}
$settings = New-ScheduledTaskSettingsSet @settingsParams

$registerParams = @{
    TaskName    = $taskName
    Action      = $action
    Trigger     = @($trigDaily, $trigLogon)
    Settings    = $settings
    Description = "autorace 直前オッズ常駐デーモン (全レース T-5/T-1 全券種 -> data/odds_combo_prerace.csv)"
    Force       = $true
}
Register-ScheduledTask @registerParams | Out-Null

Write-Host "登録完了: $taskName"
Get-ScheduledTask -TaskName $taskName | ForEach-Object {
    Write-Host ("  State : " + $_.State)
    Write-Host ("  Action: " + $_.Actions[0].Execute + " " + $_.Actions[0].Arguments)
    Write-Host ("  WorkDir: " + $_.Actions[0].WorkingDirectory)
    $_.Triggers | ForEach-Object {
        Write-Host ("  Trigger: " + $_.CimClass.CimClassName + " " + $_.StartBoundary)
    }
}
