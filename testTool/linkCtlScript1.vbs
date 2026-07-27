#$language = "VBScript"
#$interface = "1.0"

crt.Screen.Synchronous = True

' This automatically generated script may need to be
' edited in order to work correctly.

Sub Main
	crt.Screen.Send "link C1 A4 down" & chr(13)
	crt.Screen.WaitForString "netSim> "
	crt.Sleep 15000
	crt.Screen.Send "link C1 C3 down" & chr(13)
	crt.Screen.WaitForString "netSim> "
	crt.Sleep 15000
	crt.Screen.Send "link C1 A4 up" & chr(13)
	crt.Screen.WaitForString "netSim> "
	crt.Sleep 15000
	crt.Screen.Send "link C1 C3 up" & chr(13)
	crt.Screen.WaitForString "netSim> "
	crt.Sleep 15000
	
	crt.Screen.Send "link C1 A4 down" & chr(13)
	crt.Screen.WaitForString "netSim> "
	crt.Sleep 15000
	crt.Screen.Send "link C1 C2 down" & chr(13)
	crt.Screen.WaitForString "netSim> "
	crt.Sleep 15000
	crt.Screen.Send "link C1 A4 up" & chr(13)
	crt.Screen.WaitForString "netSim> "
	crt.Sleep 15000
	crt.Screen.Send "link C1 C2 up" & chr(13)
	crt.Screen.WaitForString "netSim> "
End Sub
