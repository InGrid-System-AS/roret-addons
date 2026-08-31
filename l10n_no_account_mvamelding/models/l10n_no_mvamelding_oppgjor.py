"""MVA-oppgjør — core-only (Community OG Enterprise).

Erstatter Enterprise account.return-hubens oppgjørssteg (account_reports):
der posterte den native returen oppgjørsbilaget via return-wizardene.
Her eier MVA-meldingen selv steget:

  Generer XML → Send inn → (kvittering m/KID hentes) →
  «Bokfør MVA-oppgjør» → («Opprett betaling» — bro-modulen)

**Oppgjørsbilaget** tømmer MVA-kontoene (repartition-linjer med
use_in_tax_closing) for terminen mot oppgjørskontoen (2740, fra
skattegruppenes tax_payable_account_id — community-felt, satt av
l10n_no-charten). Oppgjørskonto-linjen føres i HELE KRONER (= fastsatt
merverdiavgift, slik Skatteetaten fastsetter og fakturerer); øre-resten
føres til avrundingskontoen (7740) i samme bilag. Dermed matcher den åpne
2740-linjen betalingsbeløpet eksakt.

**Betalingen** (KID → OCA-betalingsordre → pain.001) ligger i bro-modulen
`l10n_no_account_mvamelding_payment` — denne fila skal IKKE importere
OCA-modeller, slik at kjernen kjører på enhver Odoo 19 uten OCA
(Produkt 2: modulkunder på Odoo.sh/Enterprise).

Sikkerhetsvakter (portet fra Enterprise-utgaven):
  * Avvik mellom MVA-kontoenes saldo og fastsatt > 2 kr → ABORT med
    feilmelding (reell feil, ikke avrunding — skal aldri maskeres).
  * Idempotens: oppgjørsbilaget opprettes aldri dobbelt (felt-vakt,
    samme mønster som bank-broen).
  * Til gode (fastsatt < 0): oppgjør posteres (2740 debet) — Skatteetaten
    utbetaler selv (broen nekter også utbetaling).
"""
import logging

from odoo import _, Command, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Skatteetaten fastsetter MVA i hele kroner; øre-resten mellom MVA-kontoenes
# saldo og fastsatt beløp er < 1 kr. En STØRRE differanse er en reell feil
# (manglende bilag, feilført MVA-konto) og skal IKKE føres stille til
# avrundingskontoen.
_MAX_MVA_ROUNDING_NOK = 2.0


