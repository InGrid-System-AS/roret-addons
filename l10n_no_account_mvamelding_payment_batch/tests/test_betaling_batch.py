"""Betalingsbunt-flyten for MVA — Enterprise-speilet av OCA-broens tester.

Testene ER spesifikasjonen for betalingssteget på Enterprise-stacken:
betaling mot oppgjørslinjen (KID, beløp, Skatteetaten-bank, 2740 som
destination), bunt-medlemskap, avstemming, idempotens, krav om postert
oppgjør, og til gode-blokkering. Vaktene er identiske med OCA-broens
(l10n_no_account_mvamelding_payment/tests/test_betaling.py) — kun
mekanismen bak knappen er en annen.

Kan KUN kjøres der account_batch_payment finnes (Enterprise/Odoo.sh) —
i clean-room-monorepoet installeres modulen aldri, så testene lastes
ikke der. Første kjøring er Odoo.sh-bygget.

Gjenbruker kjernens fixtures (bilag + mva-melding) ved subklassing.
"""
from odoo import fields
from odoo.addons.l10n_no_account_mvamelding.tests.test_tax_source_community \
    import TestTaxSourceCommunity
from odoo.exceptions import UserError
from odoo.tests import tagged


@tagged('post_install_l10n', 'post_install', '-at_install')
class TestMvaBetalingBatch(TestTaxSourceCommunity):

    def _prepare_paid_state(self):
        self._generate()
        self.mva.action_bokfor_oppgjor()
        # Simuler mottatt kvittering med betalingsinfo (KID med gyldig
        # MOD10-sjekksiffer — samme fixtures som OCA-bro-testene)
        self.mva.write({
            'state': 'mottatt',
            'betalings_kid': '12345678901237',
            'betalingskonto': '76940517061',
            'betalingsbeloep': 150.0,
            'betalingsfrist': fields.Date.from_string('2026-06-10'),
        })
        # Deterministisk bilagsgenerering: i Odoo 19 får betalingen kun
        # eget bilag når betalingsmetoden har en outstanding-konto
        # (payment.write → _generate_journal_entry filtrerer på
        # outstanding_account_id). Uten den bokfører først bank-
        # avstemmingen — da ville avstemmings-asserten under vært
        # miljøavhengig.
        journal = self.company_data['default_journal_bank']
        method_line = journal.outbound_payment_method_line_ids[:1]
        if not method_line.payment_account_id:
            outstanding = self.env['account.account'].create({
                'code': '1961',
                'name': 'Utestående utbetalinger (test)',
                'account_type': 'asset_current',
                'reconcile': True,
            })
            method_line.payment_account_id = outstanding

    def test_betaling_creates_payment_and_batch(self):
        self._prepare_paid_state()
        action = self.mva.action_opprett_betaling_batch()
        batch = self.env['account.batch.payment'].browse(action['res_id'])
        self.assertEqual(self.mva.betaling_batch_id, batch)
        self.assertEqual(batch.batch_type, 'outbound')
        self.assertEqual(len(batch.payment_ids), 1)

        payment = batch.payment_ids
        self.assertEqual(self.mva.betaling_payment_id, payment)
        self.assertEqual(payment.payment_type, 'outbound')
        self.assertEqual(payment.amount, 150.0)
        self.assertEqual(payment.memo, '12345678901237')
        self.assertEqual(payment.partner_id.name, 'Skatteetaten')
        self.assertTrue(payment.partner_bank_id.allow_out_payment)
        self.assertEqual(batch.journal_id, payment.journal_id)

        settlement = self.mva._l10n_no_get_settlement_account()
        self.assertEqual(payment.destination_account_id, settlement)

    def test_betaling_reconciles_settlement_line(self):
        self._prepare_paid_state()
        self.mva.action_opprett_betaling_batch()
        payment = self.mva.betaling_payment_id

        # Outstanding-konto er konfigurert i _prepare_paid_state, så
        # bilaget skal finnes og 2740-linjen være avstemt mot det.
        self.assertTrue(payment.move_id, "betalingen skal ha eget bilag")
        settlement = self.mva._l10n_no_get_settlement_account()
        oppgjor_line = self.mva.oppgjor_move_id.line_ids.filtered(
            lambda l: l.account_id == settlement)
        self.assertTrue(oppgjor_line.reconciled,
                        "oppgjørslinjen (2740) skal være avstemt")
        motpart = payment.move_id.line_ids.filtered(
            lambda l: l.account_id == settlement)
        self.assertTrue(motpart.reconciled)
        self.assertIn(payment, self.mva.oppgjor_move_id.matched_payment_ids)

    def test_betaling_idempotent(self):
        self._prepare_paid_state()
        self.mva.action_opprett_betaling_batch()
        with self.assertRaises(UserError) as ctx:
            self.mva.action_opprett_betaling_batch()
        self.assertIn('finnes allerede', str(ctx.exception))

    def test_betaling_requires_oppgjor(self):
        self._generate()
        self.mva.write({
            'state': 'mottatt',
            'betalings_kid': '12345678901237',
            'betalingskonto': '76940517061',
            'betalingsbeloep': 150.0,
        })
        with self.assertRaises(UserError) as ctx:
            self.mva.action_opprett_betaling_batch()
        self.assertIn('oppgjør', str(ctx.exception).lower())

    def test_tilgode_blocks_payment(self):
        self._prepare_paid_state()
        self.mva.betalingsbeloep = -50.0
        with self.assertRaises(UserError) as ctx:
            self.mva.action_opprett_betaling_batch()
        self.assertIn('til gode', str(ctx.exception))
