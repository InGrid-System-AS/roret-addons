"""Tag for årsavslutningsbilag — skiller dem fra ordinære P&L-bevegelser
ved skattemelding-aggregering.

Når brukeren klikker 'Opprett avslutningsbilag' på en l10n.no.skattemelding-
record, oppretter modulen et 4-linjes account.move som lukker P&L-kontoer
mot 8800 (Årsresultat) og overfører resultatet til 2050/2080 i balansen.
Dette bilaget markeres med l10n_no_skattemelding_closing=True så vi kan:

  1. Ekskludere det fra resultatregnskap-aggregering i XML-en
     (slik at 7798 fortsatt vises som ekte kostnad, ikke netto 0)
  2. Inkludere det i balanseregnskap-aggregering (slik at 2080 viser
     riktig saldo)
  3. Hindre Phase 4 (syntetisk 2080-linje i XML) fra å duplisere

Flagget er additivt — eksisterende moves har default=False og påvirkes
ikke. Brukere kan i prinsippet sette det manuelt på et eget årsavslutnings-
bilag de selv har bokført, så Eristo-modulen respekterer det.
"""
from odoo import fields, models


class AccountMove(models.Model):
    _inherit = 'account.move'

    l10n_no_skattemelding_closing = fields.Boolean(
        string="Årsavslutningsbilag (skattemelding)",
        default=False,
        copy=False,
        index=True,
        help="Markerer dette som et årsavslutningsbilag for skattemelding-"
             "rapportering. Resultatregnskap-aggregering ekskluderer disse "
             "(slik at årets P&L-bevegelser vises korrekt), mens "
             "balanseregnskap inkluderer dem (saldo på 2050/2080). "
             "Settes automatisk av 'Opprett avslutningsbilag'-action; kan "
             "også settes manuelt på eksisterende avslutningsbilag.",
    )
