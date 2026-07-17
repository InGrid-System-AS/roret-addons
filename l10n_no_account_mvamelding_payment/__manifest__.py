{
    'name': 'Norway - MVA-melding betalingsordre (OCA)',
    'version': '19.0.1.0.0',
    'category': 'Accounting/Localizations',
    'summary': 'MVA-betaling (KID → pain.001) via OCA betalingsordre',
    'description': """
Bro-modul mellom MVA-melding-kjernen (l10n_no_account_mvamelding, core-only)
og OCA bank-payment-stacken.

Legger «Opprett betaling»-steget på MVA-meldingen: KID-betalingen fra
Skatteetatens kvittering legges som betalingslinje i en OCA-betalingsordre
(account_payment_order) med move_line_id = oppgjørsbilagets 2740-linje.
OCA-flyten avstemmer da oppgjørskontoen automatisk når ordren effektueres,
og KID-en går i pain.001 (konverteres til Strd/SCOR av l10n_no_dnb_payments
siden gyldig KID). Ingen penger flyttes før fila er godkjent i nettbanken.

Skilt ut fra kjernen (2026-07) slik at kjernen kjører på enhver Odoo 19 —
Community OG Enterprise/Odoo.sh — uten OCA (Produkt 2: modulkunder).
Enterprise-kunder betaler i stedet fra sin egen betalingsflyt; beløp, KID,
konto og frist står uansett i Betaling-fanen på MVA-meldingen.

auto_install: ved NYINSTALLASJON installeres broen automatisk der kjernen
OG OCA-modulene finnes (Roret Cloud-malen).

⚠️ OPPGRADERING AV EKSISTERENDE DATABASE (fra kombinert modul ≤19.0.3.x):
auto_install trigges IKKE retroaktivt av en ren `-u`. Kjør oppgradering og
bro-installasjon i SAMME kommando, ellers sletter Odoos felt-opprydding
betaling_order_id-kolonnen (verifisert 2026-07-17):

    odoo-bin -d <db> -u l10n_no_account_mvamelding \\
             -i l10n_no_account_mvamelding_payment --stop-after-init

Da bevares feltet (samme kolonne gjenbrukes) og betalingsflyten er uendret.
""",
    'author': 'Eristo AS',
    'website': 'https://eristo.no',
    'depends': [
        'l10n_no_account_mvamelding',
        # OCA bank-payment — betalingsordre + pain.001
        'account_payment_order',
        'account_banking_sepa_credit_transfer',
    ],
    'data': [
        'views/l10n_no_mvamelding_views.xml',
    ],
    'auto_install': True,
    'license': 'LGPL-3',
}
