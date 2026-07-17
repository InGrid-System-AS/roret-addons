{
    'name': 'Norway - Skattemelding (Tax Return)',
    'version': '19.0.8.5.1',
    'category': 'Accounting/Localizations',
    'summary': 'Skattemelding-innsending for AS via Skatteetaten Altinn 3 + valideringsjobb-API',
    'description': """
Generer og send skattemelding (RF-1167-erstatter) + næringsspesifikasjon
elektronisk til Skatteetaten via deres Altinn 3 instance API + asynkron
valideringsjobb.

Mappingen mellom Odoo-kontoplan (NS 4102) og Skatteetatens
resultat- og balanseregnskaps-typer er implementert som standard for
generelle næringer, med mulighet for kunde-spesifikke overstyringer.

Auth: gjenbruker l10n_no_eristo_base for Maskinporten-tokens. Krever
scope skatteetaten:formueinntekt/skattemelding på Eristos klient.

API-flyt:
  1. Bygg XML (skattemelding + næringsspesifikasjon) fra account.move-data
  2. Pakk i konvolutt-XML
  3. Opprett Altinn 3-instans (skd/formueinntekt-skattemelding-v2)
  4. Last opp konvolutt
  5. Trigger valideringsjobb hos Skatteetaten
  6. Poll til validert
  7. Submit via Altinn

Status (2026-05-08): Phase 1 — minimum viable for AS uten aktivitet.
NS 4102-mapping og full Altinn-integrasjon kommer i senere faser.
""",
    'author': 'Eristo AS',
    'website': 'https://eristo.no',
    'depends': [
        'account',
        'mail',
        'l10n_no',
        'l10n_no_eristo_base',
        # Community/OCA-port: account_reports/l10n_no_reports-avhengigheten
        # er kuttet. Tallene bygges av modulens EGEN NS 4102-mapping over
        # account.move-data (som alltid), og egen-uttrekket ekskluderer
        # avslutningsbilag selv (move_id.l10n_no_skattemelding_closing).
        # Enterprise-sømmen var kun et visningsfilter i Enterprise-P&L-UIet;
        # tilsvarende filter i OCA account_financial_report kan legges til
        # ved behov (backlogg).
    ],
    'data': [
        'security/ir.model.access.csv',
        'security/skattemelding_security.xml',
        'data/skattemelding_kodetype_data.xml',
        'data/ir_cron_data.xml',
        'views/account_account_views.xml',
        'views/l10n_no_skattemelding_views.xml',
        'views/res_company_views.xml',
        'wizards/submit_confirm_wizard_views.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'l10n_no_account_skattemelding/static/src/js/skattemelding_poller.js',
            'l10n_no_account_skattemelding/static/src/xml/skattemelding_poller.xml',
            'l10n_no_account_skattemelding/static/src/scss/skattemelding_xml_display.scss',
        ],
    },
    'installable': True,
    'auto_install': False,
    'license': 'LGPL-3',
}
