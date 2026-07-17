"""MVA-betaling via OCA betalingsordre — flyttet fra kjernen (2026-07).

Koden er uendret fra l10n_no_account_mvamelding (l10n_no_mvamelding_oppgjor
.py før splitten); kun hjemmen er ny. Kjernen skal være core-only så den
kjører på enhver Odoo 19 uten OCA (Produkt 2) — all bruk av
account.payment.order/mode/line bor derfor her.

Sikkerhetsvakter (uendret):
  * Idempotens: betalingsordre-linjen opprettes aldri dobbelt (felt-vakt).
  * Til gode (fastsatt < 0): ingen utbetaling opprettes — Skatteetaten
    utbetaler selv.
  * Oppgjørsbilaget må være postert og oppgjørslinjen åpen og lik
    betalingsbeløpet fra kvitteringen — ellers abort med klar melding.
"""
import logging

from odoo import _, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class L10nNoMvamelding(models.Model):
    _inherit = 'l10n.no.mvamelding'

    betaling_order_id = fields.Many2one(
        'account.payment.order', string="Betalingsordre", copy=False,
        readonly=True,
        help="OCA-betalingsordren som inneholder MVA-betalingen til "
             "Skatteetaten (KID fra kvitteringen). Idempotens-vakt: hindrer "
             "at gjentatte klikk lager dobbel betaling.",
    )

    def action_opprett_betaling(self):
        """Legg MVA-betalingen (KID fra kvitteringen) i en betalingsordre."""
        self.ensure_one()
        company = self.company_id

        # Idempotens: en levende ordre for terminen blokkerer ny betaling.
        existing = self.betaling_order_id
        if existing and existing.state != 'cancel':
            raise UserError(_(
                "Betalingsordre %(o)s finnes allerede for denne terminen "
                "(status: %(s)s).", o=existing.name, s=existing.state))

        if self.state != 'mottatt' or not (
                self.betalings_kid and self.betalingskonto):
            raise UserError(_(
                "Skatteetaten har ikke sendt betalingsinformasjon (KID) "
                "ennå. Den hentes automatisk etter innsending — prøv igjen "
                "om et par minutter."))
        if self.currency_id.compare_amounts(self.betalingsbeloep, 0) <= 0:
            raise UserError(_(
                "Terminen er til gode (%(b).2f kr) — Skatteetaten utbetaler "
                "selv; ingen utbetaling skal opprettes.",
                b=self.betalingsbeloep))
        if not self.oppgjor_move_id or self.oppgjor_move_id.state != 'posted':
            raise UserError(_(
                "Bokfør MVA-oppgjøret først — betalingen avstemmes mot "
                "oppgjørskontoen."))

        settlement = self._l10n_no_get_settlement_account()
        settlement_line = self.oppgjor_move_id.line_ids.filtered(
            lambda l: l.account_id == settlement and not l.reconciled)
        if len(settlement_line) != 1:
            raise UserError(_(
                "Fant ikke en åpen oppgjørskonto-linje i %(m)s å betale "
                "mot (funnet: %(n)d).", m=self.oppgjor_move_id.name,
                n=len(settlement_line)))
        residual = -settlement_line.amount_residual  # kredit → positivt
        if company.currency_id.compare_amounts(
                residual, self.betalingsbeloep) != 0:
            raise UserError(_(
                "Oppgjørskonto-linjen (%(r).2f kr) matcher ikke "
                "betalingsbeløpet fra Skatteetaten (%(b).2f kr). Undersøk "
                "oppgjørsbilaget før betaling.",
                r=residual, b=self.betalingsbeloep))

        mode = self.env['account.payment.mode'].search([
            ('company_id', '=', company.id),
            ('payment_method_id.code', '=', 'sepa_credit_transfer'),
            ('payment_type', '=', 'outbound'),
        ], limit=1)
        if not mode:
            raise UserError(_(
                "Fant ingen betalingsmodus (SEPA Credit Transfer) for "
                "%(c)s — opprett en under Fakturering → Konfigurasjon → "
                "Betalingsmoduser.", c=company.name))

        bank = self._l10n_no_get_skatteetaten_bank()

        order = self.env['account.payment.order'].create({
            'payment_mode_id': mode.id,
            'payment_type': 'outbound',
        })
        vals = settlement_line._prepare_payment_line_vals(order)
        vals.update({
            'partner_id': bank.partner_id.id,
            'partner_bank_id': bank.id,
            'communication': self.betalings_kid,
            'date': self.betalingsfrist or fields.Date.context_today(self),
        })
        self.env['account.payment.line'].create(vals)

        self.betaling_order_id = order.id
        self.message_post(body=_(
            "MVA-betaling lagt i betalingsordre %(o)s: %(b).2f kr til "
            "Skatteetaten, KID %(kid)s, frist %(frist)s. Bekreft ordren og "
            "send til bank — oppgjørskontoen avstemmes automatisk når "
            "ordren effektueres.",
            o=order.name, b=self.betalingsbeloep, kid=self.betalings_kid,
            frist=self.betalingsfrist or '—'))
        order.message_post(body=_(
            "MVA-betaling for %(navn)s (KID %(kid)s) — opprettet fra "
            "MVA-meldingen.", navn=self.display_name,
            kid=self.betalings_kid))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.payment.order',
            'res_id': order.id,
            'view_mode': 'form',
            'target': 'current',
        }
