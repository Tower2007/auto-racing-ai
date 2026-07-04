' Odds time-series collector launcher (2026-07-04)
' Purpose: run scripts/odds_timeseries_collector.py WITHOUT a console window.
' Used by Task Scheduler "AutoraceOddsTsCollector" (daily 07:05).
' Guard: if another collector python is already running today, the collector
'   itself is restart-safe (resumes from today's jsonl), so double-start is
'   harmless but wasteful; we keep it simple and always launch.
Option Explicit
Dim sh, fso, proj, cmd
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
proj = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
cmd = "cmd /c chcp 65001 >nul && cd /d """ & proj & """ && " & _
      "python scripts\odds_timeseries_collector.py " & _
      ">> data\odds_ts_run.log 2>&1"
' 0 = hidden window, False = don't wait (runs until last race ends)
sh.Run cmd, 0, False
