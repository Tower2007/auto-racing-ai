' AutoraceDyn one-shot wrapper (2026-06-11)
' Purpose: run daily_predict.py for one race WITHOUT showing a console window.
'   Old style ("cmd /c ... python daily_predict.py ...") popped a console
'   window every time a per-race task fired (10+ times/day).
' Usage: wscript.exe //B run_predict_hidden.vbs <place_code> <race_no> <label>
' Notes:
'   - Runs in the interactive user session, so auto_buy's Playwright Chrome
'     (headless=False) still works. Only the cmd console is hidden (Run ..., 0).
'   - stdout/stderr are appended to data\dynamic_run.log as before.
'   - 2026-07-12 audit P1-2: sh.Run's return value (the child exit code) used
'     to be discarded, so Python failures looked like rc=0 to Task Scheduler.
'     Capture it and propagate via WScript.Quit (0 only on real success).
Option Explicit
Dim sh, fso, proj, pc, rn, label, cmd, rc
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
' project root = parent of this script's folder (scripts\..)
proj = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
pc = WScript.Arguments(0)
rn = WScript.Arguments(1)
label = WScript.Arguments(2)
cmd = "cmd /c chcp 65001 >nul && cd /d """ & proj & """ && " & _
      "python daily_predict.py --venues " & pc & " --races " & rn & _
      " --suppress-noresult-email --time-label """ & label & """" & _
      " >> data\dynamic_run.log 2>&1"
' 0 = hidden window, True = wait for completion -> rc = child exit code
rc = sh.Run(cmd, 0, True)
WScript.Quit rc
