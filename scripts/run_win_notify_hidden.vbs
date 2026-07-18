' Win notification launcher (2026-07-18)
' Purpose: run scripts/win_notify.py WITHOUT a console window.
' Used by Task Scheduler "AutoraceWinNotify" (daily 02:45), i.e. right after
' AutoraceFetchOrderHistory (02:30) has refreshed bet_history_detail.csv.
' Sends a mail only when the target day actually had winning bets.
Option Explicit
Dim sh, fso, proj, cmd, rc
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
proj = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
cmd = "cmd /c chcp 65001 >nul && cd /d """ & proj & """ && " & _
      "python scripts\win_notify.py " & _
      ">> data\win_notify.log 2>&1"
' 0 = hidden window, True = wait -> propagate exit code to Task Scheduler
rc = sh.Run(cmd, 0, True)
WScript.Quit rc
