"""Tester for innsendingsmodus: ID-porten (person) vs systembruker.

Bakgrunn (2026-07-12, docs/mva-systembruker-funn.md): Skatteetatens
behandlingsløp henter ikke innsendinger gjort av Altinn 3 systembruker —
verifisert ved A/B-eksperiment i TT02 der identisk melding sendt med
ID-porten-person-token ble behandlet på under 90 sekunder, mens
systembruker-instansen aldri ble hentet. Innsending må derfor skje med
person-token (ID-porten) inntil Skatteetaten bekrefter systembruker-
støtte; kvittering hentes fortsatt automatisk med systembruker-token
(lesetilgang på person-opprettede instanser er verifisert).

Testene mocker Altinn-/token-helperne — ingen nettverk.
"""
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from odoo.addons.l10n_no_account_mvamelding.models.l10n_no_mvamelding_submit \
    import AltinnNotReadyError

_ACT_URL = {'type': 'ir.actions.act_url', 'url': 'https://idporten.example/x',
            'target': 'self'}


@tagged('post_install', '-at_install')
class TestSubmitModes(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.sudo().write({
            'l10n_no_eristo_test_orgnr': '313531074',
            # aktivert-flagget er compute fra aktive scopes — sett kilden
            'l10n_no_eristo_active_scopes': ['skatteetaten:mvamelding'],
        })
        cls.Melding = cls.env['l10n.no.mvamelding']

    def _melding(self, state='generated', **vals):
        return self.Melding.create({
            'company_id': self.company.id,
            'aar': 2026,
            'periode': '2',
            'state': state,
            'mvamelding_xml': '<melding/>',
            'konvolutt_xml': '<konvolutt/>',
            **vals,
        })

    # ---------- modusvalg ----------

    def test_default_modus_er_idporten(self):
        """Systembruker-innsending behandles ikke av Skatteetaten i dag —
        idporten MÅ være default til de bekrefter støtte."""
        self.assertEqual(self.company.l10n_no_mva_submit_mode, 'idporten')

    def test_idporten_modus_starter_authorize_flow(self):
        melding = self._melding()
        with patch.object(
            type(self.env['l10n.no.eristo.idporten.service']),
            'start_authorize_flow', return_value=dict(_ACT_URL),
        ) as flow, patch.object(
            type(melding), '_get_altinn_token',
            side_effect=AssertionError("systembruker-token skal ikke hentes"),
        ):
            action = melding.action_submit()
        self.assertEqual(action['type'], 'ir.actions.act_url')
        kwargs = flow.call_args.kwargs
        self.assertEqual(kwargs['company'], self.company)
        self.assertEqual(kwargs['target_record'], melding)
        self.assertEqual(kwargs['callback_method'],
                         '_submit_med_idporten')

    def test_idporten_modus_gjelder_ogsaa_submitting(self):
        """'submitting' i idporten-modus krever nytt person-token (ny
        BankID) — cron kan ikke fullføre med systembruker-token."""
        melding = self._melding(state='submitting')
        with patch.object(
            type(self.env['l10n.no.eristo.idporten.service']),
            'start_authorize_flow', return_value=dict(_ACT_URL),
        ) as flow:
            action = melding.action_submit()
        self.assertEqual(action['type'], 'ir.actions.act_url')
        self.assertTrue(flow.called)

    def test_systembruker_modus_bruker_maskinporten(self):
        self.company.sudo().l10n_no_mva_submit_mode = 'systembruker'
        melding = self._melding()
        with patch.object(type(melding), '_get_altinn_token',
                          return_value='sb-token') as tok, \
             patch.object(type(melding), '_utfor_innsending',
                          return_value={'ok': True}) as utfor:
            melding.action_submit()
        self.assertTrue(tok.called)
        utfor.assert_called_once_with('sb-token')

    # ---------- callback fra /idporten/done ----------

    def test_callback_uten_token_feiler(self):
        """Manglende token skal gi den handlingsrettede beskjeden — og
        ALDRI nå innsendingssekvensen (som ville gjort ekte HTTP-kall
        med 'Bearer None')."""
        melding = self._melding()
        with patch.object(
            type(melding), '_utfor_innsending',
            side_effect=AssertionError("skal ikke nå innsending uten token"),
        ), self.assertRaises(UserError) as ctx:
            melding._submit_med_idporten()
        self.assertIn('ID-porten', str(ctx.exception))

    def test_callback_kjorer_innsending_med_person_token(self):
        melding = self._melding()
        with patch.object(type(melding), '_utfor_innsending',
                          return_value={'ok': True}) as utfor, \
             patch.object(
                 type(melding), '_get_altinn_token',
                 side_effect=AssertionError("skal bruke person-tokenet"),
             ):
            melding._submit_med_idporten(
                altinn_token='person-token', pid='26814795941')
        utfor.assert_called_once_with('person-token')

    def test_callback_logger_maskert_pid(self):
        """Sporbarhet uten fødselsnummer i klartekst i chatteren."""
        melding = self._melding()
        with patch.object(type(melding), '_utfor_innsending',
                          return_value={'ok': True}):
            melding._submit_med_idporten(
                altinn_token='person-token', pid='26814795941')
        bodies = ' '.join(melding.message_ids.mapped('body'))
        self.assertNotIn('26814795941', bodies)
        self.assertIn('5941', bodies)  # maskert referanse
        self.assertIn('ID-porten', bodies)

    def test_callback_i_submitted_state_henter_kvittering(self):
        melding = self._melding(state='submitted')
        with patch.object(type(melding), 'action_fetch_receipt',
                          return_value={'hentet': True}) as fetch:
            res = melding._submit_med_idporten(altinn_token='t')
        self.assertTrue(fetch.called)
        self.assertEqual(res, {'hentet': True})

    def test_callback_not_ready_gir_submitting_med_reklikk_beskjed(self):
        melding = self._melding()
        with patch.object(type(melding), '_altinn_create_instance',
                          return_value='konv-guid'), \
             patch.object(type(melding), '_altinn_finish_submission',
                          side_effect=AltinnNotReadyError('ikke klar')):
            action = melding._submit_med_idporten(altinn_token='t')
        self.assertEqual(melding.state, 'submitting')
        # I idporten-modus må brukeren logge inn på nytt — beskjeden skal
        # si det, ikke love automatisk fullføring.
        self.assertIn('Send inn', action['params']['message'])

    def test_callback_maskerer_kort_pid_helt(self):
        """pid[-4:] av en kort identifikator ville vært hele verdien i
        klartekst — korte pids skal maskeres fullstendig."""
        melding = self._melding()
        with patch.object(type(melding), '_utfor_innsending',
                          return_value={'ok': True}):
            melding._submit_med_idporten(altinn_token='t', pid='1234')
        bodies = ' '.join(melding.message_ids.mapped('body'))
        self.assertNotIn('1234', bodies)
        self.assertIn('****', bodies)

    def test_callback_avviser_feil_state(self):
        """Precheck skal stoppe callbacken for meldinger uten XML/state —
        aldri sende tom/stale XML til Skatteetaten."""
        melding = self._melding(state='draft')
        with patch.object(
            type(melding), '_utfor_innsending',
            side_effect=AssertionError("skal ikke sende fra draft"),
        ), self.assertRaises(UserError):
            melding._submit_med_idporten(altinn_token='t')

    def test_idporten_avviser_feil_state_for_authorize(self):
        """Brukeren skal stoppes FØR BankID-runden når meldingen ikke er
        klar — ikke etter."""
        melding = self._melding(state='draft')
        with patch.object(
            type(self.env['l10n.no.eristo.idporten.service']),
            'start_authorize_flow',
            side_effect=AssertionError("authorize skal ikke startes"),
        ), self.assertRaises(UserError):
            melding.action_submit()

    def test_idporten_avviser_manglende_xml(self):
        melding = self._melding(mvamelding_xml=False)
        with self.assertRaises(UserError) as ctx:
            melding.action_submit()
        self.assertIn('XML', str(ctx.exception))

    def test_callback_maa_vaere_allowlistet(self):
        """Broen skal nekte callbacks som ikke er deklarert i
        _idporten_callbacks — ellers er den en generisk
        «autentiser og kjør vilkårlig metode»-primitiv."""
        melding = self._melding()
        with self.assertRaises(UserError) as ctx:
            self.env['l10n.no.eristo.idporten.service'].start_authorize_flow(
                company=self.company,
                target_record=melding,
                callback_method='action_fetch_receipt',
            )
        self.assertIn('_idporten_callbacks', str(ctx.exception))

    # ---------- cron ----------

    def test_cron_hopper_over_idporten_submitting(self):
        """Compliance-guarden: cron skal ALDRI fullføre person-innsendinger
        med systembruker-token (instansen ville aldri blitt behandlet).
        Assert via mock-registrering — en plantet exception ville blitt
        svelget av cron-ens brede except."""
        melding = self._melding(state='submitting')
        tok = MagicMock(return_value='sb-token')
        with patch.object(type(melding), '_get_altinn_token', tok), \
             patch.object(type(melding), '_altinn_finish_submission') as fin:
            self.Melding._cron_complete_submissions()
        tok.assert_not_called()
        fin.assert_not_called()
        self.assertEqual(melding.state, 'submitting')

    def test_cron_fullforer_systembruker_submitting(self):
        self.company.sudo().l10n_no_mva_submit_mode = 'systembruker'
        melding = self._melding(state='submitting')
        with patch.object(type(melding), '_get_altinn_token',
                          return_value='sb-token'), \
             patch.object(type(melding), '_altinn_finish_submission') as fin:
            self.Melding._cron_complete_submissions()
        self.assertTrue(fin.called)

    def test_systembruker_submitting_ruter_til_continue(self):
        """Retry-klikk i systembruker-modus skal gjenbruke eksisterende
        instans (_continue_submission) — aldri opprette ny."""
        self.company.sudo().l10n_no_mva_submit_mode = 'systembruker'
        melding = self._melding(state='submitting')
        with patch.object(type(melding), '_continue_submission',
                          return_value={'ok': True}) as cont, \
             patch.object(
                 type(melding), '_altinn_create_instance',
                 side_effect=AssertionError("skal ikke opprette ny instans"),
             ):
            melding.action_submit()
        cont.assert_called_once()
