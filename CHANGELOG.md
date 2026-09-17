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

## v2.2.2 — 2026-09-17

PATCH: kun testkode, ingen handling.

**Fikset**

- **Odoo.sh markerte bygget «Test: Failed» selv med alle tester grønne.**
  Én test i `l10n_no_eristo_base` lot wizardens advarsel med traceback gå
  rett i byggeloggen, og Odoo.sh flagger enhver traceback. Testen hevder
  nå advarselen i stedet. Med v2.2.1 og denne skal et Odoo.sh-bygg med
  Roret-modulene stå grønt når testene er grønne.

## v2.2.1 — 2026-09-17

PATCH: feilretting, ingen handling utover å bumpe taggen.
`l10n_no_account_mvamelding` har hevet modulversjon (19.0.4.1.0 → 19.0.4.2.0),
så oppgraderingen kjører av seg selv og legger inn de nye tilgangsreglene.

**Fikset**

- **MVA-oppgjøret feilet med «Fant ingen MVA-kontoer (use_in_tax_closing)
  for … — er den norske kontoplanen installert?» selv om kontoplanen var i
  orden.** Meldingen var synlig og klikkbar fra et annet selskap enn sitt
  eget, mens tallgrunnlaget bak knappen fulgte Odoos vanlige selskapsregler
  og var tomt derfra. Nå har MVA-meldingen og merknadene samme
  selskapsregel som resten av regnskapet: de er synlige når meldingens
  selskap er blant de aktive. Skjemaet lar seg dermed ikke åpne fra feil
  selskap, og Odoos egen melding sier hvilket selskap du må bytte til.
  Oppgjørsbilaget bruker i tillegg kun meldingens eget selskap ved
  kontooppslag, så delte kontoer fra et søsterselskap kan ikke havne i det.

- **Merknad-fanen sorterer mva-kodene numerisk** (1, 3, 11, 12 …), ikke som
  tekst (1, 11, 12, 13, 3 …).

**Nytt**

- **Fasitfilene under `tests/golden/` følger ikke lenger med.** De er ekte
  meldinger Rorets egne selskaper har sendt til Skatteetaten, og
  distribusjonsrepoet er offentlig. Testen som bruker dem hopper over seg
  selv hos deg; alle andre tester er med som før og kjører på egen Odoo
  som før.

- **Testene som med vilje utløser feilstier logger ikke lenger ERROR.**
  Odoo.sh markerer et bygg rødt på ERROR-linjer alene, også når alle
  tester er grønne. Feilmeldingstestene i `l10n_no_eristo_base` hevder nå
  loggen i stedet for å slippe den gjennom, så et rødt Odoo.sh-bygg igjen
  betyr at noe faktisk feilet.

## v2.2.0 — 2026-08-31

MINOR: to feilrettinger i mva-melding, pluss det nye merknad-feltet de
krevde. Ingen handling utover å bumpe taggen — modulversjonen er hevet,
så oppgraderingen kjører av seg selv.

Begge feilene ble avdekket ved første reelle produksjonskjøring av
MVA-flyten (InGrid System AS og Eristo AS, 3. termin 2026).

**Fikset**

- **MVA-oppgjøret lot seg ikke bokføre for terminer med øre.**
  Avrundingslinjen fikk feil fortegn, slik at bilaget summerte til
  `2 × avrundingsdifferansen` i stedet for null og ble avvist med «The
  entry is not balanced». Skatteetaten fastsetter i hele kroner mens
  MVA-kontoene nesten alltid har øre, så feilen traff i praksis hver
  eneste termin — den var usynlig fordi den eneste testen som bokførte
  et oppgjør brukte en fikstur på eksakt 150 kr, der grenen aldri kjøres.
  Dekket nå av tester i begge avrundingsretninger.

- **Meldinger som tilbakefører inngående merverdiavgift ble avvist av
  Skatteetaten.** Når en termins tilbakeføringer overstiger dens egne
  fradrag, får fradragskoden motsatt fortegn, og Skatteetatens regel
  **R021** krever da en merknad som forklarer hvorfor. Modulen kunne ikke
  produsere `merknad`-elementet, så meldingen ble avvist med
  alvorlighetsgrad `UGYLDIG_SKATTEMELDING` — den ble altså ikke fastsatt.
  Typisk tilfelle: retting av uberettiget fradragsført MVA fra en
  tidligere termin.

**Nytt**

