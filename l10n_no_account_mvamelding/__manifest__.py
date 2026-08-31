{
    'name': 'Norway - MVA-melding (VAT Return)',
    'version': '19.0.4.1.0',
    'category': 'Accounting/Localizations',
    'summary': 'MVA-melding-innsending til Skatteetaten via Altinn 3 (ID-porten)',
    'description': """
Generer og send den moderniserte mva-meldingen (skattemelding for
merverdiavgift, gjeldende fra 2022) elektronisk til Skatteetaten —
helautomatisk via Maskinporten-systembruker, slik Fiken/Tripletex gjør.

Tallene hentes fra den norske Tax Report-DEFINISJONEN (account.report
l10n_no.tax_report — community), hvis rapportlinjer allerede er kodet med
Skatteetatens standard mva-koder (3, 31, 33, 1, 11, 13, 81–92, …).
Community/OCA-utgave: beregningen gjøres av modulen selv (tag-summering
med Odoo 19-semantikk) siden Enterprise-motoren (account_reports) ikke er
tilgjengelig. Hver linje med verdi ≠ 0 blir én mvaSpesifikasjonslinje —
ingen manuell mapping-tabell.

Oppgjør (erstatter Enterprise account.return-flyten):
  * «Bokfør MVA-oppgjør» tømmer MVA-kontoene mot oppgjørskontoen (2740)
    i hele kroner, øre-rest til avrundingskonto
  * Betalingsinformasjonen (beløp/KID/konto/frist) fra kvitteringen vises
    i Betaling-fanen. Selve betalingsordre-flyten (KID → pain.001 via OCA
    account_payment_order) ligger i bro-modulen
    l10n_no_account_mvamelding_payment — denne kjernen er core-only og
    kjører på enhver Odoo 19 (Community og Enterprise/Odoo.sh)

Auth: gjenbruker l10n_no_eristo_base for Maskinporten-tokens. Innsending
mot Altinn 3-appen bruker token-scope altinn:instances.write; systembrukeren
delegeres Altinns tilgangspakke `merverdiavgift` (MVA-app-ressursen er
delegable:false → delegering MÅ gå via tilgangspakke). Onboarding-nøkkel:
skatteetaten:mvamelding.

API-flyt:
  1. Bygg mva-melding-XML (mvaMeldingDto) + konvolutt (mvaMeldingInnsending)
     fra Tax Report-verdiene for terminen
  2. Opprett Altinn 3-instans (TT02: skd/mva-melding-innsending-test, prod -v1)
  3. PUT konvolutt + POST mva-melding
  4. PUT process/next ×2 (fullfør utfylling + innsending, helautomatisk).
     Altinn-appen kjører selv valideringen og returnerer 409 + avvik ved feil
     (eget frittstående validerings-kall er bevisst utelatt).
  5. Poll → hent betalingsinformasjon + kvittering

Status (2026-06): ende-til-ende verifisert mot TT02 (alminnelig mva-melding,
bimånedlige terminer).
""",
    'author': 'Eristo AS',
    'website': 'https://eristo.no',
    'depends': [
        'account',
        'mail',
        'l10n_no',
        'l10n_no_eristo_base',
        # ID-porten-person-innsending (default modus): Skatteetatens
        # behandlingsløp henter ikke systembruker-innsendinger per 2026-07
        # — se docs/mva-systembruker-funn.md.
        'l10n_no_eristo_idporten',
        # NB: bevisst INGEN OCA-avhengigheter — modulen skal kjøre på enhver
        # Odoo 19 (Community, Enterprise/Odoo.sh). Betalingsordre-flyten
        # (KID → pain.001 via OCA account_payment_order) ligger i bro-modulen
        # l10n_no_account_mvamelding_payment (auto_install når OCA finnes).
    ],
    'data': [
        'security/ir.model.access.csv',
        'data/ir_cron_data.xml',
        'views/l10n_no_mvamelding_views.xml',
        'views/res_company_views.xml',
    ],
    'installable': True,
    'auto_install': False,
    'license': 'LGPL-3',
}
