"""Parallellkjøring 2, steg 1: golden-ekvivalens mot produksjon.

Golden-filene er FAKTISK INNSENDTE MVA-meldinger fra Rorets egne selskaper:
2. termin 2026 (generert av Enterprise-utgaven i produksjon) og 3. termin
2026 (generert av DENNE motoren, og fastsatt av Skatteetaten). Én av dem
bærer en merknad — den er fasit for R021-stien, som Skatteetaten avviste
en melding uten. Testene mater den porterte motoren med fasit-tallene og
krever at den bygger BYTE-IDENTISK XML etter normalisering av de bevisst
volatile feltene (regnskapssystemsreferanse-UUID og systemversjon).

Filene ligger i tests/golden/ og følger ikke med i den offentlige
roret-addons-distribusjonen (#319); hvilke selskaper, saksnumre og
instanser de stammer fra står i docs/parallellkjoring/README.md
(«Parallellkjøring 2 — MVA/TT02», punkt 3), ikke her. Testene slår
derfor opp katalogen i stedet for å navngi filene.

Består disse, er hele XML-byggeveien (periode, fortegn, avrunding,
elementrekkefølge, namespaces) bevist ekvivalent med det Skatteetaten
allerede har akseptert — det som gjenstår av parallellkjøring 2 er da
kun tallKILDEN (verifiseres mot produksjonstall) og TT02-innsendingen.
"""
import os
import re
import unittest
from unittest.mock import patch

from lxml import etree

from odoo.modules import get_module_path
from odoo.tests import TransactionCase, tagged

GOLDEN_DIR = os.path.join(
    get_module_path('l10n_no_account_mvamelding'), 'tests', 'golden')
NS = 'no:skatteetaten:fastsetting:avgift:mva:skattemeldingformerverdiavgift:v1.0'
NSMAP = {'m': NS}


def _parse_golden(filename):
    """Les fasit-fila → (orgnr, periode_verdi, aar, code_values, merknader,
    xml_bytes).

    code_values rekonstruerer motor-inputen: for hver spesifikasjonslinje
    med grunnlag settes BASE_<kode>/TAX_<kode>; fradragslinjer (negativ
    merverdiavgift uten grunnlag) settes som positiv TAX_<kode> (builderen
    negerer selv, jf. _collect_mva_lines).
    """
    path = os.path.join(GOLDEN_DIR, filename)
    with open(path, 'rb') as f:
        xml_bytes = f.read()
    tree = etree.fromstring(xml_bytes)
    orgnr = tree.findtext(
        'm:skattepliktig/m:organisasjonsnummer', namespaces=NSMAP)
    aar = int(tree.findtext(
        'm:skattegrunnlagOgBeregnetSkatt/m:skattleggingsperiode/m:aar',
        namespaces=NSMAP))
    periode_verdi = tree.findtext(
        'm:skattegrunnlagOgBeregnetSkatt/m:skattleggingsperiode/m:periode/'
        'm:skattleggingsperiodeToMaaneder', namespaces=NSMAP)
    code_values = {}
    merknader = {}
    for line in tree.findall(
            'm:skattegrunnlagOgBeregnetSkatt/m:mvaSpesifikasjonslinje',
            NSMAP):
        kode = line.findtext('m:mvaKode', namespaces=NSMAP)
        grunnlag = line.findtext('m:grunnlag', namespaces=NSMAP)
        mva = int(line.findtext('m:merverdiavgift', namespaces=NSMAP))
        if grunnlag is not None:
            code_values['BASE_%s' % kode] = float(grunnlag)
            code_values['TAX_%s' % kode] = float(mva)
        else:
            # Fradragslinje: meldingen har negativt beløp, motoren
            # leverer positiv TAX-verdi
            code_values['TAX_%s' % kode] = float(-mva)
        # Merknaden er ikke utledbar fra tallene — den må mates inn som
        # egne records, ellers ville byggeren utelatt elementet og
        # sammenligningen feilet av feil grunn.
        beskrivelse = line.findtext(
            'm:merknad/m:beskrivelse', namespaces=NSMAP)
        if beskrivelse:
            merknader[kode] = beskrivelse
    return orgnr, periode_verdi, aar, code_values, merknader, xml_bytes