class L10nNoMvamelding(models.Model):
    _inherit = 'l10n.no.mvamelding'

    oppgjor_move_id = fields.Many2one(
        'account.move', string="MVA-oppgjørsbilag", copy=False, readonly=True,
        help="Bilaget som tømmer MVA-kontoene for terminen mot "
             "oppgjørskontoen (2740). Idempotens-vakt: posteres aldri "
             "dobbelt for samme termin.",
    )

    # ------------------------------------------------------------------
    # Oppgjørsbilag
    # ------------------------------------------------------------------

    def action_bokfor_oppgjor(self):
        """Poster MVA-oppgjørsbilaget for terminen."""
        self.ensure_one()
        if self.oppgjor_move_id and self.oppgjor_move_id.state != 'cancel':
            raise UserError(_(
                "Oppgjørsbilaget %(m)s er allerede bokført for denne "
                "terminen.", m=self.oppgjor_move_id.name))
        if not self.xml_generated_at:
            raise UserError(_(
                "Generer MVA-meldingen først — fastsatt beløp må være "
                "beregnet før oppgjøret bokføres."))

        _element, _value, date_from, date_to = self._periode_spec()
        company = self.company_id
        fastsatt = self.fastsatt_mva  # hele kroner (fra _collect_mva_lines)

        # MVA-kontoene: alle kontoer brukt av repartition-linjer med
        # use_in_tax_closing på selskapets norske avgifter.
        rep_lines = self.env['account.tax.repartition.line'].search([
            ('company_id', '=', company.id),
            ('use_in_tax_closing', '=', True),
            ('account_id', '!=', False),
        ])
        tax_accounts = rep_lines.account_id
        if not tax_accounts:
            raise UserError(_(
                "Fant ingen MVA-kontoer (use_in_tax_closing) for %(c)s — "
                "er den norske kontoplanen installert?", c=company.name))

        settlement = self._l10n_no_get_settlement_account()

        groups = self.env['account.move.line']._read_group(
            domain=[
                ('account_id', 'in', tax_accounts.ids),
                ('company_id', '=', company.id),
                ('date', '>=', date_from),
                ('date', '<=', date_to),
                ('parent_state', '=', 'posted'),
            ],
            groupby=['account_id'],
            aggregates=['balance:sum'],
        )
        lines = []
        net_balance = 0.0
        for account, balance in groups:
            if company.currency_id.is_zero(balance):
                continue
            net_balance += balance
            lines.append(Command.create({
                'account_id': account.id,
                'balance': -balance,
                'name': _("MVA-oppgjør %(navn)s", navn=self.display_name),
            }))
        if not lines:
            raise UserError(_(
                "MVA-kontoene har ingen bevegelser i terminen — ingenting "
                "å gjøre opp."))

        # Konsistensvakt: kontoenes netto (skyldig = negativ saldo) skal
        # matche fastsatt beløp innenfor øre-avrunding. Avvik = reell feil.
        diff = -net_balance - fastsatt
        if abs(diff) > _MAX_MVA_ROUNDING_NOK:
            raise UserError(_(
                "MVA-kontoenes saldo for terminen (%(saldo).2f kr) avviker "
                "%(diff).2f kr fra fastsatt merverdiavgift (%(fastsatt).2f "
                "kr) — mer enn øre-avrunding. Undersøk bilagene i terminen "
                "før oppgjøret bokføres.",
                saldo=-net_balance, diff=diff, fastsatt=fastsatt))

        # Oppgjørskonto-linje i HELE KRONER (= det Skatteetaten fakturerer);
        # øre-resten til avrundingskontoen i samme bilag.
        lines.append(Command.create({
            'account_id': settlement.id,
            'balance': -fastsatt,
            'name': _("Fastsatt merverdiavgift %(navn)s",
                      navn=self.display_name),
        }))
        if not company.currency_id.is_zero(diff):
            rounding = self._l10n_no_get_rounding_account()
            if not rounding:
                raise UserError(_(
                    "Øre-rest på %(diff).2f kr, men fant ingen "
                    "avrundingskonto (7740). Opprett kontoen først.",
                    diff=diff))
            lines.append(Command.create({
                # -diff, ikke diff: summen av de øvrige linjene er allerede
                # (-net_balance - fastsatt) == diff, så avrundingslinjen må
                # være motsatt for at bilaget skal gå i null. Med 'diff' ble
                # summen 2*diff, og bilaget lot seg aldri postere når
                # terminen hadde øre-rest.
                'account_id': rounding.id,
                'balance': -diff,
                'name': _("MVA-oppgjør avrunding (Skatteetaten fastsetter "
                          "hele kroner)"),
            }))

        journal = self.env['account.journal'].search([
            ('type', '=', 'general'),
            ('company_id', '=', company.id),
        ], limit=1)
        if not journal:
            raise UserError(_(
                "Fant ingen diversejournal (type «general») for %(c)s.",
                c=company.name))

        move = self.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': journal.id,
            'date': date_to,
            'ref': _("MVA-oppgjør %(navn)s", navn=self.display_name),
            'company_id': company.id,
            'line_ids': lines,
        })
        move.action_post()
        self.oppgjor_move_id = move.id
        self.message_post(body=_(
            "MVA-oppgjør bokført: %(m)s. Oppgjørskonto %(konto)s: "
            "%(sum).0f kr.", m=move.name,
            konto=settlement.code or settlement.name, sum=fastsatt))
        return True

    # ------------------------------------------------------------------
    # Hjelpere (portet fra Enterprise-utgaven)
    # ------------------------------------------------------------------

    def _l10n_no_get_settlement_account(self):
        """Oppgjørskontoen (2740) fra skattegruppene — community-feltet
        tax_payable_account_id, satt av l10n_no-charten."""
        self.ensure_one()
        group = self.env['account.tax.group'].search([
            ('company_id', '=', self.company_id.id),
            ('tax_payable_account_id', '!=', False),
        ], limit=1)
        account = group.tax_payable_account_id
        if not account:
            raise UserError(_(
                "Fant ikke MVA-oppgjørskonto (tax_payable_account_id på "
                "skattegruppene) for %(c)s.", c=self.company_id.name))
        return account

    def _l10n_no_get_rounding_account(self):
        """Selskapets avrundingskonto (7740 «Rounding»)."""
        self.ensure_one()
        Account = self.env['account.account']
        domain = [('company_ids', 'in', self.company_id.id),
                  ('account_type', '=', 'expense')]
        return Account.search(
            [('code', '=', '7740')] + domain, limit=1,
        ) or Account.search(
            [('name', '=', 'Rounding')] + domain, limit=1, order='code',
        )

    def _l10n_no_get_skatteetaten_bank(self):
        """Finn (eller opprett) Skatteetatens bankkonto fra
        betalingsinformasjonen i kvitteringen.

        Kun core-modeller (res.partner.bank) — ligger her (ikke i
        betalingsbroen) fordi bankkontoen fra kvitteringen er nyttig
        uavhengig av OCA-betalingsordre-flyten.
        """
        self.ensure_one()
        Bank = self.env['res.partner.bank'].sudo()
        sanitized = (self.betalingskonto or '').replace(' ', '')
        bank = Bank.search([
            ('sanitized_acc_number', '=', sanitized),
        ], order='id', limit=1)
        if bank:
            if not bank.allow_out_payment:
                bank.allow_out_payment = True
            return bank
        partner = self.env['res.partner'].search([
            ('name', '=', 'Skatteetaten'),
        ], limit=1) or self.env['res.partner'].create({
            'name': 'Skatteetaten', 'is_company': True,
            'country_id': self.env.ref('base.no').id,
        })
        return Bank.create({
            'acc_number': self.betalingskonto,
            'partner_id': partner.id,
            'allow_out_payment': True,
        })
