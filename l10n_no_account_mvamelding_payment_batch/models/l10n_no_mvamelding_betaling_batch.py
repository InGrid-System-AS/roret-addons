"""MVA-betaling via Enterprise betalingsbunt — Produkt 2 (egen Odoo.sh).

Speiler l10n_no_account_mvamelding_payment (OCA-broen) mot
Enterprise-stacken. Kjernen er core-only og kjenner ingen av delene.

Design (mot kjerne-API, verifisert i Odoo 19 Community-kilden):

  * account.payment opprettes EKSPLISITT — ikke via
    account.payment.register. Wizardens partner_id er computed uten
    readonly=False og utledes fra linjene, og oppgjørslinjen har ingen
    partner; wizarden ville gitt en partnerløs betaling som forkastes
    på require_partner_bank_account-sjekken.
  * destination_account_id settes til oppgjørskontoen (2740). Feltet er
    settbart (readonly=False), domenet er nettopp
    asset_receivable/liability_payable, og motkontolinjen bruker det
    direkte (_prepare_move_counterpart_lines). 2740 er liability_payable
    og reconcile=True i l10n_no-charten.
  * Odoo 19 gir betalingen eget bilag KUN når betalingsmetoden har
    outstanding-konto (payment.write → _generate_journal_entry filtrerer
    på outstanding_account_id). Har den det, avstemmer vi 2740-linjene
    direkte (samme grep som wizardens _reconcile_payments). Har den det
    ikke, bokfører bankavstemmingen senere — da linkes betalingen via
    matched_payment_ids, og oppgjørslinjen avstemmes i bankavstemmingen.

KID: settes som memo. l10n_no_dnb_payments konverterer numeriske
referanser >= 11 sifre med gyldig MOD10/MOD11 fra <Ustrd> til
<Strd>/CdtrRefInf/SCOR ved pain.001-eksport — samme mekanisme OCA-broen
bygger på. Betalingsmetode overstyres ikke: den defaulter fra
bankjournalen, og l10n_no_dnb_payments har egen pre-eksport-validering.

Sikkerhetsvaktene er identiske med OCA-broen:
  * Idempotens: levende betaling for terminen blokkerer ny.
  * Til gode (fastsatt < 0): ingen utbetaling — Skatteetaten utbetaler selv.
  * Selskapsisolasjon: kjernens ir.rule på l10n.no.mvamelding.
  * Oppgjørsbilaget må være postert, og oppgjørslinjen åpen og lik
    betalingsbeløpet fra kvitteringen — ellers abort med klar melding.

Penger flyttes aldri herfra: bunten må godkjennes i nettbanken.
"""
import logging

from odoo import Command, _, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Betalinger i disse tilstandene blokkerer ny betaling for terminen.
# 'canceled'/'rejected' gjør det ikke — da skal man kunne prøve igjen.
_LEVENDE_BETALING = {'draft', 'in_process', 'paid'}


class L10nNoMvamelding(models.Model):
    _inherit = 'l10n.no.mvamelding'

    betaling_payment_id = fields.Many2one(
        'account.payment', string="MVA-betaling", copy=False, readonly=True,
        help="Betalingen til Skatteetaten (KID fra kvitteringen), ført mot "
             "oppgjørskontoen. Idempotens-vakt: hindrer at gjentatte klikk "
             "lager dobbel betaling.",
    )
    betaling_batch_id = fields.Many2one(
        'account.batch.payment', string="Betalingsbunt", copy=False,
        readonly=True,
        help="Betalingsbunten MVA-betalingen ligger i. Det er bunten som "
             "eksporteres til pain.001 og sendes til banken.",
    )

    def action_opprett_betaling_batch(self):
        """Registrer MVA-betalingen (KID fra kvitteringen) i en betalingsbunt."""
        self.ensure_one()
        company = self.company_id

        existing = self.betaling_payment_id
        if existing and existing.state in _LEVENDE_BETALING:
            raise UserError(_(
                "Betaling %(p)s finnes allerede for denne terminen "
                "(status: %(s)s).", p=existing.display_name,
                s=existing.state))

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
                "Bokfør MVA-oppgjøret først — betalingen føres mot "
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

        journal = self.env['account.journal'].search([
            ('type', '=', 'bank'),
            ('company_id', '=', company.id),
        ], limit=1)
        if not journal:
            raise UserError(_(
                "Fant ingen bankjournal for %(c)s — opprett en under "
                "Regnskap → Konfigurasjon → Journaler.", c=company.name))

        bank = self._l10n_no_get_skatteetaten_bank()

        payment = self.env['account.payment'].create({
            'payment_type': 'outbound',
            'partner_type': 'supplier',
            'partner_id': bank.partner_id.id,
            'partner_bank_id': bank.id,
            'journal_id': journal.id,
            'destination_account_id': settlement.id,
            'amount': self.betalingsbeloep,
            'date': self.betalingsfrist or fields.Date.context_today(self),
            'memo': self.betalings_kid,
        })
        payment.action_post()

        if payment.move_id:
            # Metoden har outstanding-konto → bilag finnes; avstem
            # 2740-linjene direkte (samme grep som register-wizardens
            # _reconcile_payments).
            counterpart = payment.move_id.line_ids.filtered(
                lambda l: l.account_id == settlement and not l.reconciled)
            (counterpart + settlement_line).reconcile()
        self.oppgjor_move_id.matched_payment_ids = [Command.link(payment.id)]

        batch = self.env['account.batch.payment'].create({
            'journal_id': journal.id,
            'batch_type': 'outbound',
            'payment_ids': [Command.set(payment.ids)],
        })

        self.write({
            'betaling_payment_id': payment.id,
            'betaling_batch_id': batch.id,
        })
        self.message_post(body=_(
            "MVA-betaling lagt i betalingsbunt %(o)s: %(b).2f kr til "
            "Skatteetaten, KID %(kid)s, frist %(frist)s. Eksporter bunten "
            "og godkjenn i nettbanken.",
            o=batch.name, b=self.betalingsbeloep, kid=self.betalings_kid,
            frist=self.betalingsfrist or '—'))
        batch.message_post(body=_(
            "MVA-betaling for %(navn)s (KID %(kid)s) — opprettet fra "
            "MVA-meldingen.", navn=self.display_name,
            kid=self.betalings_kid))
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.batch.payment',
            'res_id': batch.id,
            'view_mode': 'form',
            'target': 'current',
        }
