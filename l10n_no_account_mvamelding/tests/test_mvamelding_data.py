"""Tester for Tax Report → mva-melding-linje-mapping (fortegn + avrunding).

Vi mocker ``_tax_report_code_values`` slik at mapping-logikken testes
deterministisk uten en full norsk avgiftsoppsett-fixture.
"""
from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from odoo.addons.l10n_no_account_mvamelding.models.l10n_no_mvamelding_data import (
    _nok,
)


@tagged('post_install', '-at_install')
class TestMvameldingData(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        # Syntetisk Tenor-orgnr så _orgnr resolver i test (TT02-mønster).
        cls.company.sudo().l10n_no_eristo_test_orgnr = '310200808'
        cls.mva = cls.env['l10n.no.mvamelding'].create({
            'company_id': cls.company.id,
            'aar': 2026,
            'periode': '2',
        })

    def _collect(self, code_values):
        with patch.object(
            type(self.mva), '_tax_report_code_values',
            return_value=code_values,
        ):
            return self.mva._collect_mva_lines()

    def test_nok_rounding_half_up(self):
        self.assertEqual(_nok(2.5), 3)
        self.assertEqual(_nok(3.4), 3)
        self.assertEqual(_nok(-2.5), -3)  # ties away from zero
        self.assertEqual(_nok(0), 0)

    def test_output_and_deduction_signs(self):
        """Kode 3 = utgående (positiv), kode 1 = fradrag (negativ)."""
        lines, fastsatt = self._collect({
            'BASE_3': 100000.0, 'TAX_3': 25000.0,
            'TAX_1': 5000.0,
        })
        by_code = {ln['mva_kode']: ln for ln in lines}

        self.assertEqual(by_code['3']['grunnlag'], 100000)
        self.assertEqual(by_code['3']['sats'], '25')
        self.assertEqual(by_code['3']['merverdiavgift'], 25000)

        self.assertIsNone(by_code['1']['grunnlag'])
        self.assertIsNone(by_code['1']['sats'])
        self.assertEqual(by_code['1']['merverdiavgift'], -5000)

        # Fastsatt = utgående - fradrag.
        self.assertEqual(fastsatt, 20000)

    def test_zerorate_line_has_base_zero_tax(self):
        lines, fastsatt = self._collect({'BASE_52': 25255.0})
        self.assertEqual(len(lines), 1)
        line = lines[0]
        self.assertEqual(line['mva_kode'], '52')
        self.assertEqual(line['grunnlag'], 25255)
        self.assertEqual(line['sats'], '0')
        self.assertEqual(line['merverdiavgift'], 0)
        self.assertEqual(fastsatt, 0)

    def test_empty_codes_skipped(self):
        """Koder med null verdi gir ingen linje."""
        lines, fastsatt = self._collect({
            'BASE_3': 0.0, 'TAX_3': 0.0, 'TAX_1': 0.0, 'BASE_52': 0.0,
        })
        self.assertEqual(lines, [])
        self.assertEqual(fastsatt, 0)

    def test_lines_sorted_by_numeric_code(self):
        lines, _f = self._collect({
            'BASE_52': 1000.0,
            'BASE_3': 2000.0, 'TAX_3': 500.0,
            'TAX_1': 100.0,
        })
        codes = [ln['mva_kode'] for ln in lines]
        self.assertEqual(codes, ['1', '3', '52'])

    def test_fastsatt_equals_line_sum(self):
        """Fastsatt MVA == Σ merverdiavgift (intern konsistens)."""
        lines, fastsatt = self._collect({
            'BASE_3': 449848.0, 'TAX_3': 112462.0,
            'TAX_1': 72351.0,
            'BASE_86': 14141.0, 'TAX_86': 3535.0,
        })
        self.assertEqual(
            fastsatt, sum(ln['merverdiavgift'] for ln in lines),
        )
        # 112462 + 3535 - 72351
        self.assertEqual(fastsatt, 43646)
