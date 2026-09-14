Option Explicit

Dim shell, fso, root, script, command
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

root = "C:\Users\quesi\Documents\ChatGPT\New project\gate量化"
script = root & "\scripts\ensure_gate_running.ps1"

If Not fso.FileExists(script) Then
    WScript.Quit 1
End If

command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File " & Chr(34) & script & Chr(34)
shell.CurrentDirectory = root
shell.Run command, 0, False
