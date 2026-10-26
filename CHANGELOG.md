# Changelog

## v1.0.0 — 2026-10-23

Eerste publieke versie. Getest in een Kia e-Niro uit 2019.

- `eniro-soc`: SOC, pakspanning en -stroom, celtemperaturen, SOH, energie in/uit,
  kilometerstand en verbruik over 100 km uit de BMS (7E4) en de cluster (7C6).
- Ontvanger (`ingest/`) op de dongle zelf, met één MQTT-verbinding door een
  WireGuard-split-tunnel; profielen *dagelijks* en *reis* voor zuinig 4G-gebruik
  (~0,06 MB/dag gemeten). Ontvanger op een eigen server via HTTPS blijft als
  alternatief.
- `wakelock/`: houdt de dongle wakker tijdens laden en rijden.
- `wwan-teller/`, `diag/`: 4G-verbruik en verbindingsdiagnostiek.
- Home Assistant-packages: sensoren, profiel-helper, meldingen, actieradius, ABRP.
