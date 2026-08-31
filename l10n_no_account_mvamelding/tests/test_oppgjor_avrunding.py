"""Avrundingslinjen i MVA-oppgjøret.

Skatteetaten fastsetter i HELE kroner, mens MVA-kontoene nesten alltid
har øre. Oppgjørsbilaget legger derfor øre-resten på avrundingskontoen —
og fortegnet der må være motsatt av `diff`, ellers går ikke bilaget i null:

    Σ linjer = Σ(-balance_i) + (-fastsatt) + avrunding
             = (-net_balance - fastsatt) + avrunding
             = diff + avrunding

Med `avrunding = diff` blir summen 2·diff, og bilaget lar seg ikke postere
for noen termin med øre-rest. Feilen overlevde fordi den eneste
oppgjørstesten brukte en fikstur på eksakt 150 kr, der diff er 0 og grenen
aldri kjøres. Verifisert i produksjon 2026-08-31: både Eristo (18 øre) og
InGrid (52 øre) feilet med «The entry is not balanced».

Testene her dekker begge avrundingsretninger.
"""
from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.tests import tagged


@tagged('post_install_l10n', 'post_install', '-at_install')
class TestOppgjorAvrunding(AccountTestInvoicingCommon):
    @classmethod
    @AccountTestInvoicingCommon.setup_country('no')
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.company_data['company']
        cls.company.sudo().l10n_no_eristo_test_orgnr = '310200808'
        cls.sale_tax_25 = cls.env['account.tax'].search([
            ('company_id', '=', cls.company.id),
            ('type_tax_use', '=', 'sale'),
            ('amount', '=', 25.0),
            ('amount_type', '=', 'percent'),
        ], limit=1)
        assert cls.sale_tax_25, "Norsk kontoplan mangler 25 %-salgsavgift"

        # 2. termin: 1000,40 @ 25 % = 250,10 → fastsatt 250, diff = +0,10
        cls._post_sale(1000.40, '2026-03-15')
        # 3. termin:  999,60 @ 25 % = 249,90 → fastsatt 250, diff = -0,10
        cls._post_sale(999.60, '2026-05-15')

        cls.mva_ned = cls._melding('2')
        cls.mva_opp = cls._melding('3')

    @classmethod
    def _post_sale(cls, amount, date_str):
        move = cls.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': cls.partner_a.id,
            'invoice_date': date_str,
            'date': date_str,
            'company_id': cls.company.id,
            'invoice_line_ids': [(0, 0, {
                'name': 'Testlinje',
                'quantity': 1,
                'price_unit': amount,
                'tax_ids': [(6, 0, cls.sale_tax_25.ids)],
            })],
        })
        move.action_post()
        return move

    @classmethod
    def _melding(cls, periode):
        return cls.env['l10n.no.mvamelding'].create({
            'company_id': cls.company.id,
            'aar': 2026,
            'periode': periode,
        })

    def _bokfor(self, mva):
        mva.action_generate_xml()
        mva.action_bokfor_oppgjor()
        return mva.oppgjor_move_id

    def _avrundingslinje(self, move):
        konto = self.mva_ned._l10n_no_get_rounding_account()
        return move.line_ids.filtered(lambda l: l.account_id == konto)

    def _oppgjorslinje(self, move, mva):
        konto = mva._l10n_no_get_settlement_account()
        return move.line_ids.filtered(lambda l: l.account_id == konto)

    # ---- selve regressjonen --------------------------------------------

    def test_bilag_balanserer_ved_ore_rest(self):
        """Kjernen: med feil fortegn feiler action_post på ubalanse."""
        for mva in (self.mva_ned, self.mva_opp):
            with self.subTest(periode=mva.periode):
                move = self._bokfor(mva)
                self.assertEqual(move.state, 'posted')
                self.assertAlmostEqual(
                    sum(move.line_ids.mapped('balance')), 0.0, places=2)

    def test_avrunding_nedover(self):
        """250,10 på konto → fastsatt 250 → avrunding krediteres 0,10."""
        move = self._bokfor(self.mva_ned)
        self.assertEqual(self.mva_ned.fastsatt_mva, 250)
        self.assertAlmostEqual(
            self._avrundingslinje(move).balance, -0.10, places=2)
        self.assertAlmostEqual(
            self._oppgjorslinje(move, self.mva_ned).balance, -250.0, places=2)

    def test_avrunding_oppover(self):
        """249,90 på konto → fastsatt 250 → avrunding debiteres 0,10."""
        move = self._bokfor(self.mva_opp)
        self.assertEqual(self.mva_opp.fastsatt_mva, 250)
        self.assertAlmostEqual(
            self._avrundingslinje(move).balance, 0.10, places=2)
        self.assertAlmostEqual(
            self._oppgjorslinje(move, self.mva_opp).balance, -250.0, places=2)

    def test_mva_kontoene_toemmes(self):
        """Etter oppgjøret skal terminens MVA-kontoer stå i null."""
        move = self._bokfor(self.mva_ned)
        settlement = self.mva_ned._l10n_no_get_settlement_account()
        rounding = self.mva_ned._l10n_no_get_rounding_account()
        for konto in move.line_ids.account_id - settlement - rounding:
            saldo = sum(self.env['account.move.line'].search([
                ('account_id', '=', konto.id),
                ('company_id', '=', self.company.id),
                ('date', '>=', '2026-03-01'), ('date', '<=', '2026-04-30'),
                ('parent_state', '=', 'posted'),
            ]).mapped('balance'))
            self.assertAlmostEqual(saldo, 0.0, places=2)
