"""res.company-utvidelse for Eristo Token Service-tilkobling.

Felt-navnene har `_eristo_`-prefiks (ikke `_amelding_eristo_`) fordi
disse brukes på tvers av alle Eristo-leverte Skatteetaten-integrasjoner,
ikke bare a-melding. Migration i l10n_no_hr_payroll renamer fra det
gamle prefikset.
"""
from odoo import _, fields, models
from odoo.exceptions import UserError


_DEFAULT_TOKEN_URL = (
    'https://vwyfckvpfnfpjbzgoqpu.supabase.co/functions/v1/maskinporten-token'
)


class ResCompany(models.Model):
    _inherit = 'res.company'

    l10n_no_eristo_environment = fields.Selection(
        [('test', 'Test (TT02)'), ('prod', 'Produksjon')],
        string="Skatteetaten miljø",
        default='test',
        help="Informasjonsfelt som viser hvilket miljø selskapet er konfigurert "
             "for i Eristo Token Service. Endring her påvirker IKKE selve "
             "tjenesten — miljøet bestemmes av customer-rad i token-tjenesten.",
    )
    l10n_no_eristo_token_url = fields.Char(
        string="Eristo Token Service URL",
        default=_DEFAULT_TOKEN_URL,
        help="HTTP-endepunkt som signerer Maskinporten JWT på vegne av "
             "selskapet. Default peker på Eristos prod-tjeneste — ikke "
             "endre med mindre du kjører din egen instans.",
    )
    l10n_no_eristo_api_key = fields.Char(
        string="Eristo API-key",
        groups='base.group_system',
        help="API-key utstedt fra Eristo ved onboarding. Identifiserer "
             "selskapet mot token-tjenesten. Kontakt Eristo support hvis "
             "nøkkelen er kompromittert eller mangler.",
    )
    l10n_no_eristo_active_scopes = fields.Json(
        string="Aktiverte Eristo-scopes",
        copy=False,
        help="Liste av Maskinporten-scopes som er aktivert for selskapet "
             "via Altinn-onboarding. Oppdateres automatisk av onboarding-"
             "wizarden når kunden godkjenner i Altinn-portalen. Lagret som "
             "JSONB-array i DB for å unngå komma-separat-string-skjørhet.",
    )
    l10n_no_eristo_test_orgnr = fields.Char(
        string="Test-orgnr override",
        groups='base.group_system',
        help="Test-override for orgnr brukt i Skatteetaten/Altinn-kall mot "
             "TT02. La stå tom i produksjon. Brukes typisk hvis selskapets "
             "ekte orgnr ikke er importert til TT02-registrene (AMLD_023, "
             "SME-EKSTERN-001) — sett til et syntetisk Tenor-orgnr som "
             "Skatteetaten kjenner. _orgnr-helperen returnerer denne om "
             "satt, ellers faller den tilbake på company.vat.",
    )

    def l10n_no_eristo_is_scope_active(self, scope):
        """Sjekk om en gitt scope er aktivert (kunde-onboarded i Altinn)."""
        self.ensure_one()
        active = self.l10n_no_eristo_active_scopes or []
        if not isinstance(active, (list, tuple)):
            return False
        return scope.strip() in {str(s).strip() for s in active}

    def action_l10n_no_test_eristo_connection(self):
        """Verifiser at API-key + URL fungerer mot Eristo Token Service.

        Kaller /eristo-ping som kun validerer API-key (ingen Maskinporten/
        Altinn-kall). Tidligere prøvde knappen å hente et a-melding-token,
        som feilet for kunder som ikke har a-melding-systembruker (eks.
        kunder med kun skattemelding-scope aktivert).

        For scope-spesifikk testing finnes egen "Test forbindelse"-knapp
        på skattemelding-record-formet.
        """
        self.ensure_one()
        if not self.env.user.has_group('base.group_system'):
            raise UserError(_(
                "Bare system-administratorer kan teste Eristo-tilgang."
            ))
        try:
            result = self.env['l10n.no.eristo.service'].ping(self)
            customer = result.get('customer') or {}
            active_scopes = customer.get('active_scopes') or []
            scopes_line = (
                ', '.join(active_scopes) if active_scopes
                else "(ingen scopes aktivert ennå)"
            )
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'type': 'success',
                    'title': _("Eristo Token Service OK"),
                    'message': _(
                        "API-key gyldig.\n\n"
                        "Kunde: %(name)s\n"
                        "Orgnr: %(orgnr)s\n"
                        "Miljø: %(env)s\n"
                        "Aktive scopes: %(scopes)s",
                        name=customer.get('name') or '?',
                        orgnr=customer.get('orgnr') or '?',
                        env=customer.get('environment') or '?',
                        scopes=scopes_line,
                    ),
                    'sticky': True,
                },
            }
        except Exception as e:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'type': 'danger',
                    'title': _("Eristo Token Service FEILET"),
                    'message': str(e)[:600],
                    'sticky': True,
                },
            }
