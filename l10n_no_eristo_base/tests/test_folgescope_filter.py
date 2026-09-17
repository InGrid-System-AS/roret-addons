"""Filteret som avgjør hva /eristo-ping får lov å skrive til selskapet.

Feltet `l10n_no_eristo_active_scopes` bærer to vokabularer med vilje:

  * ONBOARDING-nøkler — det wizarden ber om, og det modulenes
    aktivert-flagg leser (MVA onboardes som skatteetaten:mvamelding)
  * TOKEN-scopes — det gatewayen faktisk kan mint, og det eneste
    /eristo-ping rapporterer (MVA får altinn:instances.write)

De to overlapper, og overlappet er en felle: `altinn:instances.write`
er BÅDE MVA-ens token-scope OG årsregnskaps onboarding-nøkkel
(_ARSREGNSKAP_SCOPE). Leser vi pingen ufiltrert inn, får et selskap som
kun har MVA plutselig l10n_no_arsregnskap_aktivert = True — og en
innsending mot en delegering som ikke finnes.

Begge feilmodusene har vært i produksjonskoden i denne PR-ens levetid,
og begge var stille:

  * erstatning av lista → skatteetaten:mvamelding forsvant, MVA-porten
    i l10n_no_mvamelding_submit blokkerte et fullt aktivert selskap
  * ufiltrert union     → årsregnskap slått falskt på

BEGGE lå i SKRIVESTIENE, ikke i filterfunksjonen — den fantes ikke
engang da. Fila er derfor delt i to, og delingen er poenget:

  * TestFolgescopeFilter låser NAVNENE i folgescopes_fra_ping. Nyttig,
    men blind for hvordan kallstedene bruker svaret.
  * TestScopeSynkFraPing kjører de to stedene som faktisk skriver
    l10n_no_eristo_active_scopes — «Test Eristo-forbindelse» og
    wizardens _sync_active_scopes — og sjekker feltet etterpå. Det er
    her en revert av filteret eller av «utvid, ikke erstatt» blir rød.

Uten den andre klassen ville alle testene her forblitt grønne gjennom
begge produksjonsfeilene.
"""
import json
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase

from ..models.l10n_no_eristo import (
    GATEWAY_FOLGESCOPES,
    folgescopes_fra_ping,
)

_WIZARD_LOGGER = (
    'odoo.addons.l10n_no_eristo_base.wizard.l10n_no_eristo_onboarding_wizard')


def _urlopen_svar(payload):
    """Etterlign urllib-kontrakten ping() bruker: `with urlopen(...) as r`."""
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode()
    ctx = MagicMock()
    ctx.__enter__.return_value = resp
    return ctx


class TestFolgescopeFilter(TransactionCase):

    def test_folgescope_slippes_gjennom(self):
        """digdir:dialogporten er hele grunnen til at filteret finnes."""
        self.assertEqual(
            folgescopes_fra_ping(['digdir:dialogporten']),
            ['digdir:dialogporten'],
        )

    def test_arsregnskapsnokkelen_slippes_ikke_gjennom(self):
        """Regresjonen: MVA-ens token-scope ER årsregnskaps onboarding-
        nøkkel. Slipper den inn, aktiveres en tjeneste kunden aldri har
        onboardet."""
        self.assertEqual(folgescopes_fra_ping(['altinn:instances.write']), [])

    def test_onboarding_nokler_slippes_ikke_gjennom(self):
        """Onboarding-nøkler kommer fra scopes_text, ikke fra pingen —
        de skal ikke kunne snike seg inn denne veien."""
        self.assertEqual(folgescopes_fra_ping([
            'skatteetaten:mvamelding',
            'skatteetaten:innrapporteringamelding',
            'skatteetaten:formueinntekt/skattemelding',
        ]), [])

    def test_blandet_svar_gir_kun_folgescopet(self):
        """Et a-melding+MVA-selskap: begge formene i samme svar."""
        self.assertEqual(folgescopes_fra_ping([
            'skatteetaten:innrapporteringamelding',
            'altinn:instances.write',
            'digdir:dialogporten',
        ]), ['digdir:dialogporten'])

    def test_tomt_og_manglende_svar_er_trygt(self):
        """En gammel gateway uten feltet skal ikke velte skrivestien."""
        for tomt in (None, [], ()):
            self.assertEqual(folgescopes_fra_ping(tomt), [])

    def test_whitespace_normaliseres(self):
        """Verdiene sammenlignes mot flaggenes konstanter — en ubetydelig
        forskjell i form skal ikke gi et scope som aldri matcher."""
        self.assertEqual(
            folgescopes_fra_ping([' digdir:dialogporten ']),
            ['digdir:dialogporten'],
        )

    def test_hvitelista_er_eksplisitt(self):
        """Hvitelista skal utvides bevisst, ikke gro. Endres den, skal
        denne testen tvinge fram et valg — og et blikk på om det nye
        scopet kolliderer med en onboarding-nøkkel et sted."""
        self.assertEqual(GATEWAY_FOLGESCOPES, frozenset({'digdir:dialogporten'}))


class TestScopeSynkFraPing(TransactionCase):
    """Selve skrivestien, ikke bare filteret.

    Begge defektene lå HER og ikke i filteret: først skrev kallstedet
    pingens liste rått (erstatning), så la det inn hele lista (union).
    En test på filteret alene ville vært grønn gjennom begge.
    """

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.company.write({
            'l10n_no_eristo_token_url': 'https://api.example/maskinporten-token',
            'l10n_no_eristo_api_key': 'testnokkel',
        })

    def _klikk_test_forbindelse(self, rapporterte_scopes):
        svar = _urlopen_svar({
            'status': 'ok',
            'gateway_version': '1.1.0',
            'customer': {
                'name': 'Testkunde AS',
                'orgnr': '987654325',
                'environment': 'test',
                'active_scopes': rapporterte_scopes,
            },
        })
        with patch('urllib.request.urlopen', return_value=svar):
            self.company.action_l10n_no_test_eristo_connection()
        return list(self.company.l10n_no_eristo_active_scopes or [])

    def test_folgescopet_kommer_inn(self):
        """Poenget med synken: kvitteringstilgangen blir synlig uten at
        noen kjører onboarding på nytt."""
        self.company.l10n_no_eristo_active_scopes = [
            'skatteetaten:innrapporteringamelding',
        ]
        etter = self._klikk_test_forbindelse([
            'skatteetaten:innrapporteringamelding', 'digdir:dialogporten',
        ])
        self.assertIn('digdir:dialogporten', etter)
        self.assertIn('skatteetaten:innrapporteringamelding', etter)

    def test_mva_kunde_far_ikke_arsregnskap_pa_kjopet(self):
        """Regresjonen fra runde 3: pingen rapporterer MVA-ens TOKEN-scope
        altinn:instances.write, som er årsregnskaps ONBOARDING-nøkkel. Ett
        klikk på en diagnoseknapp skal ikke aktivere en tjeneste kunden
        aldri har onboardet."""
        self.company.l10n_no_eristo_active_scopes = ['skatteetaten:mvamelding']
        etter = self._klikk_test_forbindelse(['altinn:instances.write'])
        self.assertNotIn('altinn:instances.write', etter)

    def test_onboarding_nokkelen_overlever(self):
        """Regresjonen fra runde 2: en erstatning fjernet
        skatteetaten:mvamelding og blokkerte MVA-innsending for et fullt
        aktivert selskap. Synken skal aldri fjerne noe."""
        self.company.l10n_no_eristo_active_scopes = ['skatteetaten:mvamelding']
        etter = self._klikk_test_forbindelse(['altinn:instances.write'])
        self.assertIn('skatteetaten:mvamelding', etter)

    def _wizard_synk(self, scopes_text, rapporterte_scopes):
        """Wizardens skrivesti — det ANDRE stedet som skriver feltet."""
        wizard = self.env['l10n.no.eristo.onboarding.wizard'].create({
            'company_id': self.company.id,
            'scopes_text': scopes_text,
        })
        svar = _urlopen_svar({
            'status': 'ok',
            'gateway_version': '1.1.0',
            'customer': {
                'name': 'Testkunde AS',
                'orgnr': '987654325',
                'environment': 'test',
                'active_scopes': rapporterte_scopes,
            },
        })
        with patch('urllib.request.urlopen', return_value=svar):
            wizard._sync_active_scopes()
        return list(self.company.l10n_no_eristo_active_scopes or [])

    def test_wizarden_tar_med_folgescopet(self):
        """Wizarden ber om a-melding; gatewayen svarer med følgescopet i
        tillegg. Uten dette er kvitteringstilgangen usynlig for hver ny
        kunde — feilen hele denne PR-en handler om."""
        self.company.l10n_no_eristo_active_scopes = []
        etter = self._wizard_synk(
            'skatteetaten:innrapporteringamelding',
            ['skatteetaten:innrapporteringamelding', 'digdir:dialogporten'],
        )
        self.assertIn('skatteetaten:innrapporteringamelding', etter)
        self.assertIn('digdir:dialogporten', etter)

    def test_wizarden_slipper_ikke_gjennom_fremmed_token_scope(self):
        """Samme grense som på knappen: MVA onboardes som
        skatteetaten:mvamelding, men pingen rapporterer token-scopet.
        Det skal ikke bli årsregnskaps onboarding-nøkkel i feltet."""
        self.company.l10n_no_eristo_active_scopes = []
        etter = self._wizard_synk(
            'skatteetaten:mvamelding', ['altinn:instances.write'],
        )
        self.assertIn('skatteetaten:mvamelding', etter)
        self.assertNotIn('altinn:instances.write', etter)

    def test_wizarden_overlever_at_pingen_feiler(self):
        """Delegeringen ER godkjent når synken kjører. En feilende ping
        skal degradere til de forespurte scopene, ikke velte
        accept-steget og sende kunden inn i en ny onboarding-runde."""
        self.company.l10n_no_eristo_active_scopes = []
        wizard = self.env['l10n.no.eristo.onboarding.wizard'].create({
            'company_id': self.company.id,
            'scopes_text': 'skatteetaten:innrapporteringamelding',
        })
        # Wizarden logger WARNING med traceback når pingen feiler. Det er
        # riktig i drift, men Odoo.sh markerer bygget «Test: Failed» på en
        # traceback i loggen uansett nivå (sett på v2.2.1-bumpen, 589
        # tester grønne). assertLogs hevder linjen og holder den unna.
        with patch('urllib.request.urlopen', side_effect=OSError("nede")), \
                self.assertLogs(_WIZARD_LOGGER, level='WARNING') as logg:
            wizard._sync_active_scopes()
        self.assertIn('active_scopes', logg.output[0])
        self.assertEqual(
            list(self.company.l10n_no_eristo_active_scopes or []),
            ['skatteetaten:innrapporteringamelding'],
        )
