"""Tester for Altinn 3-innsendings-flyt (offline med mocking).

Vi mocker urllib.request.urlopen for å verifisere:
  - State-guards (kan ikke sende fra draft, kan kun hente kvittering hvis
    submitted/mottatt)
  - Idempotency: gjenbruk av altinn_instance_guid hvis allerede satt
  - Token-exchange-parsing (Altinn returnerer JWT som plain text)
  - Process-loop terminerer på currentTask=None
"""
import io
import json
from unittest.mock import patch, MagicMock

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


def _mock_http_response(body, status=200):
    """Bygg en requests-response-mock som leverer body via .text/.status_code.

    Brukes etter urllib→requests-migreringen (P2 #6). Tidligere mocket vi
    urllib.request.urlopen som returnerte en HTTPResponse — nå mocker vi
    requests.get/post/put som returnerer requests.Response.
    """
    mock_resp = MagicMock()
    mock_resp.text = body if isinstance(body, str) else body.decode('utf-8')
    mock_resp.content = body.encode('utf-8') if isinstance(body, str) else body
    mock_resp.status_code = status
    mock_resp.json.return_value = None  # caller bruker .text for non-JSON
    return mock_resp


@tagged('post_install', '-at_install', 'l10n_no_account_skattemelding')
class TestSkattemeldingSubmit(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env['res.company'].create({
            'name': 'Eristo Test AS',
            'country_id': cls.env.ref('base.no').id,
            'currency_id': cls.env.ref('base.NOK').id,
            'vat': 'NO917191239MVA',
            'l10n_no_eristo_environment': 'test',
            'l10n_no_eristo_token_url': 'https://fake.example/token',
            'l10n_no_eristo_api_key': 'fake-key',
        })
        cls.env.user.company_ids |= cls.company
        cls.env = cls.env(context=dict(
            cls.env.context, allowed_company_ids=cls.company.ids,
        ))
        cls.sm = cls.env['l10n.no.skattemelding'].create({
            'company_id': cls.company.id,
            'inntektsaar': 2025,
            'partsnummer': '9988776655',
            'konvolutt_xml': '<konvolutt/>',
            'state': 'validated',
        })
        # Med l10n_no_styre_skattemelding installert (post_install i full
        # suite) blokkeres submit av avleggelse-sjekken før basevalideringene
        # vi tester her — overstyr den så testene når riktig guard.
        if 'styre_avleggelse_override' in cls.sm._fields:
            cls.sm.styre_avleggelse_override = True

    # ---- State-guards ------------------------------------------------------

    def test_submit_rejects_draft(self):
        self.sm.state = 'draft'
        with self.assertRaises(UserError) as ctx:
            self.sm.action_l10n_no_skattemelding_submit()
        self.assertIn('validert', str(ctx.exception))

    def test_submit_rejects_built(self):
        self.sm.state = 'built'
        with self.assertRaises(UserError):
            self.sm.action_l10n_no_skattemelding_submit()

    def test_submit_requires_konvolutt(self):
        self.sm.write({'state': 'validated', 'konvolutt_xml': False})
        with self.assertRaises(UserError) as ctx:
            self.sm.action_l10n_no_skattemelding_submit()
        self.assertIn('Konvolutt', str(ctx.exception))

    def test_submit_requires_partsnummer(self):
        self.sm.write({'state': 'validated', 'partsnummer': False})
        with self.assertRaises(UserError) as ctx:
            self.sm.action_l10n_no_skattemelding_submit()
        self.assertIn('Partsnummer', str(ctx.exception))

    def test_fetch_receipt_rejects_pre_submit(self):
        # validated er ikke gyldig for kvittering — må være submitted/mottatt
        self.sm.state = 'validated'
        with self.assertRaises(UserError) as ctx:
            self.sm.action_l10n_no_skattemelding_fetch_receipt()
        self.assertIn('innsendte', str(ctx.exception))

    # ---- Token-exchange ----------------------------------------------------

    def test_exchange_strips_quoted_jwt(self):
        """Altinn har historisk returnert quotet JWT — defensiv parsing.

        Etter P2 #6 mocker vi requests.get i stedet for urllib.request.urlopen.
        Modulen importerer requests på toppen av submit.py, så patch-pathen
        må peke på samme namespace (l10n_no_skattemelding_submit.requests).
        """
        with patch('odoo.addons.l10n_no_account_skattemelding'
                   '.models.l10n_no_skattemelding_submit.requests.get') as mock_get:
            mock_get.return_value = _mock_http_response('"a.b.c"')
            token = self.sm._exchange_to_altinn_token('maskinporten-token')
            self.assertEqual(token, 'a.b.c')

    def test_exchange_returns_unquoted_jwt(self):
        with patch('odoo.addons.l10n_no_account_skattemelding'
                   '.models.l10n_no_skattemelding_submit.requests.get') as mock_get:
            mock_get.return_value = _mock_http_response('header.payload.sig')
            token = self.sm._exchange_to_altinn_token('mp-token')
            self.assertEqual(token, 'header.payload.sig')

    def test_exchange_rejects_non_jwt_response(self):
        """Hvis Altinn returnerer noe som ikke ser ut som JWT, må vi feile
        høyt — ikke sende en useless string videre i flyten."""
        with patch('odoo.addons.l10n_no_account_skattemelding'
                   '.models.l10n_no_skattemelding_submit.requests.get') as mock_get:
            mock_get.return_value = _mock_http_response('not-a-jwt')
            with self.assertRaises(UserError) as ctx:
                self.sm._exchange_to_altinn_token('mp-token')
            self.assertIn('uventet respons', str(ctx.exception))
