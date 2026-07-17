from odoo import _, api, fields, models


_REGNSKAPSPLIKT_SELECTION = [
    ('fullRegnskapsplikt', 'Full regnskapsplikt'),
    ('begrensetRegnskapsplikt', 'Begrenset regnskapsplikt'),
]

_VIRKSOMHETSTYPE_SELECTION = [
    ('oevrigSelskap', 'Øvrig selskap (default for AS)'),
    ('boligselskap', 'Boligselskap'),
    ('boligforvaltningsselskap', 'Boligforvaltningsselskap'),
    ('samvirkeforetak', 'Samvirkeforetak'),
    ('kraftforetak', 'Kraftforetak'),
    ('rederiselskap', 'Rederiselskap'),
    ('verdipapirfond', 'Verdipapirfond'),
]

_REGELTYPE_AARSREGNSKAP_SELECTION = [
    ('regnskapslovensAlminneligeRegler', 'Regnskapslovens alminnelige regler'),
    ('regnskapsregelverkForSmaaForetak', 'Regnskapsregelverk for små foretak'),
    ('ifrsForKonsernregnskap', 'IFRS for konsernregnskap'),
]


class ResCompany(models.Model):
    _inherit = 'res.company'

    l10n_no_skattemelding_regnskapspliktstype = fields.Selection(
        _REGNSKAPSPLIKT_SELECTION,
        string="Regnskapspliktstype",
        default='fullRegnskapsplikt',
        help="AS-er har som hovedregel full regnskapsplikt. Begrenset "
             "regnskapsplikt gjelder små foreninger, små stiftelser osv.",
    )
    l10n_no_skattemelding_virksomhetstype = fields.Selection(
        _VIRKSOMHETSTYPE_SELECTION,
        string="Virksomhetstype",
        default='oevrigSelskap',
        help="Default 'Øvrig selskap' dekker vanlige AS. Endre for spesielle "
             "selskapstyper (boligselskap, kraftforetak osv.) som har egne regler.",
    )
    l10n_no_skattemelding_regeltype_aarsregnskap = fields.Selection(
        _REGELTYPE_AARSREGNSKAP_SELECTION,
        string="Regeltype for årsregnskap",
        default='regnskapslovensAlminneligeRegler',
        help="Hvilket regelverk årsregnskapet er ført etter. Default for de fleste AS.",
    )
    l10n_no_skattemelding_partsnummer = fields.Char(
        string="Partsnummer (Skatteetaten)",
        copy=False,
        help="Skatteetatens interne parts-id for selskapet. Identifiseres "
             "ved første Altinn-kontakt og populeres da automatisk.",
    )

    # Selskaps-opplysninger (Skatteetaten merknad N_MANGLER_OPPLYSNINGER_OM_SELSKAPET).
    # Disse svarer på spørsmål om selskapets struktur som Skatteetaten krever
    # for full næringsspesifikasjon. Default-verdier dekker det typiske AS
    # (ikke børsnotert, ingen ytelser mellom aksjonær og selskap).
    l10n_no_skattemelding_boersnotert = fields.Boolean(
        string="Børsnotert",
        default=False,
        help="Er selskapet notert på en regulert markedsplass (Oslo Børs, "
             "Euronext osv.)? De aller fleste norske AS er IKKE børsnoterte.",
    )
    l10n_no_skattemelding_har_ytelser_aksjonaer = fields.Boolean(
        string="Ytelser mellom aksjonær og selskap",
        default=False,
        help="Er det inngått avtaler eller foretatt transaksjoner mellom "
             "selskapet og aksjonærene (utenom ordinær lønn og aksjeutbytte)? "
             "Eksempler: lån fra/til aksjonær, leie av lokaler eid av "
             "aksjonær, kjøp/salg av tjenester mellom selskap og aksjonærens "
             "andre virksomheter.",
    )

    l10n_no_skattemelding_aktivert = fields.Boolean(
        string="Skattemelding aktivert",
        compute='_compute_l10n_no_skattemelding_aktivert',
        help="True når selskapet har Altinn-systembruker-tilgang for "
             "skattemelding-scope. Sett via 'Aktiver skattemelding'-knappen.",
    )

    # Årsavslutnings-konti — brukes av action_l10n_no_skattemelding_create_closing_entry
    # for å bokføre årets resultat ned til balanse-EK. Default NS 4102-koder
    # via søk hvis ikke eksplisitt satt. Felt-navn matcher koder for klarhet.
    l10n_no_skattemelding_aarsresultat_account_id = fields.Many2one(
        'account.account',
        string="Årsresultat-konto (8800)",
        domain="[('account_type', 'in', ('expense', 'income'))]",
        help="Transit-konto som P&L-saldoer lukkes mot ved årsavslutning. "
             "NS 4102 default kode 8800 'Årsresultat'. Hvis tom: søker "
             "etter code='8800' automatisk.",
    )
    l10n_no_skattemelding_annen_ek_account_id = fields.Many2one(
        'account.account',
        string="Annen egenkapital-konto (2050)",
        domain="[('account_type', '=', 'equity')]",
        help="EK-konto for overskudd som overføres til balanse. NS 4102 "
             "default kode 2050 'Annen egenkapital'. Hvis tom: søker "
             "etter code='2050' automatisk.",
    )
    l10n_no_skattemelding_udekket_tap_account_id = fields.Many2one(
        'account.account',
        string="Udekket tap-konto (2080)",
        domain="[('account_type', '=', 'equity')]",
        help="EK-konto for underskudd som overføres til balanse. NS 4102 "
             "default kode 2080 'Udekket tap'. Hvis tom: søker etter "
             "code='2080' automatisk.",
    )
    l10n_no_skattemelding_closing_journal_id = fields.Many2one(
        'account.journal',
        string="Avslutningsjournal",
        domain="[('type', '=', 'general')]",
        help="Journal som avslutningsbilag bokføres i. Typisk "
             "Miscellaneous/Diverse. Hvis tom: bruker første "
             "general-journal automatisk.",
    )

    @api.depends('l10n_no_eristo_active_scopes')
    def _compute_l10n_no_skattemelding_aktivert(self):
        for c in self:
            c.l10n_no_skattemelding_aktivert = c.l10n_no_eristo_is_scope_active(
                'skatteetaten:formueinntekt/skattemelding',
            )

    def action_l10n_no_skattemelding_activate(self):
        """Åpne onboarding-wizard for skattemelding-scope."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Aktiver Skattemelding"),
            'res_model': 'l10n.no.eristo.onboarding.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_company_id': self.id,
                'default_scopes_text': 'skatteetaten:formueinntekt/skattemelding',
                'default_service_name': 'Skattemelding for næringsdrivende',
            },
        }
