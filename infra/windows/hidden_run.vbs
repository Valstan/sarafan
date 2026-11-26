' ----- ------: ---------- ---- -- --------- -----.
'
' ----- --- --------- ------. ------ ------------ Windows -- ---- ------
' ----------- - ------ --------- (--- ----- Interactive - ----- - --------
' -- ----- ----, - VK - DeepSeek --- ---- -- --------). ----- ----------
' --------- - ---- ------- (powershell -> python -> CLI ------) ------- --
' ------ ----; `-WindowStyle Hidden` ------ ---, -- -- -------- -------- -
' -- ------- ------- --- ----- --- -------.
'
' wscript.exe - ---------- --- -------, ------- --------- ------- -
' ------- ------, - ---- -- ---------- ------.
'
' -------------: wscript.exe //nologo hidden_run.vbs "<---- - .cmd>"
Option Explicit

Dim shell, fso, scriptPath, command

If WScript.Arguments.Count < 1 Then
    WScript.Quit 2
End If

scriptPath = WScript.Arguments(0)
Set fso = CreateObject("Scripting.FileSystemObject")
If Not fso.FileExists(scriptPath) Then
    WScript.Quit 3
End If

Set shell = CreateObject("WScript.Shell")
' ------ -------- 0 - ------- ----; ------ True - ---- ----------.
' --- -------- ------ ------------ -----, --- ------- ----, - ------------
' --- - ----- (- ---- ------ --- --- - ---- ---------- --------).
command = "cmd.exe /c """ & scriptPath & """"
shell.Run command, 0, True

WScript.Quit 0
