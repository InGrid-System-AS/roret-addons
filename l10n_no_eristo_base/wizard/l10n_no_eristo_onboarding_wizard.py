"""Gjenbrukbar onboarding-wizard for Eristo-tjenester.

Alle moduler (skattemelding, a-melding, MVA osv.) kaller denne wizarden
for å aktivere Altinn 3 systembruker-rettighet for sin scope. Wizarden:

  1. Kaller Eristo platform /onboard-systembruker for å opprette Altinn-
     request og få en confirm_url
  2. Viser confirm_url til kunden m. instruks om å åpne den
  3. Når kunden har godkjent (manuelt) klikker de "Sjekk status" som
     poller platform-endpoint. Når status er accepted lukker wizarden
     med suksess-melding og setter aktiv-flag på selskapet.

Brukes via en action der modul-koden setter context:
  {'default_scopes_text': 'scope1,scope2',
   'default_company_id': N,
   'default_service_name': 'Min Tjeneste'}

scopes_text er komma-separert string (Char-felt) — wizarden splitter
og deduper internt før kall til platform-API.
"""
from odoo import _, api, fields, models
from odoo.exceptions import UserError


_STATE_SELECTION = [
    ('start', '1. Klar til aktivering'),
    ('pending', '2. Venter på godkjenning i Altinn'),
    ('accepted', '3. Aktivert ✓'),
    ('rejected', '⛔ Avvist'),
    ('timeout', '⏱ Tidsavbrudd'),
    ('error', '⚠ Feil'),
]


class L10nNoEristoOnboardingWizard(models.TransientModel):
    _name = 'l10n.no.eristo.onboarding.wizard'
    _description = 'Eristo onboarding-wizard for Altinn systembruker'

    company_id = fields.Many2one(
        'res.company',
        string="Selskap",
        required=True,
        default=lambda self: self.env.company,
    )
    scopes_text = fields.Char(
        string="Scopes",
        help="Komma-separert liste av Maskinporten-scopes som "
             "skal aktiveres. Settes typisk via context.",
    )
    party_orgnr = fields.Char(
        string="Party orgnr (override)",
        help="Overstyr orgnr for testing — la stå tom for "
             "å bruke selskapets vat.",
    )
    service_name = fields.Char(
        string="Tjeneste",
        help="Visningsnavn for tjenesten som aktiveres "
             "(eks. 'Skattemelding-innsending').",
    )
    state = fields.Selection(
        _STATE_SELECTION,
        string="Status",
        default='start',
    )
    altinn_request_id = fields.Char(readonly=True)
    external_ref = fields.Char(readonly=True)
    confirm_url = fields.Char(string="Godkjennings-URL", readonly=True)
    error_message = fields.Text(string="Feilmelding", readonly=True)

    def action_start_onboarding(self):
        """Steg 1: opprett Altinn-systembruker-request."""
        self.ensure_one()
        if self.state != 'start':
            raise UserError(_("Onboarding er allerede startet."))
        if not self.scopes_text:
            raise UserError(_("Wizarden er kalt uten scopes (sjekk context)."))

        # V4: valider orgnr klient-side før vi traverserer platform/Altinn
        # — kunden får klar feilmelding hvis VAT er feilaktig formattert.
        eristo = self.env['l10n.no.eristo.service']
        try:
            eristo._orgnr(self.company_id)
        except UserError as e:
            self.write({'state': 'error', 'error_message': str(e)})
            return self._reopen()

        scopes = [s.strip() for s in self.scopes_text.split(',') if s.strip()]
        try:
            result = eristo.request_onboarding(
                self.company_id, scopes, party_orgnr=self.party_orgnr or None,
            )
        except UserError as e:
            self.write({'state': 'error', 'error_message': str(e)})
            return self._reopen()

        self.write({
            'state': 'pending',
            'altinn_request_id': result.get('altinn_request_id'),
            'external_ref': result.get('external_ref'),
            'confirm_url': result.get('confirm_url'),
            'error_message': False,  # nullstill ved suksess
        })
        return self._reopen()

    def action_check_status(self):
        """Steg 2: poll Eristo platform for godkjennings-status."""
        self.ensure_one()
        if not self.external_ref:
            raise UserError(_("Ingen onboarding-request i flight."))

        eristo = self.env['l10n.no.eristo.service']
        try:
            result = eristo.check_onboarding_status(
                self.company_id, self.external_ref,
            )
        except UserError as e:
            self.write({'state': 'error', 'error_message': str(e)})
            return self._reopen()

        status = result.get('status', 'pending')
        new_state = status if status in dict(_STATE_SELECTION) else 'error'

        if status == 'accepted':
            # V1: active_scopes er nå Json-felt (liste).
            # Merge inn nye scopes uten å miste eksisterende.
            requested = [
                s.strip() for s in (self.scopes_text or '').split(',')
                if s.strip()
            ]
            current = self.company_id.l10n_no_eristo_active_scopes or []
            if not isinstance(current, list):
                current = []
            current_set = {str(s).strip() for s in current}
            for s in requested:
                if s not in current_set:
                    current.append(s)
                    current_set.add(s)
            self.company_id.l10n_no_eristo_active_scopes = current
            # V3: nullstill error_message ved suksess
            self.write({'state': new_state, 'error_message': False})
        else:
            self.state = new_state
        return self._reopen()

    def action_open_altinn(self):
        """Åpne confirm_url i ny tab."""
        self.ensure_one()
        if not self.confirm_url:
            raise UserError(_("Ingen URL å åpne."))
        return {
            'type': 'ir.actions.act_url',
            'url': self.confirm_url,
            'target': 'new',
        }

    def _reopen(self):
        """Reopen wizarden m. samme record (etter status-endring)."""
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }
