"""Parallellkjøring 2, steg 1: golden-ekvivalens mot produksjon.

Golden-filene er de FAKTISK INNSENDTE MVA-meldingene for 2. termin 2026
(InGrid + Eristo), generert av Enterprise-utgaven i produksjon. Testene
mater den porterte motoren med fasit-tallene og krever at den bygger
BYTE-IDENTISK XML etter normalisering av de bevisst volatile feltene
(regnskapssystemsreferanse-UUID og systemversjon).

Består disse, er hele XML-byggeveien (periode, fortegn, avrunding,
elementrekkefølge, namespaces) bevist ekvivalent med det Skatteetaten
allerede har akseptert — det som gjenstår av parallellkjøring 2 er da
kun tallKILDEN (verifiseres mot produksjonstall) og TT02-innsendingen.
"""
import os
import re
from unittest.mock import patch

from lxml import etree

from odoo.modules import get_module_path
from odoo.tests import TransactionCase, tagged

GOLDEN_DIR = os.path.join(
    get_module_path('l10n_no_account_mvamelding'), 'tests', 'golden')
NS = 'no:skatteetaten:fastsetting:avgift:mva:skattemeldingformerverdiavgift:v1.0'
NSMAP = {'m': NS}


def _parse_golden(filename):
    """Les fasit-fila → (orgnr, periode_verdi, aar, code_values, xml_bytes).

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
    return orgnr, periode_verdi, aar, code_values, xml_bytes


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


@tagged('post_install', '-at_install', 'l10n_no_account_mvamelding')
class TestGoldenParallel(TransactionCase):

    def _run_golden(self, filename):
        orgnr, periode_verdi, aar, code_values, golden = \
            _parse_golden(filename)
        company = self.env['res.company'].create({
            'name': 'Golden %s' % orgnr,
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
        with patch.object(type(mva), '_tax_report_code_values',
                          return_value=code_values):
            mva.action_generate_xml()
        generated = mva.mvamelding_xml.encode('utf-8') \
            if isinstance(mva.mvamelding_xml, str) else mva.mvamelding_xml
        self.assertEqual(
            _normalize(generated), _normalize(golden),
            "Generert XML avviker fra produksjons-fasiten (%s)" % filename)

    def test_golden_ingrid_2026_2termin(self):
        self._run_golden('mva-melding-InGrid-2026-2termin.xml')

    def test_golden_eristo_2026_2termin(self):
        self._run_golden('mva-melding-Eristo-2026-2termin.xml')
