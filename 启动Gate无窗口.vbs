Option Explicit

Dim shell, fso, root, python, command
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

root = "C:\Users\quesi\Documents\ChatGPT\New project\gate量化"
python = root & "\.venv\Scripts\pythonw.exe"

If Not fso.FileExists(python) Then
    WScript.Quit 1
End If

command = Chr(34) & python & Chr(34) & " -m uvicorn gate_quant.web:app --host 127.0.0.1 --port 8081"
shell.CurrentDirectory = root
shell.Run command, 0, False
