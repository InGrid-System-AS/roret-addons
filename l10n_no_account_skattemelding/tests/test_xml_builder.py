"""Unit-tester for skattemelding XML-builder.

Dekker stabile pure-function-deler:
  - _schema_version: rett versjon per inntektsår, raise for ukjent år
  - _resolve_partsnummer: raise når mangler / non-int, returner når OK
  - build_skattemelding_xml: namespaces + påkrevde felt
  - build_naeringsspesifikasjon_xml: wrapper-struktur
  - build_konvolutt_xml: base64-enkoding + dokumentreferanse-betingelse
  - XML-escaping av spesialtegn i selskapsnavn (security regression)

HTTP-actions er IKKE testet her — venter til vi har sample-respons fra
Skatteetaten validertest. Da legger vi snapshot-tester for parserne mot
ekte response-data.
"""
import base64

from lxml import etree

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


_NS_KONVOLUTT = (
    'no:skatteetaten:fastsetting:formueinntekt:'
    'skattemeldingognaeringsspesifikasjon:request:v2'
)
_NS_SME_BASE = (
    'urn:no:skatteetaten:fastsetting:formueinntekt:skattemelding:upersonlig:ekstern'
)
_NS_NSP_BASE = (
    'urn:no:skatteetaten:fastsetting:formueinntekt:naeringsspesifikasjon:ekstern'
)


