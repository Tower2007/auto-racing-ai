' Night report launcher (2026-08-07)
' Purpose: run night_report.py WITHOUT a console window (fleet standard).
' Used by Task Scheduler "AutoraceNightReport" (daily 22:00).
' Nightly-mail contract v1: aggregates today's provisional results +
' yesterday's confirmed figures, appends reflection to logs/reflections/,
' then sends the nightly mail ([mail] sent marker goes to data\night_report.log).
Option Explicit
Dim sh, fso, proj, cmd, rc
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
proj = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
cmd = "cmd /c chcp 65001 >nul && cd /d """ & proj & """ && " & _
      "python night_report.py " & _
      ">> data\night_report_run.log 2>&1"
' 0 = hidden window, True = wait -> propagate exit code to Task Scheduler
rc = sh.Run(cmd, 0, True)
WScript.Quit rc
