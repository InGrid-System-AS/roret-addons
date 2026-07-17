"""Integrasjonstester for Community-tallkilden + oppgjør.

Verifiserer at den egne tag-summeringen (_tax_report_code_values) gir
samme tall som Enterprise-motoren ville vist, med EKTE bilag på norsk
kontoplan: kundefaktura 1000 kr @ 25 % → BASE_3=1000/TAX_3=250,
leverandørfaktura 400 kr @ 25 % → TAX_1=100, fastsatt = 150.

Dekker også oppgjørssteget som erstatter Enterprise account.return:
oppgjørsbilag med 2740 i hele kroner. Betalingsordre-flyten (OCA) testes
i bro-modulen l10n_no_account_mvamelding_payment — denne modulen (og
dens tester) skal være kjørbar UTEN OCA installert (Produkt 2).
"""
from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.exceptions import UserError
from odoo.tests import tagged


@tagged('post_install_l10n', 'post_install', '-at_install')
class TestTaxSourceCommunity(AccountTestInvoicingCommon):
    @classmethod
    @AccountTestInvoicingCommon.setup_country('no')
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.company_data['company']
        # Syntetisk Tenor-orgnr så _orgnr resolver (samme mønster som
        # modulens øvrige tester)
        cls.company.sudo().l10n_no_eristo_test_orgnr = '310200808'
        cls.Tax = cls.env['account.tax']
        cls.sale_tax_25 = cls.Tax.search([
            ('company_id', '=', cls.company.id),
            ('type_tax_use', '=', 'sale'),
            ('amount', '=', 25.0),
            ('amount_type', '=', 'percent'),
        ], limit=1)
        cls.purchase_tax_25 = cls.Tax.search([
            ('company_id', '=', cls.company.id),
            ('type_tax_use', '=', 'purchase'),
            ('amount', '=', 25.0),
            ('amount_type', '=', 'percent'),
        ], limit=1)
        assert cls.sale_tax_25 and cls.purchase_tax_25, \
            "Norsk kontoplan mangler 25 %-avgifter"

        cls._post_invoice('out_invoice', 1000.0, cls.sale_tax_25,
                          '2026-03-15')
        cls._post_invoice('in_invoice', 400.0, cls.purchase_tax_25,
                          '2026-04-02')

        cls.mva = cls.env['l10n.no.mvamelding'].create({
            'company_id': cls.company.id,
            'aar': 2026,
            'periode': '2',  # mars–april
        })

    @classmethod
    def _post_invoice(cls, move_type, amount, tax, date_str):
        invoice = cls.env['account.move'].create({
            'move_type': move_type,
            'partner_id': cls.partner_a.id,
            'invoice_date': date_str,
            'date': date_str,
            'company_id': cls.company.id,
            'invoice_line_ids': [(0, 0, {
                'name': 'Testlinje',
                'quantity': 1,
                'price_unit': amount,
                'tax_ids': [(6, 0, tax.ids)],
            })],
        })
        invoice.action_post()
        return invoice

    # ---- tallkilde ----------------------------------------------------

    def test_code_values_from_real_moves(self):
        values = self.mva._tax_report_code_values()
        self.assertEqual(round(values.get('BASE_3', 0)), 1000)
        self.assertEqual(round(values.get('TAX_3', 0)), 250)
        self.assertEqual(round(values.get('TAX_1', 0)), 100)

    def test_collect_lines_and_fastsatt(self):
        lines, fastsatt = self.mva._collect_mva_lines()
        by_code = {ln['mva_kode']: ln for ln in lines}
        self.assertEqual(by_code['3']['grunnlag'], 1000)
        self.assertEqual(by_code['3']['merverdiavgift'], 250)
        self.assertEqual(by_code['1']['merverdiavgift'], -100)
        self.assertEqual(fastsatt, 150)

    def test_period_filter_excludes_other_terms(self):
        """Bilag utenfor terminen skal ikke telle med."""
        self._post_invoice('out_invoice', 500.0, self.sale_tax_25,
                           '2026-05-10')  # 3. termin
        values = self.mva._tax_report_code_values()
        self.assertEqual(round(values.get('BASE_3', 0)), 1000)

    # ---- oppgjør -------------------------------------------------------

    def _generate(self):
        self.mva.action_generate_xml()

    def test_oppgjor_posts_settlement_move(self):
        self._generate()
        self.mva.action_bokfor_oppgjor()
        move = self.mva.oppgjor_move_id
        self.assertEqual(move.state, 'posted')
        settlement = self.mva._l10n_no_get_settlement_account()
        line = move.line_ids.filtered(
            lambda l: l.account_id == settlement)
        self.assertEqual(len(line), 1)
        # Skyldig 150 kr → kreditlinje på oppgjørskontoen, hele kroner
        self.assertEqual(line.balance, -150.0)
        # MVA-kontoene skal være tømt for terminen (netto 0 inkl. oppgjør)
        tax_accounts = move.line_ids.account_id - settlement
        for account in tax_accounts.filtered(
                lambda a: a.code and a.code.startswith('27')):
            total = sum(self.env['account.move.line'].search([
                ('account_id', '=', account.id),
                ('company_id', '=', self.company.id),
                ('date', '>=', '2026-03-01'), ('date', '<=', '2026-04-30'),
                ('parent_state', '=', 'posted'),
            ]).mapped('balance'))
            self.assertAlmostEqual(total, 0.0, places=2)

    def test_oppgjor_idempotent(self):
        self._generate()
        self.mva.action_bokfor_oppgjor()
        with self.assertRaises(UserError) as ctx:
            self.mva.action_bokfor_oppgjor()
        self.assertIn('allerede bokført', str(ctx.exception))

    def test_core_only_ingen_oca_avhengighet(self):
        """Kjernens kontrakt (Produkt 2): modulen skal ikke kreve OCA.

        Verifiserer at manifestet ikke drar inn account_payment_order/
        sepa — betalingsordre-flyten hører til i bro-modulen
        l10n_no_account_mvamelding_payment.
        """
        module = self.env['ir.module.module'].search([
            ('name', '=', 'l10n_no_account_mvamelding')])
        deps = set(module.dependencies_id.mapped('name'))
        self.assertNotIn('account_payment_order', deps)
        self.assertNotIn('account_banking_sepa_credit_transfer', deps)
