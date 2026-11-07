' Starts the ATM10 Dashboard with no console window, then opens it in the browser.
Set sh = CreateObject("WScript.Shell")
dir = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
sh.Run "pythonw.exe -I """ & dir & "server.py""", 0, False
WScript.Sleep 1800
sh.Run "http://localhost:7870/"
