Option Explicit
' Launch serve.ps1 with a fully hidden console (window style 0).
' Args: <mode> <port>   e.g. local 8006 / lan 8006
Dim sh, fso, mode, port, scriptDir, servePs1, cmd

If WScript.Arguments.Count < 2 Then
  WScript.Echo "Usage: restart_serve.vbs <mode> <port>"
  WScript.Quit 1
End If

mode = WScript.Arguments(0)
port = WScript.Arguments(1)

If StrComp(mode, "lan", vbTextCompare) <> 0 And StrComp(mode, "local", vbTextCompare) <> 0 Then
  WScript.Echo "Invalid mode: " & mode & " (use local or lan)"
  WScript.Quit 1
End If

Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
servePs1 = scriptDir & "\serve.ps1"

If Not fso.FileExists(servePs1) Then
  WScript.Echo "serve.ps1 not found: " & servePs1
  WScript.Quit 1
End If

Set sh = CreateObject("WScript.Shell")
cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & servePs1 & """ -Mode " & mode & " -Port " & port
' 0 = hide window, False = do not wait for process exit
sh.Run cmd, 0, False