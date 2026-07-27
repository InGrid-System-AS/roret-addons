"""Customer-onboarding-wizard for Eristo Token Service.

Brukes av Eristo (platform-operatør) for å registrere et nytt selskap
som kunde av token-tjenesten. Wizarden:

  1. Henter orgnr fra company.vat (via _orgnr-helperen, m. test-override)
  2. Lar admin velge environment (test/prod)
  3. Kaller /onboard-customer på tjenesten m. admin-secret som Bearer
  4. Skriver returnert API-key inn i company.l10n_no_eristo_api_key
  5. Viser API-key i klartekst én gang som backup (tjenesten lagrer
     kun SHA-256-hash)

Etter wizarden er ferdig kan admin gå videre til onboarding-wizard'en
for å aktivere scopes (skattemelding, a-melding etc.) i Altinn.

Sikkerhet: kun base.group_system kan kjøre wizarden — den både leser
admin-secret fra ir.config_parameter og skriver API-key på selskapet.
"""
from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError


_STATE_SELECTION = [
    ('start', '1. Klar til onboarding'),
    ('done', '2. Onboarding fullført ✓'),
    ('error', '⚠ Feil'),
]


class L10nNoEristoCustomerOnboardWizard(models.TransientModel):
    _name = 'l10n.no.eristo.customer.onboard.wizard'
    _description = 'Eristo customer-onboarding-wizard (admin-only)'

    company_id = fields.Many2one(
        'res.company',
        string="Selskap",
        required=True,
        default=lambda self: self.env.company,
    )
    company_name = fields.Char(
        string="Selskapsnavn",
        related='company_id.name',
        readonly=True,
    )
    orgnr = fields.Char(
        string="Organisasjonsnummer",
        help="9-sifret norsk organisasjonsnummer. Auto-utfylt fra "
             "company.vat (eller test-orgnr-override om satt). "
             "Overstyr kun hvis du onboarder selskapet under et annet "
             "orgnr enn det som står på selskapsraden.",
    )
    environment = fields.Selection(
        [('test', 'Test (TT02)'), ('prod', 'Produksjon')],
        string="Miljø",
        required=True,
        default='test',
        help="Bestemmer hvilken Maskinporten-klient Eristo bruker for "
             "dette selskapet. Start alltid på Test før du går til "
             "Produksjon — ekte Skatteetaten-innsendinger fra Test-miljø "
             "vil bli avvist.",
    )
    state = fields.Selection(
        _STATE_SELECTION,
        string="Status",
        default='start',
    )
    customer_id_display = fields.Char(
        string="Customer ID",
        readonly=True,
        help="UUID returnert av Eristo Token Service. Brukes til "
             "manuell SQL-oppslag/rotering hvis nødvendig.",
    )
    api_key_display = fields.Char(
        string="API-nøkkel (vises kun nå)",
        readonly=True,
        help="Kunde-API-nøkkelen som identifiserer selskapet mot "
             "token-tjenesten. Lagres automatisk i selskapets "
             "Eristo-tilkobling. Eristo lagrer kun SHA-256-hash — "
             "denne klartekstverdien kan ikke hentes ut igjen senere.",
    )
    error_message = fields.Text(string="Feilmelding", readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        """Auto-utfyll orgnr fra company.vat hvis ikke spesifisert."""
        for vals in vals_list:
            if not vals.get('orgnr') and vals.get('company_id'):
                company = self.env['res.company'].browse(vals['company_id'])
                try:
                    vals['orgnr'] = self.env[
                        'l10n.no.eristo.service'
                    ]._orgnr(company)
                except UserError:
                    # La feltet være tomt — kunde må fylle ut manuelt
                    pass
        return super().create(vals_list)

    def action_onboard(self):
        """Kall /onboard-customer Edge Function og lagre returnert nøkkel."""
        self.ensure_one()
        if not self.env.user.has_group('base.group_system'):
            raise AccessError(_(
                "Bare brukere med 'Settings'-rettigheter kan onboarde "
                "selskaper mot Eristo Token Service."
            ))
        if self.state != 'start':
            raise UserError(_("Onboarding er allerede kjørt."))
        if not self.orgnr:
            raise UserError(_("Organisasjonsnummer er påkrevd."))
        # B1: rebuild-safe — hvis selskapet allerede har en API-key må
        # admin slette den manuelt først for å unngå utilsiktet
        # overskriving av en aktiv nøkkel.
        if self.company_id.sudo().l10n_no_eristo_api_key:
            raise UserError(_(
                "Selskap '%(name)s' har allerede en Eristo API-key. "
                "Slett den fra Settings → Companies → Skatteetaten-"
                "tilkobling før du onboarder på nytt (eller bruk "
                "rotate-endpoint når den er bygget).",
                name=self.company_id.display_name,
            ))

        eristo = self.env['l10n.no.eristo.service']
        try:
            result = eristo.request_customer_onboarding(
                self.company_id,
                orgnr=self.orgnr.strip(),
                environment=self.environment,
                customer_name=self.company_id.name,
            )
        except UserError as e:
            self.write({'state': 'error', 'error_message': str(e)})
            return self._reopen()

        api_key = result.get('api_key')
        if not api_key:
            self.write({
                'state': 'error',
                'error_message': _(
                    "Eristo Token Service returnerte uventet respons "
                    "uten 'api_key' — kontakt support. Rådata:\n%(r)s",
                    r=str(result)[:600],
                ),
            })
            return self._reopen()

        # Lagre API-key + sett environment-display på selskapet.
        # sudo() fordi l10n_no_eristo_api_key er groups='base.group_system',
        # vi har akkurat verifisert at brukeren ER i den gruppen ovenfor.
        self.company_id.sudo().write({
            'l10n_no_eristo_api_key': api_key,
            'l10n_no_eristo_environment': self.environment,
        })
        self.write({
            'state': 'done',
            'customer_id_display': result.get('id'),
            'api_key_display': api_key,
            'error_message': False,
        })
        return self._reopen()

    def _reopen(self):
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }
