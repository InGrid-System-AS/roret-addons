"""res.company-utvidelse: aktiver MVA-melding-tjenesten (Altinn-onboarding).

Gjenbruker den generiske Eristo onboarding-wizarden (samme som skattemelding/
a-melding). MVA bruker en distinkt onboarding-nøkkel (SCOPE_ONBOARDING =
skatteetaten:mvamelding) som token-tjenesten mapper til tilgangspakken
merverdiavgift + tokenScope altinn:instances.write. Egen nøkkel hindrer
kollisjon med årsregnskap (som også bruker altinn:instances.write) både i
delegering og i aktivert-flagget.
"""
from odoo import _, api, fields, models

from .l10n_no_mvamelding import SCOPE_ONBOARDING


class ResCompany(models.Model):
    _inherit = 'res.company'

    l10n_no_mvamelding_aktivert = fields.Boolean(
        string="MVA-melding aktivert",
        compute='_compute_l10n_no_mvamelding_aktivert',
        help="True når selskapet har Altinn-systembruker-tilgang for "
             "MVA-melding. Sett via 'Aktiver MVA-melding'-knappen.",
    )

    l10n_no_mva_submit_mode = fields.Selection(
        [('idporten', "ID-porten (innlogget person, BankID)"),
         ('systembruker', "Systembruker (helautomatisk)")],
        string="MVA-innsendingsmodus",
        default='idporten',
        help="Hvordan MVA-meldingen sendes til Skatteetaten.\n\n"
             "ID-porten: brukeren logger inn med BankID ved innsending — "
             "dette er flyten Skatteetaten behandler i dag.\n"
             "Systembruker: helautomatisk, men Skatteetatens behandlingsløp "
             "henter per 2026-07 IKKE systembruker-innsendinger (verifisert "
             "ved A/B-test i TT02, se docs/mva-systembruker-funn.md) — "
             "bytt først når Skatteetaten har bekreftet støtte.\n\n"
             "Kvittering hentes alltid automatisk (systembruker), uavhengig "
             "av modus.",
    )

    @api.depends('l10n_no_eristo_active_scopes')
    def _compute_l10n_no_mvamelding_aktivert(self):
        for c in self:
            c.l10n_no_mvamelding_aktivert = c.l10n_no_eristo_is_scope_active(
                SCOPE_ONBOARDING)

    def action_l10n_no_mvamelding_activate(self):
        """Åpne onboarding-wizard for MVA-melding."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Aktiver MVA-melding"),
            'res_model': 'l10n.no.eristo.onboarding.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_company_id': self.id,
                # Distinkt onboarding-nøkkel (ikke token-scopet) — token-
                # tjenesten mapper den til merverdiavgift-tilgangspakken.
                'default_scopes_text': SCOPE_ONBOARDING,
                'default_service_name': 'MVA-melding (merverdiavgift)',
            },
        }
