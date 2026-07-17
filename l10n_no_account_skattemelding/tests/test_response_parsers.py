"""Tester for Skatteetaten-respons-parsere (offline).

`_extract_parts_from_response` og `_count_avvik_in_response` er pure
functions over JSON-strukturer fra Skatteetaten. De er stabile nok til
å testes med konstruerte fixture-strukturer — vi vet at None-grenen i
_count_avvik_in_response er kritisk (caller behandler None som 'feilet'
og nekter å si 'validert OK').
"""
import base64

from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install', 'l10n_no_account_skattemelding')
class TestSkattemeldingResponseParsers(TransactionCase):
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
        cls.sm = cls.env['l10n.no.skattemelding'].create({
            'company_id': cls.company.id,
            'inntektsaar': 2025,
        })

    # ---- _extract_parts_from_response -------------------------------------

    def _make_skattemelding_payload(self, partsnummer='1234567890', dok_id='SKI:abc'):
        """Bygg en konstruert hentGjeldende-respons med base64-encoded XML."""
        inner_xml = (
            f'<skattemelding>'
            f'<partsnummer>{partsnummer}</partsnummer>'
            f'<inntektsaar>2025</inntektsaar>'
            f'</skattemelding>'
        )
        return {
            'dokumenter': {
                'skattemeldingdokument': {
                    'id': dok_id,
                    'content': base64.b64encode(
                        inner_xml.encode('utf-8'),
                    ).decode('ascii'),
                },
            },
        }

    def test_extract_parts_from_skattemeldingdokument_key(self):
        payload = self._make_skattemelding_payload(
            partsnummer='9876543210', dok_id='SKI:xyz-1',
        )
        partsnummer, dokumentid = self.sm._extract_parts_from_response(payload)
        self.assertEqual(partsnummer, '9876543210')
        self.assertEqual(dokumentid, 'SKI:xyz-1')

    def test_extract_parts_from_alt_skattemelding_key(self):
        """Skatteetaten har historisk variert mellom skattemelding og
        skattemeldingdokument som key — begge skal håndteres."""
        inner_xml = '<skattemelding><partsnummer>5555555555</partsnummer></skattemelding>'
        payload = {
            'dokumenter': {
                'skattemelding': {
                    'dokumentidentifikator': 'SKI:legacy',
                    'content': base64.b64encode(
                        inner_xml.encode('utf-8'),
                    ).decode('ascii'),
                },
            },
        }
        partsnummer, dokumentid = self.sm._extract_parts_from_response(payload)
        self.assertEqual(partsnummer, '5555555555')
        self.assertEqual(dokumentid, 'SKI:legacy')

    def test_extract_parts_top_level_fallback(self):
        payload = {'partsnummer': '7777777777'}
        partsnummer, dokumentid = self.sm._extract_parts_from_response(payload)
        self.assertEqual(partsnummer, '7777777777')
        self.assertIsNone(dokumentid)

    def test_extract_parts_returns_none_for_non_dict(self):
        for bad in (None, 'string-not-dict', 42, []):
            partsnummer, dokumentid = self.sm._extract_parts_from_response(bad)
            self.assertIsNone(partsnummer)
            self.assertIsNone(dokumentid)

    def test_extract_parts_handles_non_xml_content_gracefully(self):
        """Content som dekoder til ikke-XML skal ikke crashe parser."""
        payload = {
            'dokumenter': {
                'skattemeldingdokument': {
                    'id': 'SKI:bad',
                    'content': base64.b64encode(b'not xml at all').decode('ascii'),
                },
            },
        }
        partsnummer, dokumentid = self.sm._extract_parts_from_response(payload)
        self.assertIsNone(partsnummer)
        self.assertEqual(dokumentid, 'SKI:bad')

    def test_extract_parts_from_xml_envelope(self):
        """Skatteetatens faktiske respons (verifisert 2026-05-08) er
        XML-konvolutt med base64-encoded inner skattemelding."""
        inner_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<skattemelding>'
            '<partsnummer>1029384756</partsnummer>'
            '<inntektsaar>2025</inntektsaar>'
            '</skattemelding>'
        )
        content_b64 = base64.b64encode(inner_xml.encode('utf-8')).decode('ascii')
        envelope = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<skattemeldingOgNaeringsspesifikasjonResponse>'
            '<dokumenter>'
            '<skattemeldingdokument>'
            '<type>skattemelding</type>'
            '<encoding>UTF-8</encoding>'
            f'<content>{content_b64}</content>'
            '</skattemeldingdokument>'
            '</dokumenter>'
            '<dokumentidentifikator>SKI-real-format</dokumentidentifikator>'
            '</skattemeldingOgNaeringsspesifikasjonResponse>'
        )
        partsnummer, dokumentid = self.sm._extract_parts_from_response(envelope)
        self.assertEqual(partsnummer, '1029384756')
        self.assertEqual(dokumentid, 'SKI-real-format')

    def test_extract_parts_xml_with_namespace_prefix(self):
        """Hvis Skatteetaten emitter ns-prefiks må regex tåle det."""
        inner_xml = '<sm:skattemelding xmlns:sm="urn:foo"><sm:partsnummer>2222222222</sm:partsnummer></sm:skattemelding>'
        content_b64 = base64.b64encode(inner_xml.encode('utf-8')).decode('ascii')
        envelope = (
            '<ns2:Response xmlns:ns2="urn:bar">'
            '<ns2:dokumentidentifikator>SKI-ns</ns2:dokumentidentifikator>'
            f'<ns2:content>{content_b64}</ns2:content>'
            '</ns2:Response>'
        )
        partsnummer, dokumentid = self.sm._extract_parts_from_response(envelope)
        self.assertEqual(partsnummer, '2222222222')
        self.assertEqual(dokumentid, 'SKI-ns')

    def test_extract_parts_returns_none_when_partsnummer_not_in_xml(self):
        inner_xml = '<skattemelding><inntektsaar>2025</inntektsaar></skattemelding>'
        payload = {
            'dokumenter': {
                'skattemeldingdokument': {
                    'id': 'SKI:no-parts',
                    'content': base64.b64encode(
                        inner_xml.encode('utf-8'),
                    ).decode('ascii'),
                },
            },
        }
        partsnummer, dokumentid = self.sm._extract_parts_from_response(payload)
        self.assertIsNone(partsnummer)
        self.assertEqual(dokumentid, 'SKI:no-parts')

    # ---- _count_avvik_in_response: 0 (eksplisitt OK) ----------------------

    def test_count_avvik_status_ok(self):
        for status in ('OK', 'ok', 'Godkjent', 'godkjent', 'validert', 'VALIDERT'):
            self.assertEqual(
                self.sm._count_avvik_in_response({'status': status}), 0,
                f"status='{status}' skal tolkes som 0 avvik",
            )

    def test_count_avvik_empty_avvik_list(self):
        self.assertEqual(
            self.sm._count_avvik_in_response({'avvik': []}), 0,
        )

    def test_count_avvik_empty_resultat_dict(self):
        self.assertEqual(
            self.sm._count_avvik_in_response({'resultatAvValidering': {}}), 0,
        )

    # ---- _count_avvik_in_response: n>0 ------------------------------------

    def test_count_avvik_top_level_list(self):
        avvik = [{'kode': 'X1'}, {'kode': 'X2'}, {'kode': 'X3'}]
        for key in ('avvik', 'valideringsfeil', 'feil', 'resultatAvValidering'):
            self.assertEqual(
                self.sm._count_avvik_in_response({key: avvik}), 3,
                f"3 avvik under '{key}' skal returnere 3",
            )

    def test_count_avvik_nested_under_wrapper_dict(self):
        payload = {
            'resultatAvValidering': {
                'avvik': [{'kode': 'A'}, {'kode': 'B'}],
            },
        }
        self.assertEqual(self.sm._count_avvik_in_response(payload), 2)

    # ---- _count_avvik_in_response: None (ukjent format) -------------------

    def test_count_avvik_returns_none_for_unknown_structure(self):
        """KRITISK: ukjent format skal returnere None, IKKE 0.

        Caller bruker None til å sette state='feilet' og advare brukeren.
        Hvis vi returnerte 0 her ville vi feilaktig vist 'validert OK'
        og brukeren ville sendt inn ugyldig data.
        """
        self.assertIsNone(self.sm._count_avvik_in_response({}))
        self.assertIsNone(
            self.sm._count_avvik_in_response({'unknownKey': 'whatever'}),
        )
        self.assertIsNone(
            self.sm._count_avvik_in_response({'status': 'noeAnnet'}),
        )

    def test_count_avvik_returns_none_for_non_dict(self):
        for bad in (None, [], 'string', 42):
            self.assertIsNone(self.sm._count_avvik_in_response(bad))

    # ---- XML-respons (faktisk Skatteetaten-format) ------------------------

    def test_count_avvik_xml_validertOK_returns_zero(self):
        """resultatAvValidering=validertOK → 0 avvik (autoritativt)."""
        body = (
            '<skattemeldingOgNaeringsspesifikasjonResponse>'
            '<resultatAvValidering>validertOK</resultatAvValidering>'
            '</skattemeldingOgNaeringsspesifikasjonResponse>'
        )
        self.assertEqual(self.sm._count_avvik_in_response(body), 0)

    def test_count_avvik_xml_validertOK_with_veiledning_returns_zero(self):
        """Pure veiledning (uten faktiskFeil) er rene råd — fortsatt OK."""
        body = (
            '<skattemeldingOgNaeringsspesifikasjonResponse>'
            '<veiledningEtterKontroll><veiledning>'
            '<veiledningstype>N_MANGLER_VERDI_BAK_AKSJENE</veiledningstype>'
            '<betjeningsstrategi>merknadStandard</betjeningsstrategi>'
            '</veiledning></veiledningEtterKontroll>'
            '<resultatAvValidering>validertOK</resultatAvValidering>'
            '</skattemeldingOgNaeringsspesifikasjonResponse>'
        )
        self.assertEqual(self.sm._count_avvik_in_response(body), 0)

    def test_count_avvik_xml_validertMedFeil_with_avvik(self):
        """validertMedFeil + N <avvik>-elementer → N."""
        body = (
            '<skattemeldingOgNaeringsspesifikasjonResponse>'
            '<avvikVedValidering>'
            '<avvik><avvikstype>kodeA</avvikstype></avvik>'
            '<avvik><avvikstype>kodeB</avvikstype></avvik>'
            '</avvikVedValidering>'
            '<resultatAvValidering>validertMedFeil</resultatAvValidering>'
            '</skattemeldingOgNaeringsspesifikasjonResponse>'
        )
        self.assertEqual(self.sm._count_avvik_in_response(body), 2)

    def test_count_avvik_xml_validertMedFeil_with_only_faktiskFeil_veiledning(self):
        """Faktisk respons fra E2E: validertMedFeil med kun veiledning
        som har betjeningsstrategi=faktiskFeil — skal returnere 1, IKKE 0.

        Forrige parser misset dette og sa state=validated feilaktig.
        """
        body = (
            '<skattemeldingOgNaeringsspesifikasjonResponse '
            'xmlns="no:skatteetaten:fastsetting:formueinntekt:'
            'skattemeldingognaeringsspesifikasjon:response:v2">'
            '<veiledningEtterKontroll><veiledning>'
            '<veiledningstype>UP_HAR_NÆRINGSSPESIFIKASJON_MANGLER_SKATTEMELDING'
            '</veiledningstype>'
            '<hjelpetekst>Selskapet mangler skattemelding.</hjelpetekst>'
            '<betjeningsstrategi>faktiskFeil</betjeningsstrategi>'
            '</veiledning></veiledningEtterKontroll>'
            '<resultatAvValidering>validertMedFeil</resultatAvValidering>'
            '</skattemeldingOgNaeringsspesifikasjonResponse>'
        )
        self.assertEqual(self.sm._count_avvik_in_response(body), 1)

    def test_count_avvik_xml_validertMedFeil_combined(self):
        """validertMedFeil med både avvik og faktiskFeil-veiledning — sum."""
        body = (
            '<skattemeldingOgNaeringsspesifikasjonResponse>'
            '<avvikVedValidering>'
            '<avvik><avvikstype>kodeA</avvikstype></avvik>'
            '</avvikVedValidering>'
            '<veiledningEtterKontroll>'
            '<veiledning><betjeningsstrategi>faktiskFeil</betjeningsstrategi></veiledning>'
            '<veiledning><betjeningsstrategi>faktiskFeil</betjeningsstrategi></veiledning>'
            '<veiledning><betjeningsstrategi>merknadStandard</betjeningsstrategi></veiledning>'
            '</veiledningEtterKontroll>'
            '<resultatAvValidering>validertMedFeil</resultatAvValidering>'
            '</skattemeldingOgNaeringsspesifikasjonResponse>'
        )
        # 1 avvik + 2 faktiskFeil = 3 (merknadStandard ignoreres)
        self.assertEqual(self.sm._count_avvik_in_response(body), 3)

    def test_count_avvik_xml_validertMedFeil_no_explicit_elements(self):
        """validertMedFeil uten <avvik>/faktiskFeil — minst 1 (ikke lyve)."""
        body = (
            '<skattemeldingOgNaeringsspesifikasjonResponse>'
            '<resultatAvValidering>validertMedFeil</resultatAvValidering>'
            '</skattemeldingOgNaeringsspesifikasjonResponse>'
        )
        self.assertEqual(self.sm._count_avvik_in_response(body), 1)

    def test_count_avvik_xml_html_error_page_returns_none(self):
        """HTML-feilside (ikke vår root) → None, ikke false positive."""
        body = '<!DOCTYPE html><html><body>Error 500</body></html>'
        self.assertIsNone(self.sm._count_avvik_in_response(body))

    def test_count_avvik_xml_with_namespace_prefix(self):
        """Skatteetaten kan emitte med ns-prefiks."""
        body = (
            '<ns2:skattemeldingOgNaeringsspesifikasjonResponse '
            'xmlns:ns2="urn:foo">'
            '<ns2:resultatAvValidering>validertOK</ns2:resultatAvValidering>'
            '</ns2:skattemeldingOgNaeringsspesifikasjonResponse>'
        )
        self.assertEqual(self.sm._count_avvik_in_response(body), 0)
