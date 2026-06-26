' Vensterloze launcher voor de Bambu Lab systray-app.
'
' Waarom dit bestaat: run-app.cmd draait Python IN een console-venster, dus de
' app is een kind-proces van dat venster -- sluit je het venster, dan sluit de
' app mee. Sinds de panel-login (Plan 08) leest de app niets meer van de console,
' dus dat venster heeft geen functie meer.
'
' Deze VBS start .venv\Scripts\pythonw.exe (de VENSTERLOZE Python) verborgen en
' losgekoppeld: geen console, niets om per ongeluk te sluiten. Het tray-icoon
' verschijnt rechtsonder; rechtsklik -> Afsluiten om te stoppen.
'
' Voor foutopsporing: gebruik run-app.cmd -- dat houdt het console-venster open
' en toont eventuele opstartfouten.

Dim fso, sh, base, pyw
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")

base = fso.GetParentFolderName(WScript.ScriptFullName)
pyw = base & "\.venv\Scripts\pythonw.exe"

If Not fso.FileExists(pyw) Then
    MsgBox ".venv niet gevonden in:" & vbCrLf & base & vbCrLf & vbCrLf & _
           "Maak hem aan met:" & vbCrLf & _
           "    python -m venv .venv" & vbCrLf & _
           "    .venv\Scripts\python.exe -m pip install -r requirements.txt", _
           vbExclamation, "Bambu Lab systray"
    WScript.Quit 1
End If

' Werkmap = projectmap, zodat "-m src.app" het pakket vindt.
sh.CurrentDirectory = base

' Run-argumenten: 0 = venster verborgen, False = niet wachten (detached).
sh.Run """" & pyw & """ -m src.app", 0, False
