"""Merknad på spesifikasjonslinje + R021-vakten.

Fikstur er den EKTE produksjonssaken som avdekket hullet: Eristo AS
3. termin 2026, der terminen tilbakefører 1 159,98 kr inngående MVA som
ikke var fradragsberettiget (privat VOEC-faktura, velferd mval § 8-3 d,
overtidsmat). Tilbakeføringen overstiger terminens egne fradrag på
770,40 kr, så kode 1 får motsatt fortegn (+390 i meldingen).

Skatteetaten avviste den innsendingen 2026-08-31 på regel R021 med
alvorlighetsgrad UGYLDIG_SKATTEMELDING — meldingen ble IKKE fastsatt.
Testene her er den regelen, kodet.
"""
from unittest.mock import patch

from lxml import etree

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

NS_M = 'no:skatteetaten:fastsetting:avgift:mva:skattemeldingformerverdiavgift:v1.0'


def _q(tag):
    return '{%s}%s' % (NS_M, tag)


def _localnames(parent):
    return [etree.QName(c).localname for c in parent]


@tagged('post_install', '-at_install')
class TestMvameldingMerknad(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.sudo().l10n_no_eristo_test_orgnr = '310200808'
        cls.mva = cls.env['l10n.no.mvamelding'].create({
            'company_id': cls.company.id,
            'aar': 2026,
            'periode': '3',
        })
        # Eristo 3. termin 2026, verbatim. TAX_1 er NEGATIV fordi
        # tilbakeføringene overstiger fradragene i terminen.
        cls.code_values = {
            'BASE_3': 606300.0, 'TAX_3': 151575.0,
            'TAX_1': -389.58,
            'TAX_13': 1332.76,
        }

    def _generate(self):
        with patch.object(
            type(self.mva), '_tax_report_code_values',
            return_value=self.code_values,
        ):
            return self.mva.action_generate_xml()

    def _merknad(self, kode='1', tekst="Tilbakeføring av uberettiget fradrag."):
        return self.env['l10n.no.mvamelding.merknad'].create({
            'mvamelding_id': self.mva.id,
            'mva_kode': kode,
            'beskrivelse': tekst,
        })

    def _linje(self, kode):
        root = etree.fromstring(self.mva.mvamelding_xml.encode('utf-8'))
        sg = root.find(_q('skattegrunnlagOgBeregnetSkatt'))
        for spes in sg.findall(_q('mvaSpesifikasjonslinje')):
            if spes.find(_q('mvaKode')).text == kode:
                return spes
        return None

    # ---- fikstur: reproduserer den avviste meldingen -------------------

    def test_fikstur_gir_produksjonstallene(self):
        """Kode 1 får motsatt fortegn, og fastsatt blir 150 632."""
        self._generate()
        self.assertEqual(self.mva.fastsatt_mva, 150632)
        self.assertEqual(self._linje('1').find(_q('merverdiavgift')).text, '390')
        self.assertEqual(self._linje('3').find(_q('merverdiavgift')).text, '151575')
        self.assertEqual(self._linje('13').find(_q('merverdiavgift')).text, '-1333')

    # ---- R021-deteksjon ------------------------------------------------

    def test_r021_oppdager_manglende_merknad(self):
        self._generate()
        self.assertEqual(self.mva._r021_koder_uten_merknad(), ['1'])

    def test_r021_stille_naar_merknad_finnes(self):
        self._merknad()
        self._generate()
        self.assertEqual(self.mva._r021_koder_uten_merknad(), [])

    def test_r021_ser_paa_payloaden_ikke_recorden(self):
        """Merknad lagt inn ETTER generering er ikke i XML-en som sendes.

        Sjekken må lese payloaden, ikke merknad-recordene — ellers ville en
        stale XML uten merknad sluppet gjennom og blitt avvist av
        Skatteetaten.
        """
        self._generate()
        self._merknad()
        self.assertEqual(self.mva._r021_koder_uten_merknad(), ['1'])

    def test_stale_xml_gir_regenerer_beskjed(self):
        """Da må feilmeldingen si 'generer på nytt', ikke 'legg inn merknad'
        — brukeren har nettopp lagt den inn."""
        self._generate()
        self._merknad()
        with self.assertRaises(UserError) as ctx:
            self.mva._precheck_innsending()
        melding = str(ctx.exception)
        self.assertIn('Generer XML', melding)
        self.assertNotIn('R021', melding)

    def test_r021_ignorerer_normalt_fortegn(self):
        """Et vanlig fradrag (negativ merverdiavgift) krever ingen merknad."""
        with patch.object(
            type(self.mva), '_tax_report_code_values',
            return_value={'BASE_3': 606300.0, 'TAX_3': 151575.0,
                          'TAX_1': 770.40},
        ):
            self.mva.action_generate_xml()
        self.assertEqual(self._linje('1').find(_q('merverdiavgift')).text, '-770')
        self.assertEqual(self.mva._r021_koder_uten_merknad(), [])

    def test_r021_ignorerer_utgaaende_koder(self):
        """Kode 3 har positiv mva, men er ikke en fradragskode."""
        self._generate()
        self.assertNotIn('3', self.mva._r021_koder_uten_merknad())

    # ---- innsendingsvakten ---------------------------------------------

    def test_innsending_blokkeres_uten_merknad(self):
        self._generate()
        with self.assertRaises(UserError) as ctx:
            self.mva._precheck_innsending()
        melding = str(ctx.exception)
        self.assertIn('R021', melding)
        self.assertIn('merknad', melding.lower())

    def test_innsending_slipper_gjennom_med_merknad(self):
        self._merknad()
        self._generate()
        self.company.sudo().l10n_no_eristo_active_scopes = [
            'skatteetaten:mvamelding']
        # Payloaden skal være ren...
        self.assertEqual(self.mva._r021_koder_uten_merknad(), [])
        # ...og vakten skal ikke stoppe på merknad av NOEN grunn. Vi lister
        # begge merknad-relaterte feiltekstene eksplisitt: en test som bare
        # sjekket 'R021' ville passert selv om stale-grenen slo inn.
        try:
            self.mva._precheck_innsending()
        except UserError as e:
            melding = str(e)
            self.assertNotIn('R021', melding)
            self.assertNotIn('Generer XML', melding)
            self.assertNotIn('merknad', melding.lower())

    # ---- XML-struktur ---------------------------------------------------

    def test_merknad_er_sist_i_linjen(self):
        """XSD-sekvensen har merknad etter merverdiavgift."""
        self._generate()
        self._merknad()
        self._generate()
        self.assertEqual(
            _localnames(self._linje('1')), ['mvaKode', 'merverdiavgift', 'merknad'])

    def test_merknad_har_beskrivelse_med_tekst(self):
        self._generate()
        self._merknad(tekst="Forklaring til Skatteetaten.")
        self._generate()
        merknad = self._linje('1').find(_q('merknad'))
        self.assertEqual(_localnames(merknad), ['beskrivelse'])
        self.assertEqual(
            merknad.find(_q('beskrivelse')).text, "Forklaring til Skatteetaten.")

    def test_merknad_kun_paa_egen_kode(self):
        """En merknad på kode 1 skal ikke lekke over på kode 3."""
        self._generate()
        self._merknad()
        self._generate()
        self.assertIsNone(self._linje('3').find(_q('merknad')))

    def test_blank_merknad_avvises(self):
        """Blank beskrivelse ville gitt tomt element — R021 godtar det ikke."""
        with self.assertRaises(Exception):
            with self.env.cr.savepoint():
                self._merknad(tekst="   ")

    def test_en_merknad_per_kode(self):
        """XSD har maxOccurs=1 på merknad."""
        self._merknad()
        with self.assertRaises(Exception):
            with self.env.cr.savepoint():
                self._merknad(tekst="En til")
