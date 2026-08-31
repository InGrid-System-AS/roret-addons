# Roret — koble til AI-agenten

Én knapp i kundens egen Odoo som kobler den til Roret-agenten, uten at noe
menneske håndterer en API-nøkkel og uten at noen åpner en terminal.

## Flyten

```
Kundens Odoo                     Roret
[Koble til Roret] --klikk-->
                                 Keycloak-innlogging (kunden selv)
                            <--- kortlevd, engangs paringskode
kode limes inn ------------>
  _generate(...)  server-side
  POST /onboard {kode, odoo_url, bruker, nøkkel} --> MCP
                                 kode -> (tenant, sub) -> KEK -> lagre
                            <--- {"status": "ok"}   (aldri nøkkelen tilbake)
```

Brukeren ber agenten om å koble til Odoo. Agenten kaller `koble_til_odoo`, som
utsteder en kode bundet til brukerens Keycloak-prinsipal. Koden limes inn her.
Modulen minter en API-nøkkel som tilhører **den innloggede brukeren**, sender
den til Roret, og kaster klarteksten.

## Hvorfor retningen er som den er

Agenten utsteder koden, Odoo innløser den — ikke omvendt. Motsatt vei ville et
gjettet treff BUNDET KUNDENS NØKKEL TIL ANGRIPERENS prinsipal, altså tyveri.
Slik det er nå, er det verste et treff kan gi at angriperens egen Odoo havner
på offerets agent — redirigering. Begge er alvorlige; den ene er verre.

## Hvem nøkkelen tilhører

Den som klikket. `res.users.apikeys._generate` binder raden til
`self.env.user.id`, og `sudo()` i `_mint_nokkel` endrer ikke `uid` — bare
`su`-flagget. Det er hele grunnen til at MCP-en lagrer per `(tenant, bruker)`:
revisjonssporet i kundens Odoo skal peke på et menneske.

`sudo()` er der for én ting: `_check_expiration_date` returnerer tidlig når
`env.is_system()`, slik at nøkkelen kan være permanent. Uten det MÅ nøkkelen ha
en utløpsdato, begrenset av `max(gruppenes api_key_duration)` — og
`base.group_user` setter 90 dager. Koblingen ville virket gjennom hele
innføringen og sluttet å virke et kvartal senere, når ingen lenger forbinder
feilen med oppsettet.

## Konfigurasjon

| Systemparameter | Default | Hva det gjør |
| --- | --- | --- |
| `roret_mcp.endepunkt` | `https://mcp.roret.no` | Hvor nøkkelen sendes. Må være https. |
| `web.base.url` | (Odoos egen) | Adressen Roret når denne Odoo-en på. Må være https og nåbar utenfra. |

Arbeidsdelingen er tilsiktet: **administratoren bestemmer hvor nøkler går, den
enkelte brukeren bestemmer om hens egen nøkkel skal dit.** Bare en
Settings-bruker kan endre parameteret; enhver intern bruker kan koble seg til.

## Koble fra

Samme skjerm. Sletter brukerens Roret-nøkkel, og koblingen er død umiddelbart —
en nøkkel som ikke finnes i denne basen autentiserer ingenting.

**Dette er halve frakoblingen.** Raden hos Roret — kryptert kopi av nøkkelen,
URL og brukernavn — tømmes ved å be agenten «koble fra Odoo»
(`koble_fra_odoo` i MCP-en). Knappen her kan ikke gjøre det selv: den har
ingenting å bevise hvem den er med, mens agent-verktøyet autoriseres av
brukerens eget Keycloak-token. Kvitteringen minner om steget.

### Hvis tilkoblingen avbrytes på feil tidspunkt

Ryker forbindelsen *etter* at Roret har lagret den nye nøkkelen, men før svaret
kommer tilbake, fjerner vi den nye nøkkelen lokalt og beholder den gamle. Roret
kjenner da en nøkkel som ikke finnes her, og vi har en Roret ikke kjenner —
koblingen er død, og paringskoden er brukt opp. Løsningen er å be agenten om en
ny kode og koble til på nytt. Det er en to-fasers-commit-svakhet ingen av sidene
kan lukke alene.

## Odoo.sh: databasenavnet endres ved rebuild

Modulen sender `odoo_discover_database` sammen med det navnet som gjelder nå.
Er `l10n_no_eristo_base` installert, serverer den `/eristo/env-info`, og Roret
slår opp navnet i runtime. Er den ikke installert, finnes ruten ikke, og det
innsendte navnet er det Roret bruker.

⚠ **Oppslaget redder bare db-navnet, ikke verten.** Odoo.sh kobler BÅDE
build-subdomenet og databasenavnet til build-id-en, og begge bytter ved
rebuild. Peker `web.base.url` på selve build-adressen
(`<prosjekt>-<branch>-<id>.dev.odoo.com`), er verten like flyktig — da går
oppslaget til en adresse som ikke lenger finnes, `_discover_database`
returnerer `None`, og koblingen er død likevel. Forutsetningen er et
**stabilt custom-domene**; det er premisset endepunktet ble laget under, og
det står i dets egen docstring. På en Odoo.sh-PRODUKSJONSbranch er
db-navnet stabilt, så der løser dette et problem instansen ikke har.

Navnet sendes ALLTID, også når oppslag er slått på: oppslaget er feiltolerant
i Roret-enden og faller tilbake på det innsendte navnet hvis ruten ikke
svarer. Uten dette ville hele bedriftens koblinger dødd samtidig ved neste
rebuild, uten at noen hadde rørt noe.

## Avhengigheter

`base`, ingenting annet — med vilje. Modulen skal kunne installeres i en
hvilken som helst Odoo 19, Community eller Enterprise/Odoo.sh, uten at du må ta
inn OCA-moduler eller noe annet fra Roret.
