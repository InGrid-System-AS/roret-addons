{
    'name': 'Norway - MVA-melding betaling (Enterprise batch)',
    'version': '19.0.1.0.0',
    'category': 'Accounting/Localizations',
    'summary': 'MVA-betaling (KID → pain.001) via Enterprise betalingsbunt',
    'description': """
Bro-modul mellom MVA-melding-kjernen (l10n_no_account_mvamelding, core-only)
og Enterprise-betalingsstacken (account_batch_payment).

Søstermodul til l10n_no_account_mvamelding_payment, som gjør det samme mot
OCA-stacken. De to dekker hver sin kundetype:

  * Roret Cloud   — Community + OCA  → l10n_no_account_mvamelding_payment
  * Produkt 2     — egen Odoo.sh/Enterprise → DENNE

Stackene er i praksis disjunkte (Odoo.sh har ikke OCA bank-payment; Roret
Cloud har ikke Enterprise), så normalt auto-installeres kun én av dem. Er
begge til stede får MVA-meldingen to «Opprett betaling»-knapper — feltene
og metodene har ulike navn, så det gir ingen konflikt, bare et valg.

Flyten:
  1. account.payment (KJERNE) opprettes med destination_account_id =
     oppgjørskontoen (2740, liability_payable + reconcile i l10n_no-
     charten) og KID som memo. Har betalingsmetoden outstanding-konto
     avstemmes 2740-linjene umiddelbart; ellers bokfører Odoo 19 først
     ved bankavstemming, og betalingen linkes via matched_payment_ids.
  2. Betalingen legges i en account.batch.payment — det er den
     l10n_no_dnb_payments eksporterer til pain.001, og som
     l10n_no_eristo_bank_bridge sender til DNB File Gateway.

KID: settes som memo på betalingen. l10n_no_dnb_payments konverterer
numeriske referanser ≥ 11 sifre med gyldig MOD10/MOD11-sjekksiffer fra
<Ustrd> til <Strd>/CdtrRefInf/SCOR — samme mekanisme OCA-broen bygger på.

Penger flyttes aldri herfra: bunten må godkjennes i nettbanken.
""",
    'author': 'Eristo AS',
    'website': 'https://eristo.no',
    'depends': [
        'l10n_no_account_mvamelding',
        # Enterprise betalingsbunt — pain.001-eksporten henger på denne.
        'account_batch_payment',
    ],
    'data': [
        'views/l10n_no_mvamelding_views.xml',
    ],
    'auto_install': True,
    'license': 'LGPL-3',
}
