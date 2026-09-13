Option Explicit
Dim shell, command
Set shell = CreateObject("WScript.Shell")
command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""C:\Users\quesi\Documents\ChatGPT\New project\gate-quant\scripts\ensure_gate_running.ps1"""
shell.Run command, 0, True
