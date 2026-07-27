{
    'name': 'Norway - Eristo Token Service Base',
    'version': '19.0.2.6.1',
    'category': 'Localization',
    'summary': 'Felles auth-lag mot Skatteetaten via Eristo Token Service',
    'description': """
Base-modul for alle Eristo-leverte Skatteetaten-integrasjoner i Odoo:
a-melding (l10n_no_hr_payroll), MVA (l10n_no_account_mva), skattemelding
(l10n_no_account_skattemelding), m.fl.

Tilbyr:
  - res.company-felt for Eristo Token Service-tilkobling (URL, API-key,
    miljø)
  - l10n.no.eristo.service med get_access_token(scope) og _orgnr-helper
  - "Test Eristo-forbindelse"-knapp på res.company-formet
  - "Skatteetaten-tilkobling"-tab på res.company som domene-modulene
    bygger oppå sine egne tabber (A-melding, MVA, etc.)

Tjenesten er Roret Compliance Gateway (api.roret.no) — en selvhostet
FastAPI-tjeneste som holder virksomhetssertifikatet. Modulen her er
HTTP-klient mot den, og bryr seg kun om URL + API-key.
""",
    'author': 'Eristo AS',
    'website': 'https://eristo.no',
    'depends': [
        'base',
        'l10n_no',
    ],
    'data': [
        'security/ir.model.access.csv',
        # Wizard-actions må lastes før res_company_views.xml siden
        # company-formet refererer customer-onboard-action via %(...)d.
        'wizard/l10n_no_eristo_onboarding_wizard_views.xml',
        'wizard/l10n_no_eristo_customer_onboard_wizard_views.xml',
        'views/res_company_views.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'l10n_no_eristo_base/static/src/js/onboarding_poller.js',
            'l10n_no_eristo_base/static/src/xml/onboarding_poller.xml',
        ],
    },
    'installable': True,
    'auto_install': False,
    'license': 'LGPL-3',
}
