' Runs its arguments as one command line with no window (style 0); child processes (git) inherit the hidden console.
Dim a, i, cmd
Set a = WScript.Arguments
For i = 0 To a.Count - 1
  If InStr(a(i), " ") > 0 Then cmd = cmd & """" & a(i) & """ " Else cmd = cmd & a(i) & " "
Next
WScript.Quit CreateObject("WScript.Shell").Run(cmd, 0, True)
