"""Spesifikasjonstester for SAF-T Financial-eksporten (test-først).

Fasit: Skatteetatens offisielle XSD v1.30 (committet under schemas/ fra
github.com/Skatteetaten/saf-t) — XSD-validering av hele fila er
hovedtesten. I tillegg: totaler (TotalDebit == TotalCredit == bokført),
kontosaldoer (åpning/lukking), kunde/leverandør, standard mva-koder og
KID i CID-feltet.

Fixture: norsk kontoplan med kundefaktura (25 % MVA), leverandørfaktura
og et rent bilag — bevegelse før perioden gir åpningssaldo.
"""
import base64
from datetime import date

from lxml import etree

from odoo import Command
from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.exceptions import UserError
from odoo.modules import get_module_path
from odoo.tests import tagged

NSMAP = {'s': 'urn:StandardAuditFile-Taxation-Financial:NO'}


@tagged('post_install_l10n', 'post_install', '-at_install')
class TestSaftExport(AccountTestInvoicingCommon):
    @classmethod
    @AccountTestInvoicingCommon.setup_country('no')
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.company_data['company']
        cls.company.write({
            'company_registry': '936903479',
            'vat': 'NO936903479MVA',
            'phone': '+47 12345678',
            'email': 'post@test.no',
        })
        cls.sale_tax_25 = cls.env['account.tax'].search([
            ('company_id', '=', cls.company.id),
            ('type_tax_use', '=', 'sale'),
            ('amount', '=', 25.0), ('amount_type', '=', 'percent'),
        ], limit=1)
        cls.purchase_tax_25 = cls.env['account.tax'].search([
            ('company_id', '=', cls.company.id),
            ('type_tax_use', '=', 'purchase'),
            ('amount', '=', 25.0), ('amount_type', '=', 'percent'),
        ], limit=1)

        # Bevegelse FØR perioden → åpningssaldo (1000 på bank/salg)
        cls._entry(date(2025, 11, 5), [
            {'account_id': cls.company_data['default_journal_bank']
                .default_account_id.id, 'debit': 1000.0, 'name': 'IB'},
            {'account_id': cls.company_data['default_account_revenue'].id,
             'credit': 1000.0, 'name': 'IB'},
        ])

        # Kundefaktura i perioden: 2000 + 500 MVA, med KID
        cls.out_inv = cls.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': cls.partner_a.id,
            'invoice_date': date(2026, 2, 10),
            'date': date(2026, 2, 10),
            'company_id': cls.company.id,
            'payment_reference': '12345678901237',  # gyldig KID (MOD10)
            'invoice_line_ids': [Command.create({
                'name': 'Vare', 'quantity': 1, 'price_unit': 2000.0,
                'tax_ids': [Command.set(cls.sale_tax_25.ids)],
            })],
        })
        cls.out_inv.action_post()

        # Leverandørfaktura i perioden: 800 + 200 MVA
        cls.in_inv = cls.env['account.move'].create({
            'move_type': 'in_invoice',
            'partner_id': cls.partner_b.id,
            'invoice_date': date(2026, 3, 5),
            'date': date(2026, 3, 5),
            'company_id': cls.company.id,
            'invoice_line_ids': [Command.create({
                'name': 'Tjeneste', 'quantity': 1, 'price_unit': 800.0,
                'tax_ids': [Command.set(cls.purchase_tax_25.ids)],
            })],
        })
        cls.in_inv.action_post()

        cls.export = cls.env['l10n.no.saft.export'].create({
            'company_id': cls.company.id,
            'date_from': date(2026, 1, 1),
            'date_to': date(2026, 12, 31),
            'kontakt_fornavn': 'Erik',
            'kontakt_etternavn': 'Stokkeland',
        })

    @classmethod
    def _entry(cls, dt, lines):
        move = cls.env['account.move'].create({
            'move_type': 'entry',
            'journal_id': cls.company_data['default_journal_misc'].id,
            'date': dt,
            'company_id': cls.company.id,
            'line_ids': [Command.create(v) for v in lines],
        })
        move.action_post()
        return move

    def _generate_tree(self):
        self.export.action_generate()
        self.assertTrue(self.export.saft_file, "ingen fil generert")
        xml_bytes = base64.b64decode(self.export.saft_file)
        return etree.fromstring(xml_bytes)

    # ---- Hovedtesten: offisiell XSD-validering -------------------------

    def test_validates_against_official_xsd(self):
        tree = self._generate_tree()
        xsd_path = get_module_path('l10n_no_saft') + \
            '/schemas/saft_financial_1_30.xsd'
        schema = etree.XMLSchema(etree.parse(xsd_path))
        valid = schema.validate(tree)
        self.assertTrue(valid, "XSD-feil: %s" % schema.error_log)

    # ---- Header ---------------------------------------------------------

    def test_header_fields(self):
        tree = self._generate_tree()
        h = tree.find('s:Header', NSMAP)
        self.assertEqual(h.findtext('s:AuditFileCountry', namespaces=NSMAP), 'NO')
        self.assertEqual(
            h.findtext('s:DefaultCurrencyCode', namespaces=NSMAP), 'NOK')
        self.assertEqual(
            h.findtext('s:Company/s:RegistrationNumber', namespaces=NSMAP),
            '936903479')
        self.assertEqual(
            h.findtext('s:TaxAccountingBasis', namespaces=NSMAP), 'A')
        self.assertEqual(
            h.findtext('s:SelectionCriteria/s:SelectionStartDate',
                       namespaces=NSMAP), '2026-01-01')

    # ---- Kontosaldoer -----------------------------------------------------

    def test_account_opening_and_closing_balances(self):
        tree = self._generate_tree()
        bank_code = self.company_data['default_journal_bank'] \
            .default_account_id.code
        for acc in tree.findall(
                's:MasterFiles/s:GeneralLedgerAccounts/s:Account', NSMAP):
            if acc.findtext('s:AccountID', namespaces=NSMAP) == bank_code:
                self.assertEqual(
                    acc.findtext('s:OpeningDebitBalance', namespaces=NSMAP),
                    '1000.00')
                self.assertEqual(
                    acc.findtext('s:ClosingDebitBalance', namespaces=NSMAP),
                    '1000.00')
                self.assertTrue(
                    acc.findtext('s:GroupingCode', namespaces=NSMAP))
                self.assertEqual(
                    acc.findtext('s:AccountType', namespaces=NSMAP), 'GL')
                break
        else:
            self.fail("bankkontoen mangler i GeneralLedgerAccounts")

    # ---- Hovedbok: totaler + KID + mva -----------------------------------

    def test_entries_totals_balance(self):
        tree = self._generate_tree()
        gle = tree.find('s:GeneralLedgerEntries', NSMAP)
        total_debit = float(gle.findtext('s:TotalDebit', namespaces=NSMAP))
        total_credit = float(gle.findtext('s:TotalCredit', namespaces=NSMAP))
        self.assertEqual(total_debit, total_credit)
        # 2500 (kundefaktura) + 1000 (leverandørfaktura) = 3500
        self.assertEqual(total_debit, 3500.0)
        n = int(gle.findtext('s:NumberOfEntries', namespaces=NSMAP))
        self.assertEqual(n, 2)

    def test_kid_emitted_as_cid(self):
        tree = self._generate_tree()
        cids = [e.text for e in tree.findall(
            's:GeneralLedgerEntries/s:Journal/s:Transaction/s:Line/s:CID',
            NSMAP)]
        self.assertIn('12345678901237', cids)

    def test_tax_information_on_tax_lines(self):
        tree = self._generate_tree()
        infos = tree.findall(
            's:GeneralLedgerEntries/s:Journal/s:Transaction/s:Line/'
            's:TaxInformation', NSMAP)
        self.assertTrue(infos, "ingen TaxInformation på mva-linjer")
        by_code = {}
        for info in infos:
            code = info.findtext('s:StandardTaxCode', namespaces=NSMAP) or \
                info.findtext('s:TaxCode', namespaces=NSMAP)
            by_code[code] = info
        # Utgående 25 % = standard mva-kode 3, inngående = 1
        self.assertIn('3', by_code)
        self.assertIn('1', by_code)

    def test_customer_and_supplier_masterfiles(self):
        tree = self._generate_tree()
        cust_ids = [c.findtext('s:CustomerID', namespaces=NSMAP)
                    for c in tree.findall(
                        's:MasterFiles/s:Customers/s:Customer', NSMAP)]
        supp_ids = [s.findtext('s:SupplierID', namespaces=NSMAP)
                    for s in tree.findall(
                        's:MasterFiles/s:Suppliers/s:Supplier', NSMAP)]
        self.assertIn(str(self.partner_a.id), cust_ids)
        self.assertIn(str(self.partner_b.id), supp_ids)

    def test_requires_orgnr(self):
        self.company.company_registry = False
        self.company.vat = False
        with self.assertRaises(UserError):
            self.export.action_generate()

    def test_section_and_note_lines_excluded(self):
        # Regresjonstest fra validering mot ekte produksjonsdata:
        # seksjons-/notatlinjer har ingen konto, og _read_group ga da en
        # tom kontogruppe som ble til <AccountID>False</AccountID> med
        # grupperingskode 'Fals' i MasterFiles.
        inv = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner_a.id,
            'invoice_date': date(2026, 4, 10),
            'date': date(2026, 4, 10),
            'company_id': self.company.id,
            'invoice_line_ids': [
                Command.create({'name': 'Seksjon',
                                'display_type': 'line_section'}),
                Command.create({'name': 'Vare', 'quantity': 1,
                                'price_unit': 100.0,
                                'tax_ids': [Command.set(
                                    self.sale_tax_25.ids)]}),
                Command.create({'name': 'Notat',
                                'display_type': 'line_note'}),
            ],
        })
        inv.action_post()
        tree = self._generate_tree()
        ns = {'s': tree.tag.split('}')[0][1:]}
        account_ids = [e.text for e in tree.findall(
            './/s:GeneralLedgerAccounts/s:Account/s:AccountID', ns)]
        self.assertNotIn('False', account_ids)
        self.assertTrue(all(a and a.isdigit() for a in account_ids),
                        f"ikke-numeriske AccountID-er: {account_ids}")
