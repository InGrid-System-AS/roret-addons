{
    'name': 'Norway - Eristo ID-porten',
    'version': '19.0.1.1.0',
    'category': 'Accounting/Localizations',
    'summary': 'ID-porten OIDC-integrasjon for skattemelding-submit og andre '
               'Skatteetaten-tjenester som krever sluttbruker-autentisering',
    'description': """
ID-porten autentiseringsmodul. Skatteetaten krever sluttbruker-spor
("utførende person") for visse innsendings-API'er — Maskinporten/
systembruker alene er utilstrekkelig. Denne modulen håndterer:

  1. Generering av authorize-URL (delegert til Eristo Token Service)
  2. Popup/redirect-flyt mot Digdir's ID-porten
  3. Callback-håndtering med session_id-utveksling
  4. Henting av Altinn-token (med PID/fnr inkludert) fra Token Service
  5. Bruk av Altinn-tokenet i kallende moduls submit-flyt

Generisk design: andre moduler kan bruke ``start_idporten_flow(record,
callback)`` for å autentisere bruker før en spesifikk operasjon.
""",
    'author': 'Eristo AS',
    'website': 'https://eristo.no',
    'depends': [
        'base',
        'l10n_no_eristo_base',
    ],
    'data': [
        'security/ir.model.access.csv',
        'views/idporten_session_views.xml',
    ],
    'installable': True,
    'auto_install': False,
    'license': 'LGPL-3',
}
