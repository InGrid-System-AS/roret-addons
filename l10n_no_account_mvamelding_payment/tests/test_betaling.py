"""Betalingsordre-flyten for MVA (flyttet fra kjernen ved splitten 2026-07).

Testene er identiske med de som lå i l10n_no_account_mvamelding/tests/
test_tax_source_community.py før splitten — de ER spesifikasjonen for
betalingssteget: ordre-linje mot oppgjørslinjen (KID, beløp, Skatteetaten-
bank), idempotens, krav om postert oppgjør, og til gode-blokkering.

Gjenbruker kjernens fixtures (bilag + mva-melding) ved subklassing.
"""
from odoo import fields
from odoo.addons.l10n_no_account_mvamelding.tests.test_tax_source_community \
    import TestTaxSourceCommunity
from odoo.exceptions import UserError
from odoo.tests import tagged


@tagged('post_install_l10n', 'post_install', '-at_install')
class TestMvaBetaling(TestTaxSourceCommunity):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Betalingsordre-tilgang (OCA-gruppe)
        cls.env.user.group_ids |= cls.env.ref(
            'account_payment_order.group_account_payment')

    def _prepare_paid_state(self):
        self._generate()
        self.mva.action_bokfor_oppgjor()
        # Simuler mottatt kvittering med betalingsinfo (KID med gyldig
        # MOD10-sjekksiffer — samme som dnb-testene bruker)
        self.mva.write({
            'state': 'mottatt',
            'betalings_kid': '12345678901237',
            'betalingskonto': '76940517061',
            'betalingsbeloep': 150.0,
            'betalingsfrist': fields.Date.from_string('2026-06-10'),
        })
        # Betalingsmodus (SEPA Credit Transfer) på bankjournalen
        method = self.env.ref(
            'account_banking_sepa_credit_transfer.sepa_credit_transfer')
        journal = self.company_data['default_journal_bank']
        self.env['account.payment.mode'].create({
            'name': 'DNB pain.001 mva-test',
            'company_id': self.company.id,
            'bank_account_link': 'fixed',
            'fixed_journal_id': journal.id,
            'payment_method_id': method.id,
        })

    def test_betaling_creates_order_line_against_settlement(self):
        self._prepare_paid_state()
        action = self.mva.action_opprett_betaling()
        order = self.env['account.payment.order'].browse(action['res_id'])
        self.assertEqual(self.mva.betaling_order_id, order)
        self.assertEqual(len(order.payment_line_ids), 1)
        line = order.payment_line_ids
        self.assertEqual(line.amount_currency, 150.0)
        self.assertEqual(line.communication, '12345678901237')
        settlement = self.mva._l10n_no_get_settlement_account()
        self.assertEqual(line.move_line_id.account_id, settlement)
        self.assertEqual(line.partner_id.name, 'Skatteetaten')
        self.assertTrue(line.partner_bank_id.allow_out_payment)

    def test_betaling_idempotent(self):
        self._prepare_paid_state()
        self.mva.action_opprett_betaling()
        with self.assertRaises(UserError) as ctx:
            self.mva.action_opprett_betaling()
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
            self.mva.action_opprett_betaling()
        self.assertIn('oppgjør', str(ctx.exception).lower())

    def test_tilgode_blocks_payment(self):
        self._prepare_paid_state()
        self.mva.betalingsbeloep = -50.0
        self.mva.betaling_order_id = False
        with self.assertRaises(UserError) as ctx:
            self.mva.action_opprett_betaling()
        self.assertIn('til gode', str(ctx.exception))
