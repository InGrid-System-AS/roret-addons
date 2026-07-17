"""Lokal speil av idporten-sessions for revisjon og state-mapping.

Skatteetaten/Altinn krever at vi kan referere tilbake til en
ID-porten-autentisering når brukeren har autentisert seg. Denne tabellen
holder en LOKAL ref-til-callback-record som forteller hvilken Odoo-
record (skattemelding, MVA-melding osv.) som skal handles på etter at
brukeren er tilbake fra ID-porten-popup.

Den faktiske Altinn-tokenet og PID lagres I Supabase Token Service —
ikke her, av sikkerhetshensyn.
"""
from datetime import datetime, timedelta

from odoo import api, fields, models


def _default_expires_at(self):
    # Odoo kaller default-callables med recordset-et (self) — signaturen
    # MÅ ta imot det, ellers TypeError ved create (fanget i klikk-test
    # 2026-07-13; enhetstestene mocket start_authorize_flow og bommet).
    return datetime.now() + timedelta(minutes=10)


class L10nNoEristoIdPortenSession(models.Model):
    _name = 'l10n.no.eristo.idporten.session'
    _description = 'ID-porten OIDC-session (lokal speil)'
    _order = 'create_date desc'

    # Eristo Token Service-side session-ID (UUID fra idporten_sessions-tabellen)
    eristo_session_id = fields.Char(
        string="Eristo Session ID",
        required=True,
        index=True,
        copy=False,
    )

    # Kontekst — hvilken record/operasjon skal ta over når bruker er tilbake
    state = fields.Char(
        string="OAuth state",
        required=True,
        copy=False,
        help="State-parameter generert av Eristo Token Service. Brukes til "
             "å verifisere callback fra Digdir og finne tilbake til riktig "
             "Odoo-record."
    )
    target_model = fields.Char(
        string="Mål-modell",
        required=True,
        help="Modellnavn som har callback-metoden, f.eks. 'l10n.no.skattemelding'.",
    )
    target_res_id = fields.Integer(
        string="Mål-record ID",
        required=True,
    )
    callback_method = fields.Char(
        string="Callback-metode",
        required=True,
        help="Metode som kalles med altinn_token når brukeren er tilbake, "
             "eks. 'action_l10n_no_skattemelding_submit_with_idporten'.",
    )

    # Tilstand
    status = fields.Selection(
        [
            ('pending', 'Pågår'),
            ('authenticated', 'Autentisert'),
            ('completed', 'Fullført'),
            ('failed', 'Feilet'),
            ('expired', 'Utløpt'),
        ],
        default='pending',
        required=True,
        copy=False,
    )
    error_code = fields.Char(copy=False)
    error_message = fields.Text(copy=False)

    # PID (fnr) av personen som autentiserte seg — KUN lagret for revisjon
    # etter at flow er fullført. Slettes av cron etter 30 dager.
    pid = fields.Char(
        string="PID (fnr)",
        copy=False,
        groups="base.group_no_one",
        help="Personidentifikator fra ID-porten — kun for revisjon. "
             "Slettes etter 30 dager av cron-jobb."
    )

    # Brukeren som STARTET flyten — callbacken må fullføres av samme
    # bruker (non-repudiation: «utførende person» skal være personen som
    # faktisk satt i Odoo-sesjonen). Settes eksplisitt av servicen siden
    # create skjer via sudo() (create_uid ville blitt superuser).
    initiator_uid = fields.Many2one(
        'res.users',
        string="Startet av",
        copy=False,
        help="Callback på /idporten/done godtas kun fra denne brukeren.",
    )

    # Tidsspor
    completed_at = fields.Datetime(copy=False)
    expires_at = fields.Datetime(
        default=_default_expires_at,
        help="Hele OIDC-flowen må fullføres innen 10 minutter.",
    )

    company_id = fields.Many2one(
        'res.company',
        required=True,
        default=lambda self: self.env.company,
    )

    @api.autovacuum
    def _gc_pid(self):
        """Slett fnr fra gamle sesjoner (personvern: 30 dagers retensjon).

        Sesjonsraden beholdes for revisjonssporet — det er kun
        personidentifikatoren som nullstilles, slik feltets help-tekst
        lover. Kjøres av Odoos autovacuum-cron (daglig).
        """
        frist = fields.Datetime.now() - timedelta(days=30)
        gamle = self.sudo().search([
            ('pid', '!=', False),
            ('create_date', '<', frist),
        ])
        gamle.write({'pid': False})
