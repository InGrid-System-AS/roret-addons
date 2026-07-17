"""Tester for P0-P3 code-review-fixes.

Fokus på pure-function-oppførsel som kan testes uten å treffe
Skatteetaten/Altinn:

  - P1 #2: _compute_altinn_substatus leser fra dedikerte felter
  - P1 #5: _archive_kvittering_and_post idempotency
  - P1 #9: confirm_html escaper dynamiske verdier (XSS-defense)

Disse er stabile pure-function-snitt som kan kjøre i CI uten
network-mock.
"""
import base64

from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install', 'l10n_no_account_skattemelding')
class TestCodeReviewFixes(TransactionCase):
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

    # ---- P1 #2: dedikert felt tilbakemelding_xml ---------------------------

    def test_substatus_reads_godkjent_from_dedicated_field(self):
        """validertOK i tilbakemelding_xml → endelig=godkjent."""
        self.sm.write({
            'state': 'mottatt',
            'tilbakemelding_xml': (
                '<tilbakemelding>'
                '<resultatAvValidering>validertOK</resultatAvValidering>'
                '</tilbakemelding>'
            ),
        })
        self.assertEqual(self.sm.skatteetaten_endelig_status, 'godkjent')
        self.assertEqual(self.sm.altinn_substatus_label, 'Godkjent')

    def test_substatus_reads_avvist_from_dedicated_field(self):
        """validertMedFeil + aarsak i tilbakemelding_xml → endelig=avvist."""
        self.sm.write({
            'state': 'mottatt',
            'tilbakemelding_xml': (
                '<tilbakemelding>'
                '<resultatAvValidering>validertMedFeil</resultatAvValidering>'
                '<aarsakTilValidertMedFeil>Manglende post</aarsakTilValidertMedFeil>'
                '</tilbakemelding>'
            ),
        })
        self.assertEqual(self.sm.skatteetaten_endelig_status, 'avvist')
        self.assertEqual(self.sm.altinn_substatus_label, 'Avvist')
        self.assertEqual(
            self.sm.altinn_substatus_description, 'Manglende post',
        )

    def test_substatus_reads_from_altinn_instance_json_field(self):
        """status.substatus.label i altinn_instance_json brukes."""
        self.sm.write({
            'state': 'mottatt',
            'altinn_instance_json': (
                '{"status": {"substatus": '
                '{"label": "Godkjent", "description": "OK"}}}'
            ),
        })
        self.assertEqual(self.sm.skatteetaten_endelig_status, 'godkjent')
        self.assertEqual(self.sm.altinn_substatus_label, 'Godkjent')

    def test_substatus_legacy_fallback_from_last_response(self):
        """Gamle records uten dedikerte felter — fall tilbake til
        substring-parse av last_response."""
        self.sm.write({
            'state': 'mottatt',
            'last_response': (
                '=== Altinn instans-respons ===\n{"foo": "bar"}\n\n'
                '=== Skatteetatens tilbakemelding (tilbakemelding.xml) ===\n'
                '<resultatAvValidering>validertOK</resultatAvValidering>'
            ),
        })
        self.assertEqual(self.sm.skatteetaten_endelig_status, 'godkjent')

    def test_substatus_dedicated_field_wins_over_legacy(self):
        """Hvis både tilbakemelding_xml OG legacy last_response finnes,
        skal det dedikerte feltet vinne."""
        self.sm.write({
            'state': 'mottatt',
            'tilbakemelding_xml': (
                '<resultatAvValidering>validertOK</resultatAvValidering>'
            ),
            'last_response': (
                '=== Skatteetatens tilbakemelding ===\n'
                '<resultatAvValidering>validertMedFeil</resultatAvValidering>'
            ),
        })
        self.assertEqual(self.sm.skatteetaten_endelig_status, 'godkjent')

    def test_substatus_pending_before_state_mottatt(self):
        """state=submitted og ingen tilbakemelding → pending."""
        self.sm.write({'state': 'submitted'})
        self.assertEqual(self.sm.skatteetaten_endelig_status, 'pending')

    def test_substatus_namespace_prefix_handled(self):
        """XML med ns-prefiks (sm:resultatAvValidering) skal også parses."""
        self.sm.write({
            'state': 'mottatt',
            'tilbakemelding_xml': (
                '<sm:tilbakemelding xmlns:sm="urn:foo">'
                '<sm:resultatAvValidering>validertOK</sm:resultatAvValidering>'
                '</sm:tilbakemelding>'
            ),
        })
        self.assertEqual(self.sm.skatteetaten_endelig_status, 'godkjent')

    # ---- P1 #5: kvittering-arkivering idempotency --------------------------

    def test_archive_kvittering_sets_archived_at(self):
        """Første kall setter kvittering_archived_at."""
        self.sm.write({
            'state': 'mottatt',
            'altinn_instance_guid': 'test-guid-123',
            'altinn_instance_owner_party_id': '12345',
            'skattemelding_xml': '<skattemelding/>',
            'tilbakemelding_xml':
                '<resultatAvValidering>validertOK</resultatAvValidering>',
        })
        self.assertFalse(self.sm.kvittering_archived_at)
        self.sm._archive_kvittering_and_post()
        self.assertTrue(self.sm.kvittering_archived_at)

    def test_archive_kvittering_idempotent_no_duplicate_attachments(self):
        """Kjør 2 ganger — bare 1 sett vedlegg + 1 chatter-melding."""
        self.sm.write({
            'state': 'mottatt',
            'altinn_instance_guid': 'test-guid-456',
            'altinn_instance_owner_party_id': '12345',
            'skattemelding_xml': '<skattemelding/>',
            'tilbakemelding_xml':
                '<resultatAvValidering>validertOK</resultatAvValidering>',
        })
        # First call
        self.sm._archive_kvittering_and_post()
        first_archived_at = self.sm.kvittering_archived_at
        attachments_after_first = self.env['ir.attachment'].search([
            ('res_model', '=', self.sm._name),
            ('res_id', '=', self.sm.id),
        ])
        messages_after_first = self.env['mail.message'].search([
            ('model', '=', self.sm._name),
            ('res_id', '=', self.sm.id),
            ('subject', 'like', '%godkjent%'),
        ])
        # Second call — should be no-op due to kvittering_archived_at guard
        self.sm._archive_kvittering_and_post()
        attachments_after_second = self.env['ir.attachment'].search([
            ('res_model', '=', self.sm._name),
            ('res_id', '=', self.sm.id),
        ])
        messages_after_second = self.env['mail.message'].search([
            ('model', '=', self.sm._name),
            ('res_id', '=', self.sm.id),
            ('subject', 'like', '%godkjent%'),
        ])
        self.assertEqual(
            len(attachments_after_first),
            len(attachments_after_second),
            "Idempotent re-call skal IKKE skape duplikat-vedlegg",
        )
        self.assertEqual(
            len(messages_after_first),
            len(messages_after_second),
            "Idempotent re-call skal IKKE skape duplikat-chatter",
        )
        self.assertEqual(
            self.sm.kvittering_archived_at, first_archived_at,
            "kvittering_archived_at skal IKKE endres ved no-op re-call",
        )

    def test_archive_kvittering_creates_json_attachment(self):
        """P1 #5 bugfix: altinn-instans-{guid}.json ble ikke matchet av det
        gamle name-filteret. Sjekk at vedlegget nå faktisk opprettes."""
        self.sm.write({
            'state': 'mottatt',
            'altinn_instance_guid': 'json-test-guid',
            'altinn_instance_owner_party_id': '12345',
            'altinn_instance_json': '{"status": "test"}',
            'skattemelding_xml': '<skattemelding/>',
            'tilbakemelding_xml':
                '<resultatAvValidering>validertOK</resultatAvValidering>',
        })
        self.sm._archive_kvittering_and_post()
        json_atts = self.env['ir.attachment'].search([
            ('res_model', '=', self.sm._name),
            ('res_id', '=', self.sm.id),
            ('name', '=', 'altinn-instans-json-test-guid.json'),
        ])
        self.assertEqual(
            len(json_atts), 1,
            "JSON-vedlegg skal opprettes (P1 #5 bugfix)",
        )

    # ---- P1 #9: confirm_html XSS-defense -----------------------------------

    def test_confirm_html_escapes_company_name(self):
        """Selskapsnavn med HTML-tegn må escapes — ikke renderes som HTML."""
        # Lag en wizard for denne sm-en
        evil_company = self.env['res.company'].create({
            'name': '<script>alert(1)</script>Bad AS',
            'country_id': self.env.ref('base.no').id,
            'currency_id': self.env.ref('base.NOK').id,
            'vat': 'NO999999999',
        })
        self.env.user.company_ids |= evil_company
        evil_sm = self.env['l10n.no.skattemelding'].create({
            'company_id': evil_company.id,
            'inntektsaar': 2025,
        })
        wiz = self.env['l10n.no.skattemelding.submit.confirm'].create({
            'skattemelding_id': evil_sm.id,
        })
        html = wiz.confirm_html
        # markupsafe.escape gjør < til &lt; etc.
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;', html)
        # Selve "Bad AS" skal fortsatt være med (escapet)
        self.assertIn('Bad AS', html)

    def test_confirm_html_renders_legitimate_content(self):
        """Sanity: vanlig output renderes korrekt (med to-stegs-tekst)."""
        wiz = self.env['l10n.no.skattemelding.submit.confirm'].create({
            'skattemelding_id': self.sm.id,
        })
        html = wiz.confirm_html
        # To-stegs-tekst skal være med (UX-fix fra wizard)
        self.assertIn('To-stegs innsending', html)
        self.assertIn('altinn.no', html)
        self.assertIn('BankID', html)
        # Selskapsnavn skal være med
        self.assertIn('Eristo Test AS', html)
        # Inntektsår skal være med
        self.assertIn('2025', html)
