{
    'name': 'Roret — koble til AI-agenten',
    'version': '19.0.1.0.0',
    'category': 'Productivity',
    'summary': 'Koble denne Odoo-en til Roret-agenten uten å håndtere nøkler',
    'description': """
Én knapp som gjør det en Roret-operatør tidligere gjorde i en terminal:
minter en API-nøkkel for DEN INNLOGGEDE BRUKEREN og sender den til Roret,
autorisert av en kortlevd paringskode brukeren fikk fra agenten.

Nøkkelen vises aldri på skjermen og havner aldri i en logg. Den lages
server-side, POSTes én gang, og klarteksten kastes lokalt; hos Roret
lagres den kryptert.

Se README.md for flyten og for hvorfor retningen (agent utsteder kode,
Odoo innløser den) er valgt som den er.
""",
    'author': 'Roret',
    'website': 'https://roret.no',
    # Core-only, med vilje: modulen skal kunne installeres i en hvilken som
    # helst Odoo 19 — Community eller Enterprise/Odoo.sh — uten at du må ta
    # inn OCA-moduler eller noe annet fra Roret.
    'depends': ['base'],
    'data': [
        'security/ir.model.access.csv',
        'data/roret_mcp_kobling_data.xml',
        'wizard/roret_mcp_kobling_views.xml',
        'views/roret_mcp_kobling_menus.xml',
    ],
    'installable': True,
    'auto_install': False,
    'license': 'LGPL-3',
}
