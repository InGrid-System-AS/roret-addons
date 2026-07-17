{
    'name': 'Norway - SAF-T Financial',
    'version': '19.0.1.0.0',
    'category': 'Accounting/Localizations',
    'summary': 'SAF-T Financial-eksport (RF-1363) — lovpålagt, test-først '
               'mot Skatteetatens offisielle XSD v1.30',
    'description': """
SAF-T Financial-eksport for norske bokføringspliktige (bokførings-
forskriften § 7-8). Genererer komplett XML validert mot Skatteetatens
offisielle XSD v1.30: kontoplan med åpnings-/lukkesaldoer og
næringsspesifikasjons-gruppering, kunde-/leverandørreskontro, mva-tabell
med standard mva-koder, og hele hovedboken (journal → bilag → linjer med
mva-informasjon, KID/CID og forfall).

Fila lastes opp i Altinn (RF-1363) på forespørsel fra Skatteetaten.

Bygget for Roret fordi OCA/l10n-norway er tom og Odoos egen SAF-T er
Enterprise-only. Gate for produksjonsflytting av InGrid/Eristo.
""",
    'author': 'Eristo AS',
    'website': 'https://eristo.no',
    'depends': ['account', 'l10n_no'],
    'external_dependencies': {'python': ['lxml']},
    'data': [
        'security/ir.model.access.csv',
        'views/l10n_no_saft_views.xml',
    ],
    'installable': True,
    'auto_install': False,
    'license': 'LGPL-3',
}