@tagged('post_install', '-at_install', 'l10n_no_account_skattemelding')
class TestSkattemeldingXmlBuilder(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env['res.company'].create({
            'name': 'Eristo Test AS',
            'country_id': cls.env.ref('base.no').id,
            'currency_id': cls.env.ref('base.NOK').id,
            'vat': 'NO917191239MVA',
        })
        cls.env.user.company_ids |= cls.company
        cls.env = cls.env(context=dict(
            cls.env.context, allowed_company_ids=cls.company.ids,
        ))
        cls.xml_svc = cls.env['l10n.no.skattemelding.xml.service']

    def _make_skattemelding(self, year=2025, partsnummer='1234567890', **extra):
        vals = {
            'company_id': self.company.id,
            'inntektsaar': year,
            'partsnummer': partsnummer,
        }
        vals.update(extra)
        return self.env['l10n.no.skattemelding'].create(vals)

    # ---- _schema_version ---------------------------------------------------

    def test_schema_version_2024(self):
        self.assertEqual(self.xml_svc._schema_version(2024, 'skattemelding'), 'v4')
        self.assertEqual(
            self.xml_svc._schema_version(2024, 'naeringsspesifikasjon'), 'v5',
        )

    def test_schema_version_2025_current(self):
        self.assertEqual(self.xml_svc._schema_version(2025, 'skattemelding'), 'v5')
        self.assertEqual(
            self.xml_svc._schema_version(2025, 'naeringsspesifikasjon'), 'v6',
        )

    def test_schema_version_2026(self):
        self.assertEqual(self.xml_svc._schema_version(2026, 'skattemelding'), 'v6')
        self.assertEqual(
            self.xml_svc._schema_version(2026, 'naeringsspesifikasjon'), 'v7',
        )

    def test_schema_version_unknown_year_raises(self):
        with self.assertRaises(UserError) as ctx:
            self.xml_svc._schema_version(2030, 'skattemelding')
        self.assertIn('2030', str(ctx.exception))

    # ---- _resolve_partsnummer ---------------------------------------------

    def test_resolve_partsnummer_missing_raises(self):
        sm = self._make_skattemelding(partsnummer=False)
        with self.assertRaises(UserError) as ctx:
            self.xml_svc._resolve_partsnummer(sm)
        self.assertIn('artsnummer', str(ctx.exception))

    def test_resolve_partsnummer_non_integer_raises(self):
        sm = self._make_skattemelding(partsnummer='not-a-number')
        with self.assertRaises(UserError) as ctx:
            self.xml_svc._resolve_partsnummer(sm)
        self.assertIn('xsd:long', str(ctx.exception))

    def test_resolve_partsnummer_valid(self):
        sm = self._make_skattemelding(partsnummer='1234567890')
        self.assertEqual(self.xml_svc._resolve_partsnummer(sm), '1234567890')

    # ---- build_skattemelding_xml ------------------------------------------

    def test_skattemelding_xml_has_required_fields(self):
        sm = self._make_skattemelding(year=2025, partsnummer='1234567890')
        xml = self.xml_svc.build_skattemelding_xml(sm)
        root = etree.fromstring(xml.encode('utf-8'))
        ns = f'{_NS_SME_BASE}:v5'
        self.assertEqual(root.tag, f'{{{ns}}}skattemelding')
        self.assertEqual(root.find(f'{{{ns}}}partsnummer').text, '1234567890')
        self.assertEqual(root.find(f'{{{ns}}}inntektsaar').text, '2025')

    def test_skattemelding_xml_uses_year_specific_namespace(self):
        sm_2024 = self._make_skattemelding(year=2024)
        sm_2026 = self._make_skattemelding(year=2026)
        xml_2024 = self.xml_svc.build_skattemelding_xml(sm_2024)
        xml_2026 = self.xml_svc.build_skattemelding_xml(sm_2026)
        self.assertIn(f'{_NS_SME_BASE}:v4', xml_2024)
        self.assertIn(f'{_NS_SME_BASE}:v6', xml_2026)

    def test_skattemelding_xml_propagates_missing_partsnummer(self):
        sm = self._make_skattemelding(partsnummer=False)
        with self.assertRaises(UserError):
            self.xml_svc.build_skattemelding_xml(sm)

    # ---- build_naeringsspesifikasjon_xml ----------------------------------

    def test_naeringsspesifikasjon_has_required_wrapper_structure(self):
        sm = self._make_skattemelding(year=2025)
        xml = self.xml_svc.build_naeringsspesifikasjon_xml(sm)
        root = etree.fromstring(xml.encode('utf-8'))
        ns = f'{_NS_NSP_BASE}:v6'
        self.assertEqual(root.tag, f'{{{ns}}}naeringsspesifikasjon')

        self.assertEqual(root.find(f'{{{ns}}}partsreferanse').text, '1234567890')
        self.assertEqual(root.find(f'{{{ns}}}inntektsaar').text, '2025')

        virksomhet = root.find(f'{{{ns}}}virksomhet')
        self.assertIsNotNone(virksomhet)

        # regnskapspliktstype/regnskapspliktstype (wrapper-pattern fra XSD)
        rpt_wrap = virksomhet.find(f'{{{ns}}}regnskapspliktstype')
        self.assertIsNotNone(rpt_wrap)
        rpt_inner = rpt_wrap.find(f'{{{ns}}}regnskapspliktstype')
        self.assertEqual(rpt_inner.text, 'fullRegnskapsplikt')

        # virksomhetstype/virksomhetstype
        vt_wrap = virksomhet.find(f'{{{ns}}}virksomhetstype')
        vt_inner = vt_wrap.find(f'{{{ns}}}virksomhetstype')
        self.assertEqual(vt_inner.text, 'oevrigSelskap')

        # regnskapsperiode/start/dato + slutt/dato
        periode = virksomhet.find(f'{{{ns}}}regnskapsperiode')
        start_dato = periode.find(f'{{{ns}}}start/{{{ns}}}dato').text
        slutt_dato = periode.find(f'{{{ns}}}slutt/{{{ns}}}dato').text
        self.assertEqual(start_dato, '2025-01-01')
        self.assertEqual(slutt_dato, '2025-12-31')

        # skalBekreftesAvRevisor er påkrevd
        self.assertEqual(
            root.find(f'{{{ns}}}skalBekreftesAvRevisor').text, 'false',
        )

    # ---- XML-escaping (security regression) -------------------------------

    def test_xml_escaping_handles_special_characters(self):
        """Selskapsnavn med &, <, >, æøå må ikke kunne breake ut av XML.

        F-string-konkatenering (gammel implementasjon) var sårbar for
        XML-injection. lxml.etree skal escape automatisk.
        """
        evil_company = self.env['res.company'].create({
            'name': 'Bad & "<Company>" æøå AS',
            'country_id': self.env.ref('base.no').id,
            'currency_id': self.env.ref('base.NOK').id,
            'vat': 'NO936903479MVA',
        })
        sm = self.env['l10n.no.skattemelding'].create({
            'company_id': evil_company.id,
            'inntektsaar': 2025,
            'partsnummer': '1234567890',
        })

        xml = self.xml_svc.build_naeringsspesifikasjon_xml(sm)
        # Must parse without errors — proves no broken structure.
        root = etree.fromstring(xml.encode('utf-8'))
        self.assertIsNotNone(root)

    # ---- build_konvolutt_xml ----------------------------------------------

    def test_konvolutt_xml_base64_encodes_inner_documents(self):
        sm = self._make_skattemelding(year=2025)
        sm.skattemelding_xml = self.xml_svc.build_skattemelding_xml(sm)
        sm.naeringsspesifikasjon_xml = (
            self.xml_svc.build_naeringsspesifikasjon_xml(sm)
        )
        konvolutt = self.xml_svc.build_konvolutt_xml(sm)

        root = etree.fromstring(konvolutt.encode('utf-8'))
        ns = _NS_KONVOLUTT
        self.assertEqual(
            root.tag, f'{{{ns}}}skattemeldingOgNaeringsspesifikasjonRequest',
        )

        dokumenter = root.find(f'{{{ns}}}dokumenter')
        dokumenter_list = dokumenter.findall(f'{{{ns}}}dokument')
        self.assertEqual(len(dokumenter_list), 2)

        types = [
            d.find(f'{{{ns}}}type').text for d in dokumenter_list
        ]
        self.assertEqual(
            types, ['skattemeldingUpersonlig', 'naeringsspesifikasjon'],
        )

        # Verify the base64 round-trips back to the inner XML.
        sme_b64 = dokumenter_list[0].find(f'{{{ns}}}content').text
        decoded = base64.b64decode(sme_b64).decode('utf-8')
        self.assertEqual(decoded, sm.skattemelding_xml)

    def test_konvolutt_xml_omits_dokumentreferanse_when_no_prior(self):
        sm = self._make_skattemelding(year=2025)
        sm.skattemelding_xml = self.xml_svc.build_skattemelding_xml(sm)
        sm.naeringsspesifikasjon_xml = (
            self.xml_svc.build_naeringsspesifikasjon_xml(sm)
        )
        konvolutt = self.xml_svc.build_konvolutt_xml(sm)

        root = etree.fromstring(konvolutt.encode('utf-8'))
        ref = root.find(
            f'{{{_NS_KONVOLUTT}}}dokumentreferanseTilGjeldendeDokument',
        )
        self.assertIsNone(
            ref,
            "dokumentreferanse skal IKKE emittes når ingen prior_id "
            "er kjent — validertest-endpoint godtar fravær, /valider gjør ikke.",
        )

    def test_konvolutt_xml_emits_dokumentreferanse_when_prior_id_set(self):
        sm = self._make_skattemelding(
            year=2025, dokumentidentifikator='SKI:abc-123',
        )
        sm.skattemelding_xml = self.xml_svc.build_skattemelding_xml(sm)
        sm.naeringsspesifikasjon_xml = (
            self.xml_svc.build_naeringsspesifikasjon_xml(sm)
        )
        konvolutt = self.xml_svc.build_konvolutt_xml(sm)

        root = etree.fromstring(konvolutt.encode('utf-8'))
        ns = _NS_KONVOLUTT
        ref = root.find(f'{{{ns}}}dokumentreferanseTilGjeldendeDokument')
        self.assertIsNotNone(ref)
        self.assertEqual(
            ref.find(f'{{{ns}}}dokumenttype').text, 'skattemeldingUpersonlig',
        )
        self.assertEqual(
            ref.find(f'{{{ns}}}dokumentidentifikator').text, 'SKI:abc-123',
        )

    def test_konvolutt_xml_innsendingsinformasjon(self):
        sm = self._make_skattemelding(year=2025)
        sm.skattemelding_xml = self.xml_svc.build_skattemelding_xml(sm)
        sm.naeringsspesifikasjon_xml = (
            self.xml_svc.build_naeringsspesifikasjon_xml(sm)
        )
        konvolutt = self.xml_svc.build_konvolutt_xml(sm)

        root = etree.fromstring(konvolutt.encode('utf-8'))
        ns = _NS_KONVOLUTT
        innsending = root.find(f'{{{ns}}}innsendingsinformasjon')
        # innsendingstype='komplett' er kritisk for at Skatteetatens
        # visnings-tjeneste skal akseptere innsending for signering.
        # Endret fra 'ikkeKomplett' i 19.0.7.9.0 etter SMEVB-005-feil-
        # diagnose mot Gamify-innsendingen — Skatteetatens 2024-eksempel
        # + asynk-API-doc viser entydig at innsending krever 'komplett'.
        self.assertEqual(
            innsending.find(f'{{{ns}}}innsendingstype').text, 'komplett',
        )
        self.assertEqual(
            innsending.find(f'{{{ns}}}opprettetAv').text,
            'Eristo Skattemelding for Odoo 19',
        )
        # innsendingsformaal er påkrevd fra 2025 (E2E mot Skatteetaten
        # validertest viste avvik 'innkommendeForespoerselManglerInnsendingsformaal'
        # uten den).
        self.assertEqual(
            innsending.find(f'{{{ns}}}innsendingsformaal').text, 'egenfastsetting',
        )
        # tin/orgnr hører IKKE under innsendingsinformasjon per spec-eksempel.
        self.assertIsNone(innsending.find(f'{{{ns}}}tin'))

    def test_konvolutt_xml_requires_inner_xmls_built_first(self):
        sm = self._make_skattemelding(year=2025)
        with self.assertRaises(UserError):
            self.xml_svc.build_konvolutt_xml(sm)

    # ---- _net_amount_for_kodetype: fortegn-håndtering -------------------

    def test_net_amount_takes_abs_for_negativ_fortegn(self):
        """Kodetyper m. fortegn='negativ' (eks. 2080 Negativ egenkapital /
        udekket tap) skal sendes som absoluttverdi — fortegnet er
        implisitt i koden selv. Direkte sending av negativ verdi
        triggrer N_NEGATIV_KONTO_<kode>-avvik fra Skatteetaten.

        Bug funnet 2026-05-11 i E2E mot Eristo: konto 2080 ble rapportert
        med minus-tegn → Skatteetaten avviste m. N_NEGATIV_KONTO_2080.
        """
        # Bruk inntektsaar=9999 for å unngå kollisjon med seed-data
        # (kodetype 2080 for 2025 finnes allerede i skattemelding_kodetype_data.xml).
        kt_2080 = self.env['l10n.no.skattemelding.kodetype'].create({
            'code': '2080', 'name': 'Negativ egenkapital',
            'inntektsaar': 9999,
            'underkodeliste': 'egenkapital',
            'fortegn': 'negativ',
        })
        # Udekket tap har debitert balanse — credit < debit i kredit-konvensjon
        # → net = credit - debit = -50000 før abs()
        net = self.xml_svc._net_amount_for_kodetype(
            kt_2080, debit=50000, credit=0,
        )
        self.assertEqual(
            net, 50000.0,
            "fortegn='negativ' skal returnere absoluttverdi, ikke -50000",
        )

    def test_net_amount_preserves_sign_for_positiv_fortegn(self):
        """Kontroll: kodetyper m. fortegn='positiv' følger fortsatt
        debet/kredit-konvensjon — abs()-fix skal kun gjelde negativ-flagget."""
        # Bruk inntektsaar=9999 for å unngå kollisjon med seed-data.
        kt_2050 = self.env['l10n.no.skattemelding.kodetype'].create({
            'code': '2050', 'name': 'Annen egenkapital',
            'inntektsaar': 9999,
            'underkodeliste': 'egenkapital',
            'fortegn': 'positiv',
        })
        # Egenkapital m. credit-saldo: credit > debit → positiv net
        net = self.xml_svc._net_amount_for_kodetype(
            kt_2050, debit=0, credit=120000,
        )
        self.assertEqual(net, 120000.0)
