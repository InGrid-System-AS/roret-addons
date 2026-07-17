# Changelog — roret-addons

Alle kundevendte endringer i Produkt 2-modulsettet. Følges av release-tags
(`vX.Y.Z`) i dette repoet; genereres fra Rorets interne monorepo.

Oppgraderingsnotater som krever handling (f.eks. parede `-u`/`-i`-kommandoer)
står under den aktuelle releasen — les dem FØR du bumper submodule-taggen.

## v1.0.1 — 2026-07-17

- Default token-URL og placeholder peker nå på Roret Compliance Gateway
  (`https://api.roret.no/maskinporten-token`) i stedet for den interne
  legacy-tjenesten. Eksisterende selskaper beholder sin lagrede URL —
  kun default for nye selskaper er endret.

## v1.0.0 — 2026-07-17

### Første release
- MVA-melding med elektronisk innsending via ID-porten (BankID) og
  Roret Compliance Gateway; kvittering med verdikt (godkjent/avvist).
- MVA-oppgjørsbilag (2740, hele kroner) — core-only.
- Valgfri OCA-bro for MVA-betaling (KID → pain.001):
  `l10n_no_account_mvamelding_payment` (auto_install).
- Skattemelding for selskap via Altinn 3.
- SAF-T Financial-eksport.
- Auth-lag mot api.roret.no (Maskinporten-systembruker + ID-porten).

**Oppgraderingsnotat (fra kombinert MVA-modul ≤19.0.3.x):** kjør oppgradering
og bro-installasjon i samme kommando, ellers mistes betalingsordre-feltet:

    odoo-bin -d <db> -u l10n_no_account_mvamelding \
             -i l10n_no_account_mvamelding_payment --stop-after-init
