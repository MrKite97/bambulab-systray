# Bambu Lab Systray

## What This Is

Een Windows 11 systeemvak-app (system tray) die de actieve print van een Bambu Lab 3D-printer volgt via de Bambu cloud. De app draait stil op de achtergrond en laat in één oogopslag zien hoe ver de huidige print is: de resterende tijd staat op het tray-icoon, en bij hover toont de tooltip de voortgang in procenten plus de resterende tijd.

## Core Value

Met één blik op de taakbalk weten hoe ver de actieve print is — zonder een app te openen, in te loggen of ergens op te klikken.

## Requirements

### Validated

<!-- Shipped and confirmed valuable. -->

(None yet — ship to validate)

### Active

<!-- Current scope. Building toward these. -->

- [ ] App verbindt met één Bambu Lab printer via het Bambu cloud-account
- [ ] App leest de status van de actieve print uit (voortgang %, resterende tijd)
- [ ] Tray-icoon toont de resterende tijd compact op het icoon zelf
- [ ] Tooltip toont bij hover de voortgang % én de resterende tijd
- [ ] App draait stil op de achtergrond als systray-app (geen vensters)
- [ ] App start automatisch mee met Windows
- [ ] App gaat netjes om met "geen actieve print" (idle/offline staat)

### Out of Scope

<!-- Explicit boundaries. Includes reasoning to prevent re-adding. -->

- Detail-/popup-venster bij klikken — gebruiker wil bewust alleen tooltip, houdt het simpel
- Windows-meldingen (print klaar / mislukt / filament-actie) — gebruiker wil geen pop-ups
- Meerdere printers tegelijk volgen — gebruiker heeft één printer
- Temperaturen, laag x/y, bestandsnaam, thumbnail tonen — niet nodig voor de kernwaarde
- Printer aansturen (pauzeren, stoppen, starten) — alleen-lezen monitoring
- LAN-only / lokale MQTT-modus — gebruiker werkt via cloud-account
- Cross-platform (macOS/Linux) — doel is Windows 11

## Context

- Doel-OS: Windows 11.
- Verbinding: Bambu **cloud-account** (niet lokaal/LAN). De Bambu cloud heeft geen officiële publieke API; toegang loopt via account-authenticatie en een MQTT-stroom over TLS. Bestaande integraties (bijv. `bambu-connect`, Home Assistant Bambu Lab-integratie, `pybambu`) zijn reverse-engineered referenties.
- Interactiemodel is bewust minimaal: passieve, altijd-zichtbare indicator. Geen UI-chrome.
- Een tray-icoon is 16×16 px; "resterende tijd op het icoon" vereist compacte rendering (bijv. `1:23`). Exacte weergavevorm wordt in de planning bepaald.

## Constraints

- **Platform**: Windows 11 systray-app — moet als achtergrond-app in het systeemvak leven.
- **Connectiviteit**: Bambu cloud (geen LAN) — afhankelijk van Bambu's (ongedocumenteerde) cloud-auth en MQTT-stroom; risico op wijzigingen aan Bambu's kant.
- **Authenticatie**: vereist Bambu-accountgegevens; mogelijk 2FA/region-handling en veilige opslag van tokens.
- **Footprint**: lichtgewicht, lage CPU/geheugen — draait permanent op de achtergrond.
- **Scope**: bewust klein en alleen-lezen; geen printeraansturing.

## Key Decisions

<!-- Decisions that constrain future work. Add throughout project lifecycle. -->

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Verbinden via Bambu cloud i.p.v. LAN | Gebruiker werkt met cloud-account, printer niet lokaal benaderd | — Pending |
| Alleen tooltip, geen detail-venster | Gebruiker wil bewust minimale, simpele indicator | — Pending |
| Geen Windows-meldingen | Gebruiker wil geen pop-ups, alleen passieve indicator | — Pending |
| Resterende tijd op icoon, % + tijd in tooltip | Icoon te klein voor alles; tijd is de meest waardevolle losse waarde | — Pending |
| Autostart met Windows | App moet altijd op de achtergrond draaien | — Pending |
| GSD licht/Coarse uitvoeren | Klein project, maar cloud-koppeling rechtvaardigt research-de-risking | — Pending |

## Evolution

This document evolves at phase transitions and milestone boundaries.

**After each phase transition** (via `/gsd-transition`):
1. Requirements invalidated? → Move to Out of Scope with reason
2. Requirements validated? → Move to Validated with phase reference
3. New requirements emerged? → Add to Active
4. Decisions to log? → Add to Key Decisions
5. "What This Is" still accurate? → Update if drifted

**After each milestone** (via `/gsd-complete-milestone`):
1. Full review of all sections
2. Core Value check — still the right priority?
3. Audit Out of Scope — reasons still valid?
4. Update Context with current state

---
*Last updated: 2026-06-20 after initialization*
