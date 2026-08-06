# Roret-moduler for norsk compliance i Odoo 19

Norsk MVA-melding, skattemelding og SAF-T for Odoo 19 — **Community og
Enterprise/Odoo.sh** — med elektronisk innsending til Skatteetaten via
Altinn 3 og ID-porten.

Settet inneholder i tillegg `roret_mcp_kobling`, som kobler din Odoo til
Roret-agenten. Den er ikke compliance og kan installeres uavhengig av
resten.

> **Dette repoet er en distribusjon.** Innholdet genereres automatisk fra
> Rorets interne monorepo ved hver release — pull requests tas ikke imot her.
> Feil og ønsker: kontakt Roret-support (eller åpne en issue her).

## Hva modulene gjør

| Modul | Funksjon |
|---|---|
| `l10n_no_account_mvamelding` | MVA-melding: generering fra Tax Report, elektronisk innsending, kvittering med verdikt (godkjent/avvist), MVA-oppgjørsbilag |
| `l10n_no_account_mvamelding_payment` | Valgfri bro: MVA-betaling (KID → pain.001) via OCA betalingsordre. Installeres automatisk der OCA bank-payment finnes |
| `l10n_no_account_skattemelding` | Skattemelding for selskap: innsending via Altinn 3 |
| `l10n_no_saft` | SAF-T Financial-eksport (lovpålagt) |
| `l10n_no_account_mvamelding_payment_batch` | Samme bro for Enterprise/Odoo.sh: MVA-betaling via `account_batch_payment`. Installeres automatisk der den finnes |
| `l10n_no_eristo_base` / `l10n_no_eristo_idporten` | Auth-laget mot Roret Compliance Gateway: Maskinporten/systembruker og ID-porten (BankID) |
| `roret_mcp_kobling` | «Koble til Roret»-knapp: kobler din Odoo til Roret-agenten. Nøkkelen lages i din egen Odoo, tilhører din bruker, og kan trekkes tilbake av deg. Den lagres kryptert hos Roret. Avhenger kun av `base` |

## Forutsetninger

- **Odoo 19** (Community eller Enterprise) — gjelder alle modulene i settet.

Resten av lista gjelder kun **compliance-modulene** (MVA, skattemelding,
SAF-T). `roret_mcp_kobling` trenger ingen av dem: den avhenger kun av `base`,
snakker med `mcp.roret.no` og ikke med compliance-gatewayen, og har ingen
BankID-flyt. Den kan installeres alene.

- Norsk kontoplan (`l10n_no`).
- **Roret-kundeavtale**: innsendingen går via Roret Compliance Gateway
  (`https://api.roret.no`) og krever en API-nøkkel per organisasjon.
  Virksomhetssertifikatet driftes av Roret — du trenger ikke eget.
- MVA-innsending autentiseres med **BankID** (ID-porten) av personen som
  sender inn — dette er Skatteetatens krav til sluttbrukersystemer.

## Installasjon (Odoo.sh)

Legg dette repoet som git-submodule i Odoo.sh-repoet ditt, **pinnet til en
release-tag** (aldri flytende main):

```sh
git submodule add https://github.com/InGrid-System-AS/roret-addons.git roret-addons
cd roret-addons && git checkout <SISTE-TAG> && cd ..
git add .gitmodules roret-addons && git commit -m "Roret-moduler <TAG>"
```

Odoo.sh plukker opp undermapper automatisk. Selvhostet: legg katalogen i
`addons_path`.

Konfigurer deretter **compliance-modulene**: Innstillinger → Selskaper →
Skatteetaten-tilkobling — gateway-URL og API-nøkkelen fra Roret. Dette
krever administratorrettigheter og gjøres én gang per selskap.

`roret_mcp_kobling` konfigureres **ikke** der. Den kobles av hver enkelt
ansatt selv, fra **Roret → Koble til Roret**, med en engangskode agenten
gir deg. Ingen administrator er involvert: nøkkelen lages i din egen Odoo,
tilhører din bruker, og du kan koble fra igjen fra samme skjerm når som
helst — forutsatt at `web.base.url` allerede peker på adressen dere
faktisk bruker. Gjør den ikke det, stopper modulen tilkoblingen, og det
parameteret kan bare en administrator rette. På Odoo.sh settes den
riktig av seg selv; selvhostet er den verdt å sjekke først.

## Oppgradering

Sjekk CHANGELOG.md, bump submodule-taggen på en **staging-branch** mot en
kopi av produksjonsdata, verifiser, og merge. Kjør modul-oppgradering med
eventuelle par-kommandoer beskrevet i CHANGELOG-en for releasen (enkelte
releaser krever `-u` og `-i` i samme kjøring).

## Lisens

LGPL-3 — se [LICENSE](LICENSE). Modulene er fri programvare; innsendings-
tjenesten (Roret Compliance Gateway) er en egen avtale.
