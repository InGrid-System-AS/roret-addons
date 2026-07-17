"""XML-bygging for mva-melding (mvaMeldingDto) + konvolutt (mvaMeldingInnsending).

Bruker lxml.etree for korrekt escaping av norske tegn/spesialtegn i
felt-verdier (samme valg som skattemelding-modulen — f-string-konkatenering
er sårbart for XML-injection).

Element-rekkefølge er XSD-bundet (xsd:sequence) og MÅ bevares. Verifisert mot
Skatteetaten/mva-meldingen@master XSD-er 2026-06-05:

  mvaMeldingDto (ns skattemeldingformerverdiavgift:v1.0):
    innsending(regnskapssystemsreferanse, regnskapssystem(systemnavn,
      systemversjon))
    skattegrunnlagOgBeregnetSkatt(skattleggingsperiode(periode, aar),
      fastsattMerverdiavgift, mvaSpesifikasjonslinje*)
    betalingsinformasjon            ← tomt element ved innsending
    skattepliktig(organisasjonsnummer)
    meldingskategori

  mvaSpesifikasjonslinje: mvaKode, [spesifikasjon], [mvaKodeRegnskapsystem],
    [grunnlag], [sats], merverdiavgift, [merknad]

  mvaMeldingInnsending (ns mvameldinginnsending:v1.0):
    norskIdentifikator(organisasjonsnummer)
    skattleggingsperiode(periode, aar)
    meldingskategori
    [innsendingstype]               ← 'komplett'
    opprettetAv                     ← påkrevd fri tekst
    [vedlegg*]                      ← utelatt i v1 (verifiseres mot TT02)
"""
import logging

from lxml import etree

from odoo import models

_logger = logging.getLogger(__name__)

_KILDESYSTEM = 'Eristo MVA-melding for Odoo 19'
_KILDESYSTEM_VERSJON = '19.0.1.0.0'

_NS_MVAMELDING = (
    'no:skatteetaten:fastsetting:avgift:mva:skattemeldingformerverdiavgift:v1.0'
)
_NS_KONVOLUTT = (
    'no:skatteetaten:fastsetting:avgift:mva:mvameldinginnsending:v1.0'
)


def _root(tag, ns):
    return etree.Element('{%s}%s' % (ns, tag), nsmap={None: ns})


def _sub(parent, tag, ns, text=None):
    el = etree.SubElement(parent, '{%s}%s' % (ns, tag))
    if text is not None:
        el.text = str(text)
    return el


def _serialize(root):
    return etree.tostring(
        root, xml_declaration=True, encoding='UTF-8', pretty_print=True,
    ).decode('utf-8')


class L10nNoMvamelding(models.Model):
    _inherit = 'l10n.no.mvamelding'

    def _build_mvamelding_xml(self, lines, fastsatt):
        """Bygg mvaMeldingDto-XML fra innsamlede linjer."""
        self.ensure_one()
        eristo = self.env['l10n.no.eristo.service']
        orgnr = eristo._orgnr(self.company_id)
        element, value, _df, _dt = self._periode_spec()
        ns = _NS_MVAMELDING

        root = _root('mvaMeldingDto', ns)

        innsending = _sub(root, 'innsending', ns)
        _sub(innsending, 'regnskapssystemsreferanse', ns,
             self.regnskapssystemsreferanse)
        regnskapssystem = _sub(innsending, 'regnskapssystem', ns)
        _sub(regnskapssystem, 'systemnavn', ns, _KILDESYSTEM)
        _sub(regnskapssystem, 'systemversjon', ns, _KILDESYSTEM_VERSJON)

        grunnlag_skatt = _sub(root, 'skattegrunnlagOgBeregnetSkatt', ns)
        periode_wrap = _sub(grunnlag_skatt, 'skattleggingsperiode', ns)
        periode_el = _sub(periode_wrap, 'periode', ns)
        _sub(periode_el, element, ns, value)
        _sub(periode_wrap, 'aar', ns, self.aar)
        _sub(grunnlag_skatt, 'fastsattMerverdiavgift', ns, fastsatt)

        for line in lines:
            spes = _sub(grunnlag_skatt, 'mvaSpesifikasjonslinje', ns)
            _sub(spes, 'mvaKode', ns, line['mva_kode'])
            if line.get('grunnlag') is not None:
                _sub(spes, 'grunnlag', ns, line['grunnlag'])
            if line.get('sats') is not None:
                _sub(spes, 'sats', ns, line['sats'])
            _sub(spes, 'merverdiavgift', ns, line['merverdiavgift'])

        # Tomt betalingsinformasjon-element (påkrevd i sekvensen ved innsending).
        _sub(root, 'betalingsinformasjon', ns)

        skattepliktig = _sub(root, 'skattepliktig', ns)
        _sub(skattepliktig, 'organisasjonsnummer', ns, orgnr)

        _sub(root, 'meldingskategori', ns, self.meldingskategori)

        return _serialize(root)

    def _build_konvolutt_xml(self):
        """Bygg mvaMeldingInnsending-konvolutt-XML."""
        self.ensure_one()
        eristo = self.env['l10n.no.eristo.service']
        orgnr = eristo._orgnr(self.company_id)
        element, value, _df, _dt = self._periode_spec()
        ns = _NS_KONVOLUTT

        root = _root('mvaMeldingInnsending', ns)

        ident = _sub(root, 'norskIdentifikator', ns)
        _sub(ident, 'organisasjonsnummer', ns, orgnr)

        periode_wrap = _sub(root, 'skattleggingsperiode', ns)
        periode_el = _sub(periode_wrap, 'periode', ns)
        _sub(periode_el, element, ns, value)
        _sub(periode_wrap, 'aar', ns, self.aar)

        _sub(root, 'meldingskategori', ns, self.meldingskategori)
        _sub(root, 'innsendingstype', ns, 'komplett')
        _sub(root, 'opprettetAv', ns, _KILDESYSTEM)

        return _serialize(root)
