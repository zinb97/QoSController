#$language = "VBScript"
#$interface = "1.0"

crt.Screen.Synchronous = True

' This automatically generated script may need to be
' edited in order to work correctly.

Sub Main
	crt.Screen.Send "t1 iperf -c 10.0.0.102 -u -i 1 -t 20 -b 360M" & chr(13)
	crt.Screen.WaitForString "netSim> "
	crt.Sleep 20000
	crt.Screen.Send "t1 iperf -c 10.0.0.103 -u -i 1 -t 20 -b 360M" & chr(13)
	crt.Screen.WaitForString "netSim> "
	crt.Sleep 20000
	crt.Screen.Send "t1 iperf -c 10.0.0.104 -u -i 1 -t 20 -b 360M" & chr(13)
	crt.Screen.WaitForString "netSim> "
End Sub
