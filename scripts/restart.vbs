Option Explicit
' Double-click entry: run restart.bat fully hidden (no black console).
' Examples:
'   restart.vbs
'   restart.vbs lan
'   restart.vbs 8010 lan
'   restart.vbs lan /nobrowser
Dim sh, fso, scriptDir, bat, args, i, cmd

Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
bat = scriptDir & "\restart.bat"

If Not fso.FileExists(bat) Then
  WScript.Echo "restart.bat not found: " & bat
  WScript.Quit 1
End If

args = ""
For i = 0 To WScript.Arguments.Count - 1
  args = args & " " & WScript.Arguments(i)
Next

Set sh = CreateObject("WScript.Shell")
cmd = "cmd.exe /c """ & bat & """" & args
' 0 = hide window, False = do not wait
sh.Run cmd, 0, False