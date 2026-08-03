# Changelog — roret-addons

Alle kundevendte endringer i Produkt 2-modulsettet. Følges av release-tags
(`vX.Y.Z`) i dette repoet; genereres fra Rorets interne monorepo.

Oppgraderingsnotater som krever handling (f.eks. parede `-u`/`-i`-kommandoer)
står under den aktuelle releasen — les dem FØR du bumper submodule-taggen.

## Slik leser du en oppføring

Hver release følger samme struktur, slik at du kan skumme rett til det som
angår deg:

| Seksjon | Betyr |
|---|---|
| **Krever handling** | Du må gjøre noe utover å bumpe taggen — les før oppgradering |
| **Nytt** | Ny funksjonalitet. Trygt å ta, ingen handling |
| **Fikset** | Feilretting. Trygt å ta |
| **Gateway** | Minstekrav til Roret Compliance Gateway (api.roret.no) |

Seksjoner uten innhold utelates. Står det ingen **Krever handling**, er
oppgraderingen en ren tagg-bump.

Versjonsnumrene betyr:

- **MAJOR** (`v2.0.0`) — kan kreve handling; les alltid
- **MINOR** (`v1.2.0`) — ny funksjonalitet, bakoverkompatibel
- **PATCH** (`v1.1.1`) — feilretting, trygt å ta blindt

`scripts/eksporter_produkt2.sh` nekter å publisere en release som mangler
oppføring her — en udokumentert oppgradering er en oppgradering ingen tør ta.

## v2.0.0 — 2026-07-27

MAJOR fordi releasen har en **Krever handling**-seksjon: lesehjelpen over
sier at MINOR er trygg å bumpe blindt, og det stemmer ikke her. En kunde
som følger tabellen ville tatt oppgraderingen uten å rotere secreten.

**Krever handling**

- Modulen lagrer ikke lenger tjenestens admin-secret. System-parameteren
  `l10n_no_eristo.onboard_admin_secret` **slettes** ved oppgradering, og
  knappen «Onboard hos Eristo» er fjernet fra selskapsskjemaet.

  Grunnen: den secreten er hovednøkkelen til hele kunderegisteret — den
  kan opprette og endre hvilken som helst kunde, ikke bare din. En
  Odoo-admin med server-action-rettigheter kunne lese den ut av
  `ir.config_parameter`. Samme resonnement som holder virksomhets-
  sertifikatet utenfor Odoo-prosessen.

  **Har du hatt en gyldig verdi der, be leverandøren rotere den.**
  Slettingen lukker lagringen, ikke eventuell tidligere eksponering.

  Onboarding av nye selskaper gjøres nå av leverandøren direkte mot
  tjenesten, og du får API-key utlevert. Det er ingen praktisk endring
  for deg: admin-flaten (`/onboard-customer`, `/admin/*`) er uansett
  ikke offentlig eksponert, så knappen kunne aldri fungere fra en Odoo
  utenfor tjenestens eget nett — den ga en uforståelig feil i stedet.

  Oppgraderingen er en vanlig `-u l10n_no_eristo_base`.

**Nytt**

- Aktivering av scopes fra din egen Odoo virker nå mot tjenestens
  offentlige flate. Endepunktet for systembruker-onboarding var tidligere
  stengt utenfra, så «Aktiver»-knappen ga en uforståelig feil for kunder
  med Odoo utenfor tjenestens nett. Ingen endring for deg utover at den
  faktisk fungerer — kunden godkjenner fortsatt i Altinn-portalen.

**Fikset**

- Onboarding-feil forklarer nå hva som er galt. En stengt eller
  feilkonfigurert flate ga «HTTP 404» etterfulgt av en rå HTML-side;
  nå står det hvilken URL som ble forsøkt og hva du skal gjøre.
  Tilsvarende for 403 (avvist forespørsel), som før falt til den
  generiske grenen.

**Gateway**

- Ingen nye krav. Fungerer mot samme gateway-versjon som v1.2.0.
  Forutsetter at tjenesten eksponerer `/onboard-systembruker` — gjelder
  api.roret.no fra og med denne releasen.

## v1.2.0 — 2026-07-27

**Fikset**

- Ordlyd i kommentarer, docstrings og feilmeldinger viste til Supabase,
  som ikke lenger er tjenesten bak `l10n_no_eristo_token_url`. To
  feilmeldinger var direkte villedende: de ba deg sjekke en Supabase-
  secret og endre en rad i Supabase — steder som ikke finnes.
  Ordlyden er nå leverandøruavhengig («tjenesten»), slik at den også
  stemmer for kunder som kjører mot egen instans.
- Dokumentert i `_eristo_onboard_url()` og `_idporten_endpoint()` at ALLE
  avledede endepunkter (onboard, bank, ID-porten) følger automatisk med
  når `l10n_no_eristo_token_url` endres. Endrer du URL-en, må hvert
  endepunkt verifiseres for seg — en tjeneste uten ID-porten-klient
  konfigurert svarer `501`, og da stopper MVA og skattemelding.

Ingen funksjonsendring: verifisert med AST-sammenligning at kontrollflyt,
kall og argumenter er identiske. Ren tagg-bump.

**Gateway**

- Ingen nye krav. Fungerer mot samme gateway-versjon som v1.1.0.

## v1.1.0 — 2026-07-18

- Ny bro-modul `l10n_no_account_mvamelding_payment_batch`: MVA-betaling
  (KID → pain.001) via Enterprise betalingsbunt (account_batch_payment).
  Enterprise-speilet av OCA-broen — auto_install hos Odoo.sh-kunder.
  Betalingen føres mot oppgjørskontoen (2740) med KID som melding;
  bunten eksporteres og godkjennes i nettbanken som vanlig. Ingen
  endring i eksisterende moduler.

**Oppgraderingsnotat (eksisterende database):** auto_install fyrer kun
når avhengighetene INSTALLERES — er kjernen og account_batch_payment
allerede installert (enhver eksisterende Odoo.sh-database), installeres
broen IKKE av en ren `-u`/rebuild (verifisert på staging 2026-07-18).
Kjør eksplisitt:

    odoo-bin -d <db> -i l10n_no_account_mvamelding_payment_batch --stop-after-init

Nyinstallasjoner er uberørt — der fyrer auto_install som normalt.

## v1.0.2 — 2026-07-17

- Bytt deprecated `read_group` → `_read_group` i skattemelding
  (closing + xml-builder). Fjerner DeprecationWarning som gjorde
  Odoo.sh-bygg oransje («Test: Warning»). Ingen funksjonell endring —
  samme saldoer/tall (95 tester grønne).

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
