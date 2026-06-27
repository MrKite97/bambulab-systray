# Bambu Lab Systray

A lightweight **Windows 11 system-tray app** that follows the active print of a
Bambu Lab 3D printer over the Bambu **cloud**. The remaining time is drawn right
on the tray icon; hovering shows progress (%) + remaining time; left-clicking the
icon opens a small flyout panel with live status (layer, file, nozzle/bed temps)
and read-only controls. It runs quietly in the background — one glance at the
taskbar tells you how far the print is, without opening an app or logging in.

> **Cloud-only, read-only monitor.** Uses your Bambu account (no LAN access code).
> The access token is stored encrypted in the Windows Credential Manager (DPAPI) —
> never in a plaintext file, and your password is never persisted.

---

## Installeren (aanbevolen)

1. Ga naar de **[Releases](../../releases/latest)** pagina van deze repository.
   <!-- Replace ../../releases/latest with the full URL once the repo is public. -->
2. Download het installatiebestand **`BambuLabSystray-Setup-<versie>.exe`**.
3. Dubbelklik het. De installatie is **per gebruiker** — er is **géén
   admin/UAC-prompt**. Je krijgt een snelkoppeling in het Startmenu en de optie
   om de app **met Windows mee te laten opstarten** (standaard aangevinkt).

### "Windows heeft uw pc beveiligd" (SmartScreen)

De app is **niet ondertekend** (code-signing is bewust uitgesteld voor deze
versie), dus Windows SmartScreen toont bij de eerste keer een blauw venster
**"Windows heeft uw pc beveiligd"** met **"Onbekende uitgever"**. Dat is hier te
verwachten — het is geen virus. Zo ga je verder:

1. Klik op **Meer info**.
2. Klik op de knop **Toch uitvoeren** die dan verschijnt.

![SmartScreen: Meer info → Toch uitvoeren](docs/img/smartscreen.png)
<!-- Screenshot toegevoegd bij de eerste echte run op een desktop. De stappen
     hierboven zijn leidend, ook zonder de afbeelding. -->

De installatie gaat daarna gewoon verder.

---

## Gebruiken

- **Tray-icoon** — toont de resterende tijd van de actieve print. Hover voor
  voortgang (%) + resterende tijd.
- **Linksklik op het icoon** — opent/sluit het flyout-paneel met live status.
- **Eerste keer inloggen** — gebeurt **in het paneel**: e-mailadres + wachtwoord,
  daarna de e-mailverificatiecode. Daarna kies je je printer.
- **Rechtsklik op het icoon** — **"Met Windows opstarten"** aan/uit en
  **"Afsluiten"**.
- **Updates** — de app controleert stil op nieuwere releases en toont een
  discrete banner in het paneel (+ eenmalig een tray-melding). Via **"Nu
  bijwerken"** werkt de app zichzelf bij (download → SHA-256-controle → stille
  installatie → herstart), met behoud van je login en instellingen. Je kunt een
  versie overslaan, de melding wegklikken ("Later"), of de automatische controle
  uitzetten.

## De-installeren

Via **Instellingen → Apps → Geïnstalleerde apps → Bambu Lab Systray →
Verwijderen**. De snelkoppeling, het programma en de autostart-waarde worden
opgeruimd. Je **blijft ingelogd** voor een volgende installatie: het token
(Credential Manager) en je instellingen (regio/serienummer in `%APPDATA%`)
blijven staan.

---

## Vanuit broncode draaien (ontwikkelaars)

### Vereisten

- Windows 11
- Python 3.12 (gebruik het `python`-commando, niet `python3`)
- Microsoft Edge **WebView2** runtime (standaard aanwezig op Windows 11) — nodig
  voor het flyout-paneel.

### Setup (PowerShell)

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### Draaien

```powershell
python -m src.app
```

Inloggen gebeurt in het paneel (e-mail → verificatiecode), net als in de
gepackagede app.

### Zelf bouwen / een release maken

Zie **[BUILD.md](BUILD.md)** voor het bouwen van de `.exe`, het compileren van de
Inno Setup-installer, en het tag-gestuurde release-proces (een `v*`-tag triggert
de GitHub Actions build die de installer + SHA-256 publiceert).

### Tokenopslag

De Bambu access-token wordt versleuteld opgeslagen in de **Windows Credential
Manager** (DPAPI, per gebruiker) onder de servicenaam `BambuLabSystray`. De token
wordt **nooit** naar een plaintext-bestand geschreven, en het wachtwoord wordt
nooit bewaard — alleen de access-token.

## Licentie

[MIT](LICENSE) © 2026 Maarten Vlieger.
