"""Tester for mva-melding + konvolutt XML-bygging (struktur, rekkefølge, ns)."""
from unittest.mock import patch

from lxml import etree

from odoo.tests import TransactionCase, tagged

NS_M = 'no:skatteetaten:fastsetting:avgift:mva:skattemeldingformerverdiavgift:v1.0'
NS_K = 'no:skatteetaten:fastsetting:avgift:mva:mvameldinginnsending:v1.0'


def _localnames(parent):
    return [etree.QName(c).localname for c in parent]


@tagged('post_install', '-at_install')
class TestMvameldingXml(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.sudo().l10n_no_eristo_test_orgnr = '310200808'
        cls.mva = cls.env['l10n.no.mvamelding'].create({
            'company_id': cls.company.id,
            'aar': 2026,
            'periode': '2',
        })
        cls.code_values = {
            'BASE_3': 100000.0, 'TAX_3': 25000.0,
            'TAX_1': 5000.0,
            'BASE_52': 2000.0,
        }

    def _generate(self):
        with patch.object(
            type(self.mva), '_tax_report_code_values',
            return_value=self.code_values,
        ):
            self.mva.action_generate_xml()

    def test_generate_sets_state_and_fastsatt(self):
        self._generate()
        self.assertEqual(self.mva.state, 'generated')
        self.assertEqual(self.mva.fastsatt_mva, 20000)
        self.assertTrue(self.mva.mvamelding_xml)
        self.assertTrue(self.mva.konvolutt_xml)

    def test_mvamelding_root_and_namespace(self):
        self._generate()
        root = etree.fromstring(self.mva.mvamelding_xml.encode('utf-8'))
        self.assertEqual(etree.QName(root).localname, 'mvaMeldingDto')
        self.assertEqual(etree.QName(root).namespace, NS_M)

    def test_mvamelding_top_level_order(self):
        self._generate()
        root = etree.fromstring(self.mva.mvamelding_xml.encode('utf-8'))
        self.assertEqual(_localnames(root), [
            'innsending',
            'skattegrunnlagOgBeregnetSkatt',
            'betalingsinformasjon',
            'skattepliktig',
            'meldingskategori',
        ])

    def test_skattegrunnlag_order_and_period(self):
        self._generate()
        root = etree.fromstring(self.mva.mvamelding_xml.encode('utf-8'))
        sg = root.find('{%s}skattegrunnlagOgBeregnetSkatt' % NS_M)
        names = _localnames(sg)
        self.assertEqual(names[0], 'skattleggingsperiode')
        self.assertEqual(names[1], 'fastsattMerverdiavgift')
        self.assertTrue(all(n == 'mvaSpesifikasjonslinje' for n in names[2:]))
        # 2. termin = mars-april
        periode = sg.find('{%s}skattleggingsperiode/{%s}periode' % (NS_M, NS_M))
        toMaaneder = periode.find('{%s}skattleggingsperiodeToMaaneder' % NS_M)
        self.assertEqual(toMaaneder.text, 'mars-april')

    def test_spesifikasjonslinje_field_order(self):
        """Utgående linje: mvaKode, grunnlag, sats, merverdiavgift."""
        self._generate()
        root = etree.fromstring(self.mva.mvamelding_xml.encode('utf-8'))
        sg = root.find('{%s}skattegrunnlagOgBeregnetSkatt' % NS_M)
        kode3 = None
        kode1 = None
        for spes in sg.findall('{%s}mvaSpesifikasjonslinje' % NS_M):
            kode = spes.find('{%s}mvaKode' % NS_M).text
            if kode == '3':
                kode3 = spes
            elif kode == '1':
                kode1 = spes
        self.assertEqual(
            _localnames(kode3),
            ['mvaKode', 'grunnlag', 'sats', 'merverdiavgift'],
        )
        self.assertEqual(kode3.find('{%s}merverdiavgift' % NS_M).text, '25000')
        # Fradragslinje: kun mvaKode + merverdiavgift (negativ).
        self.assertEqual(_localnames(kode1), ['mvaKode', 'merverdiavgift'])
        self.assertEqual(kode1.find('{%s}merverdiavgift' % NS_M).text, '-5000')

    def test_orgnr_uses_test_override(self):
        self._generate()
        root = etree.fromstring(self.mva.mvamelding_xml.encode('utf-8'))
        orgnr = root.find(
            '{%s}skattepliktig/{%s}organisasjonsnummer' % (NS_M, NS_M))
        self.assertEqual(orgnr.text, '310200808')

    def test_konvolutt_structure(self):
        self._generate()
        root = etree.fromstring(self.mva.konvolutt_xml.encode('utf-8'))
        self.assertEqual(etree.QName(root).localname, 'mvaMeldingInnsending')
        self.assertEqual(etree.QName(root).namespace, NS_K)
        self.assertEqual(_localnames(root), [
            'norskIdentifikator',
            'skattleggingsperiode',
            'meldingskategori',
            'innsendingstype',
            'opprettetAv',
        ])
        orgnr = root.find(
            '{%s}norskIdentifikator/{%s}organisasjonsnummer' % (NS_K, NS_K))
        self.assertEqual(orgnr.text, '310200808')
        self.assertEqual(
            root.find('{%s}innsendingstype' % NS_K).text, 'komplett')
