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
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..models.l10n_no_eristo import folgescopes_fra_ping

_logger = logging.getLogger(__name__)


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
            self._sync_active_scopes()
            # V3: nullstill error_message ved suksess
            self.write({'state': new_state, 'error_message': False})
        else:
            self.state = new_state
        return self._reopen()

    def _sync_active_scopes(self):
        """Utvid selskapets scope-liste med de forespurte OG gatewayens.

        Feltet bærer to vokabularer, og det er ikke et uhell:

          * ONBOARDING-nøkler — det wizarden ber om, og det modulenes
            aktivert-flagg leser. MVA onboardes som
            skatteetaten:mvamelding.
          * TOKEN-scopes — det gatewayen faktisk kan mint. For MVA er det
            altinn:instances.write (tilgangspakken merverdiavgift), og
            gatewayens /eristo-ping rapporterer KUN denne formen, siden
            active_scopes utledes av scope_refs (store.py: `list(scope_refs)`).

        For a-melding og skattekort er de to like, og forskjellen er
        usynlig. For MVA er de ulike — og et flagg som
        l10n_no_mvamelding_aktivert leser onboarding-nøkkelen. Ville vi
        ERSTATTET lista med pingens svar, forsvant skatteetaten:mvamelding,
        flagget ble False, og porten i l10n_no_mvamelding_submit ville
        blokkert innsending med «ikke aktivert ennå» på et selskap som er
        fullt aktivert. Derfor er dette additivt, aldri erstattende.

        Pingen tilfører det forespørselen ikke kan uttrykke: følgescopes
        gatewayen aktiverer på eget initiativ (a-melding gir
        digdir:dialogporten, som ikke har noen Altinn-ressurs å be om og
        derfor aldri kan stå i scopes_text). Feiler pingen, står vi igjen
        med nøyaktig den gamle oppførselen — ikke noe tap.

        Kun GATEWAY_FOLGESCOPES tas inn fra pingen, ikke hele svaret.
        Resten av active_scopes er token-scopes som kolliderer med
        onboarding-nøkler: altinn:instances.write er både MVA-ens
        token-scope og årsregnskaps onboarding-nøkkel, så en ufiltrert
        union ville slått årsregnskap falskt på for en MVA-only-kunde.
        """
        self.ensure_one()
        company = self.company_id
        navn = list(company.l10n_no_eristo_active_scopes or [])
        if not isinstance(navn, list):
            navn = []
        sett = {str(s).strip() for s in navn}

        def legg_til(kandidater):
            for s in kandidater or []:
                s = str(s).strip()
                if s and s not in sett:
                    navn.append(s)
                    sett.add(s)

        legg_til((self.scopes_text or '').split(','))
        try:
            svar = self.env['l10n.no.eristo.service'].ping(company)
            legg_til(folgescopes_fra_ping(
                (svar.get('customer') or {}).get('active_scopes')))
        except Exception:
            # Bevisst bred: på dette punktet ER delegeringen godkjent i
            # Altinn, og pingen er kun et tillegg. Enhver feil her må
            # degradere til de forespurte scopene, aldri velte
            # accept-steget — da måtte kunden kjørt wizarden på nytt, og
            # en re-kjøring lager en ny systembruker-forespørsel.
            # ping() pakker HTTP/nett i UserError, men ikke ValueError
            # fra json.loads (proxy-feilside med status 200).
            _logger.warning(
                "Kunne ikke hente active_scopes fra gatewayen for %s — "
                "beholder de forespurte scopene.", company.name,
                exc_info=True)
        company.l10n_no_eristo_active_scopes = navn

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
