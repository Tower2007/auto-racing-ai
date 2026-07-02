' Streamlit app launcher (2026-06-11)
' Purpose: start app/streamlit_app.py on port 8501 WITHOUT a console window,
'   detached from any Claude/terminal session so it survives session restarts.
' Used by Task Scheduler "AutoraceStreamlitApp" (ONLOGON + manual /Run).
' Guard: if port 8501 is already serving, do nothing (idempotent).
Option Explicit
Dim sh, fso, proj, cmd, http
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
proj = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))

' already running? (HTTP check on localhost:8501)
On Error Resume Next
Set http = CreateObject("MSXML2.XMLHTTP")
http.Open "GET", "http://localhost:8501/_stcore/health", False
http.Send
If Err.Number = 0 And http.Status = 200 Then
    WScript.Quit 0   ' already serving — do not double-start
End If
On Error GoTo 0

' log rotation at (re)start: keep streamlit.log from growing unbounded (58MB by 2026-07-02).
' Rotation is only safe here — while streamlit runs, the cmd redirect holds the file handle.
' Root cause (use_container_width deprecation spam) is fixed in the app; this is the backstop.
Dim logf, oldf
logf = proj & "\data\streamlit.log"
oldf = proj & "\data\streamlit.log.1"
On Error Resume Next
If fso.FileExists(logf) Then
    If fso.GetFile(logf).Size > 10485760 Then   ' 10MB
        If fso.FileExists(oldf) Then fso.DeleteFile oldf, True
        fso.MoveFile logf, oldf
    End If
End If
On Error GoTo 0

cmd = "cmd /c chcp 65001 >nul && cd /d """ & proj & """ && " & _
      "python -m streamlit run app\streamlit_app.py " & _
      "--server.port 8501 --server.headless true " & _
      ">> data\streamlit.log 2>&1"
' 0 = hidden window, False = don't wait (streamlit runs indefinitely)
sh.Run cmd, 0, False
