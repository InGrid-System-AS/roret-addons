"""Parser for Skatteetatens <valideringsresultat>.

DESIGNVALG (2026-06-06): Vi kaller IKKE Skatteetatens frittstående
validerings-endepunkt (/api/mva/grensesnittstoette/mva-melding/valider).
Det endepunktet krever et ID-porten-token (acr Level3/4, bruker-innlogging)
— ikke et Maskinporten-token — og test-hosten (idporten-api-sbstest.sits.no)
er IP-/ID-porten-gated. For den helautomatiske ERP-modellen (Maskinporten-
systembruker, slik Fiken/Tripletex gjør) validerer vi i stedet via
Altinn-appens innebygde kontroll: når vi fullfører utfyllingssteget med
PUT process/next, kjører appen samme validering og returnerer HTTP 409 +
<valideringsresultat> ved avvik (se l10n_no_mvamelding_submit.py).

Denne modulen beholder derfor kun parser-helperen som submit-flyten og
UI-et bruker for å vise avvik fra 409-responsen.

Valideringsresultat-struktur (XSD ...:valideringsresultat:v1):
  valideringsresultat
    avvikVedMeldingslevering   ← 'ingen avvik' | 'avvikende ...' |
                                 'mangelfull ...' | 'ugyldig skattemelding'
    avvik*                     ← ett per avvikende element
      stiTilAvvik, [mvaKode], [xmlLinjenummer], [xmlPosisjonPaaLinje]
      avviksinformasjon+ (begrunnelse, avvikstype, avvikKode,
                          regelDefinisjon, [detaljer])
"""
import logging

from lxml import etree

from odoo import _, models

_logger = logging.getLogger(__name__)

_NS_VALIDERING = 'no:skatteetaten:fastsetting:avgift:mva:valideringsresultat:v1'


class L10nNoMvamelding(models.Model):
    _inherit = 'l10n.no.mvamelding'

    def _parse_valideringsresultat(self, xml_text):
        """Parse <valideringsresultat> → (summary, avvik_count, human_text).

        ``summary`` = avvikVedMeldingslevering-verdien. ``human_text`` er en
        lesbar oppsummering av hvert avvik (sti + begrunnelse) for visning.
        """
        self.ensure_one()
        try:
            root = etree.fromstring(xml_text.encode('utf-8'))
        except etree.XMLSyntaxError as e:
            return ('parsefeil', 1, _(
                "Kunne ikke tolke valideringsresultat-XML: %(e)s\n%(raw)s",
                e=str(e)[:200], raw=xml_text[:500],
            ))

        def q(tag):
            return f'{{{_NS_VALIDERING}}}{tag}'

        summary_el = root.find(q('avvikVedMeldingslevering'))
        summary = (summary_el.text or '').strip() if summary_el is not None else ''

        avvik_els = root.findall(q('avvik'))
        if not avvik_els:
            return (summary or 'ingen avvik', 0, _("Ingen avvik."))

        lines = []
        for avvik in avvik_els:
            sti_el = avvik.find(q('stiTilAvvik'))
            sti = (sti_el.text or '').strip() if sti_el is not None else ''
            for info in avvik.findall(q('avviksinformasjon')):
                begr_el = info.find(q('begrunnelse'))
                kode_el = info.find(q('avvikKode'))
                begr = (begr_el.text or '').strip() if begr_el is not None else ''
                kode = (kode_el.text or '').strip() if kode_el is not None else ''
                prefix = f"[{kode}] " if kode else ''
                lines.append(f"• {prefix}{begr}" + (f"\n   ({sti})" if sti else ''))

        return (summary, len(avvik_els), '\n'.join(lines))

    def _vurder_valideringsresultat(self, xml_text):
        """Avgjør verdiktet i et valideringsresultat → (avvist, summary, n, human).

        Brukes av mottaks-flyten (feedback/kvittering-steget) for å skille en
        GODKJENT (fastsatt) melding fra en AVVIST. Skatteetatens toppnivå-
        element ``avvikVedMeldingslevering`` er verdiktet:

          'ingen avvik'            → godkjent (fastsatt)
          'avvikende ...'          → godkjent med merknad (fastsatt)
          'mangelfull ...'         → godkjent med merknad (fastsatt)
          'ugyldig skattemelding'  → AVVIST (ikke fastsatt)
          (= 'ugyldig mva-melding' i eldre ordlyd)

        Returverdi ``avvist``:
          True   meldingen ble avvist
          False  meldingen ble fastsatt (ev. med merknad)
          None   verdiktet kunne IKKE avgjøres (uparsebar XML, namespace-drift,
                 eller tomt verdikt uten avvik) — caller skal da IKKE rapportere
                 suksess, men vente/prøve igjen.

        VIKTIG: vi utleder ALDRI 'godkjent' fra et fravær av signal. Et tomt
        toppnivå-verdikt med avvik behandles som avvist (tryggere enn å
        rapportere mottatt for en avvist compliance-melding), og et dokument vi
        ikke forstår (feil namespace) gir None. Speiler namespace-guarden i
        _parse_betalingsinformasjon.

        Ref: skatteetaten.github.io/mva-meldingen — Tilbakemelding/
        valideringsresultat, alvorlighetsgrad UGYLDIG_SKATTEMELDING.
        """
        self.ensure_one()
        try:
            root = etree.fromstring(xml_text.encode('utf-8'))
        except etree.XMLSyntaxError as e:
            _logger.error(
                "Mvamelding %s: valideringsresultat uparsebar XML (%s) — verdikt "
                "ukjent, rapporterer IKKE mottatt.", self.id, str(e)[:200])
            return (None, None, -1, None)

        actual_ns = etree.QName(root).namespace
        if actual_ns != _NS_VALIDERING:
            # Namespace-/skjema-drift: vi forstår ikke dokumentet. IKKE gjett
            # godkjent — la caller vente. (Uten denne sjekken ville en tom
            # find() sett ut som 'ingen avvik'.)
            _logger.error(
                "Mvamelding %s: valideringsresultat har uventet namespace %s "
                "(forventet %s) — verdikt ukjent, rapporterer IKKE mottatt.",
                self.id, actual_ns, _NS_VALIDERING)
            return (None, None, -1, None)

        summary, avvik_count, human = self._parse_valideringsresultat(xml_text)
        norm = (summary or '').strip().lower()
        if 'ugyldig' in norm:
            # 'ugyldig skattemelding' / 'ugyldig mva-melding' = hard avvisning.
            return (True, summary, avvik_count, human)
        if norm:
            # Gjenkjent, ikke-tomt verdikt uten 'ugyldig' ('ingen avvik' /
            # 'avvikende ...' / 'mangelfull ...') → fastsatt (ev. med merknad).
            return (False, summary, avvik_count, human)
        # Tomt toppnivå-verdikt (element mangler/omdøpt).
        if avvik_count and avvik_count > 0:
            # Det finnes avvik, men verdiktet er uleselig. Tryggere å avvise
            # enn å rapportere mottatt.
            _logger.warning(
                "Mvamelding %s: valideringsresultat mangler toppnivå-verdikt men "
                "har %d avvik — behandler som avvist.", self.id, avvik_count)
            return (True, summary or 'ugyldig mva-melding', avvik_count, human)
        _logger.error(
            "Mvamelding %s: valideringsresultat uten verdikt og uten avvik — "
            "verdikt ukjent, rapporterer IKKE mottatt.", self.id)
        return (None, None, -1, None)