def _normalize(xml_bytes):
    """Nøytraliser bevisst volatile felter + normaliser whitespace før
    byte-sammenligning."""
    text = xml_bytes.decode('utf-8')
    text = re.sub(
        r'<regnskapssystemsreferanse>[^<]+</regnskapssystemsreferanse>',
        '<regnskapssystemsreferanse>UUID</regnskapssystemsreferanse>', text)
    text = re.sub(
        r'<systemversjon>[^<]+</systemversjon>',
        '<systemversjon>V</systemversjon>', text)
    # XML-deklarasjonens quote-stil varierer mellom serialiserere
    text = re.sub(r"<\?xml[^>]*\?>", '', text, count=1)
    # Kollaps all whitespace mellom tagger (innrykk er kosmetisk)
    text = re.sub(r'>\s+<', '><', text.strip())
    return text


PERIODE_BY_VERDI = {
    'januar-februar': '1', 'mars-april': '2', 'mai-juni': '3',
    'juli-august': '4', 'september-oktober': '5', 'november-desember': '6',
}


@unittest.skipUnless(
    os.path.isdir(GOLDEN_DIR),
    "tests/golden/ følger ikke med i roret-addons-distribusjonen (#319) — "
    "fasitfilene er Rorets egne innsendte meldinger og kjøres kun i Roret-CI",
)
@tagged('post_install', '-at_install', 'l10n_no_account_mvamelding')
class TestGoldenParallel(TransactionCase):

    def _run_golden(self, filename):
        orgnr, periode_verdi, aar, code_values, merknader, golden = \
            _parse_golden(filename)
        company = self.env['res.company'].create({
            # Filnavnet, ikke orgnr: samme selskap har flere fasitfiler,
            # og res.company.name er unik.
            'name': 'Golden %s' % filename,
            'country_id': self.env.ref('base.no').id,
            'currency_id': self.env.ref('base.NOK').id,
        })
        company.sudo().l10n_no_eristo_test_orgnr = orgnr
        self.env.user.company_ids |= company
        mva = self.env['l10n.no.mvamelding'].create({
            'company_id': company.id,
            'aar': aar,
            'periode': PERIODE_BY_VERDI[periode_verdi],
        })
        for kode, beskrivelse in merknader.items():
            self.env['l10n.no.mvamelding.merknad'].create({
                'mvamelding_id': mva.id,
                'mva_kode': kode,
                'beskrivelse': beskrivelse,
            })
        with patch.object(type(mva), '_tax_report_code_values',
                          return_value=code_values):
            mva.action_generate_xml()
        generated = mva.mvamelding_xml.encode('utf-8') \
            if isinstance(mva.mvamelding_xml, str) else mva.mvamelding_xml
        self.assertEqual(
            _normalize(generated), _normalize(golden),
            "Generert XML avviker fra produksjons-fasiten (%s)" % filename)

    @staticmethod
    def _golden_filenames():
        return sorted(
            f for f in os.listdir(GOLDEN_DIR) if f.endswith('.xml'))

    def test_every_golden_file_is_rebuilt_byte_identical(self):
        """Hver fasitfil i tests/golden/ bygges byte-identisk av motoren.

        Én subTest per fil, så et avvik peker på riktig fasit uten at
        filnavnet (selskap + termin) må stå i kildekoden.
        """
        filenames = self._golden_filenames()
        self.assertGreaterEqual(
            len(filenames), 4, "tests/golden/ mangler fasitfiler")
        for filename in filenames:
            # Savepoint per fil: en feil i én fasit skal ikke ødelegge
            # transaksjonen for de neste.
            with self.subTest(golden=filename), self.env.cr.savepoint():
                self._run_golden(filename)

    def test_golden_set_covers_both_paths(self):
        """Fasitsettet skal dekke både R021-stien og en tilgodemelding.

        R021: en fasitfil med merknad — den ble fastsatt etter at samme
        termin uten merknad var avvist som UGYLDIG_SKATTEMELDING. Tilgode:
        en fasitfil med negativt fastsatt beløp. Uten denne testen kunne
        begge stiene falle ut av fasitsettet uten at noen merket det.
        """
        parsed = [_parse_golden(f) for f in self._golden_filenames()]
        self.assertTrue(
            any(merknader for _, _, _, _, merknader, _ in parsed),
            "ingen fasitfil bærer merknad — R021-stien er udekket")
        self.assertTrue(
            any(b'<fastsattMerverdiavgift>-' in xml for *_, xml in parsed),
            "ingen fasitfil er en tilgodemelding")