- **Merknader per spesifikasjonslinje.** Ny fane «Merknader» på
  mva-meldingen der du kan skrive en fritekstforklaring knyttet til en
  bestemt mva-kode. Forklaringen sendes til Skatteetaten sammen med
  linjen, og er formulert i selskapets navn — skriv den som du ville
  forklart forholdet til en saksbehandler.

- **R021 fanges før innsending.** «Send inn» stopper nå med en forklarende
  melding hvis en linje mangler påkrevd merknad, i stedet for at du bruker
  BankID på en innsending Skatteetaten garantert avviser. «Generer XML»
  varsler allerede ved generering. Legger du merknaden inn etter at XML-en
  er generert, sier meldingen fra at du må generere på nytt — merknaden er
  ikke i payloaden før da.

**Koble til Roret**

Releasen tar også med endringer i `roret_mcp_kobling` som har landet siden
v2.1.0:

- Frakobling fjerner nå raden hos Roret, ikke bare nøkkelen i din egen Odoo.
  Tømmingen skjer med `UPDATE` — tjenesten har ingen `DELETE`-grant.
- MCP-verktøyene har fått norske navn (engelsk kode bak). Bruker du dem ved
  navn fra en agent, heter frakoblingen nå `koble_fra_odoo`.

## v2.1.0 — 2026-08-05

MINOR, ikke MAJOR: releasen har ingen **Krever handling**-seksjon. Den nye
modulen er valgfri og installeres ikke av seg selv — gjør du ingenting,
endrer ingenting seg, og taggen kan bumpes blindt slik lesehjelpen lover.
(Til forskjell fra v2.0.0, der en systemparameter ble slettet ved
oppgradering uansett hva du gjorde.)

**Nytt**

- `roret_mcp_kobling` — «Koble til Roret»-knapp for AI-agenten.

  Erstatter prosedyren der en Roret-ansatt tok imot API-nøkkelen din og
  registrerte den manuelt. Nå gjør du det selv, uten terminal og uten at
  nøkkelen går gjennom hendene på en Roret-ansatt:

  1. Be Roret-agenten koble til Odoo. Den gir deg en kode.
  2. Åpne menyen **Roret → Koble til Roret** i din egen Odoo.
  3. Lim inn koden og klikk «Koble til».

  Modulen lager en API-nøkkel som tilhører **deg** — den som klikker —
  sender den til Roret, og kaster klarteksten. Hos Roret lagres den
  kryptert. Agenten får dermed
  nøyaktig de rettighetene du selv har, og alt den gjør står på deg i
  revisjonssporet. Det var hele grunnen til å endre flyten: da en
  operatør håndterte nøkkelen, pekte sporet på den som tilfeldigvis
  leverte den.

  Installeres med `-i roret_mcp_kobling` når du vil ta den i bruk.
  Avhenger kun av `base`, så den kjører på både Community og Odoo.sh.

  **På Odoo.sh tåler koblingen rebuilds — på en branch med stabilt
  domene.** Databasenavnet på staging- og dev-builds endres ved hver
  rebuild, så modulen ber Roret om å slå opp navnet i runtime i stedet
  for å stole på det som gjaldt da du koblet til. Det krever to ting:
  `l10n_no_eristo_base` (den serverer oppslagsruten), og at
  `web.base.url` peker på et custom-domene som overlever rebuilden —
  peker den på selve build-adressen, bytter verten også, og da er det
  ingenting å slå opp mot. Har du ingen av delene, brukes navnet fra
  tilkoblingen, og det er riktig både for en instans med fast
  databasenavn og for Odoo.sh-produksjon.

  Menyen ligger på toppnivå og ikke under Innstillinger, med vilje:
  Innstillinger krever administratorrettigheter, og hver ansatt skal
  kunne koble seg selv.

  **Koble fra** ligger på samme skjerm. Den sletter nøkkelen din, og
  agenten mister tilgangen umiddelbart.

  Konfigurasjon (systemparametere, endres av en administrator):

  | Parameter | Default | Hva det gjør |
  |---|---|---|
  | `roret_mcp.endepunkt` | `https://mcp.roret.no` | Hvor nøkkelen sendes. Må være https. |
  | `web.base.url` | (Odoos egen) | Adressen Roret når din Odoo på. Må være https og nåbar utenfra. |

  Arbeidsdelingen er tilsiktet: administratoren bestemmer **hvor** nøkler
  går, den enkelte bestemmer **om** hens egen nøkkel skal dit.

**Gateway**

- Ingen endring. Modulen snakker med Roret-agenten (`mcp.roret.no`), ikke
  med compliance-gatewayen.

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
