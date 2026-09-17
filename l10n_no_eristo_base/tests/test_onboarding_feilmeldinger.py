"""Feilmeldingene fra request_onboarding (issue #85).

Onboarding-flaten kan være stengt mot offentlig nett — enten fordi
kunden peker på en tjeneste som ikke eksponerer den, eller fordi URL-en
er feil. nginx svarer da 404 uten å røpe at endepunktet finnes, og
kroppen er HTML.

Uten en egen 404-gren fikk brukeren «Eristo onboarding-service feilet
(HTTP 404)» etterfulgt av en nginx-HTML-side. Det er nøyaktig den
uforståelige feilen som gjorde at admin-secret-mismatchen ble
feildiagnostisert som et secret-problem i månedsvis: symptomet pekte
ikke på årsaken.

Testen låser at meldingen nevner URL-en og hva brukeren skal gjøre.
"""
import urllib.error
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase

# Tjenesten logger hver HTTP-feil på ERROR før den oversetter den til en
# UserError. Det er riktig i drift, men i testene er feilen selve poenget:
# assertLogs hevder at linjen kommer, og holder den unna byggeloggen. Odoo.sh
# markerer bygget «Test: Failed» på ERROR-linjer alene, uavhengig av
# testresultatet (InGrid-System-AS/odoo#68).
_SERVICE_LOGGER = 'odoo.addons.l10n_no_eristo_base.models.l10n_no_eristo'


def _http_feil(kode, kropp=b"<html>404 Not Found (nginx)</html>"):
    """Bygg en HTTPError slik urllib faktisk kaster den."""
    import io
    return urllib.error.HTTPError(
        url="https://api.example/onboard-systembruker", code=kode,
        msg="Not Found", hdrs=None, fp=io.BytesIO(kropp),
    )


class TestOnboardingFeilmeldinger(TransactionCase):

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.company.write({
            'l10n_no_eristo_token_url': 'https://api.example/maskinporten-token',
            'l10n_no_eristo_api_key': 'testnokkel',
        })
        self.service = self.env['l10n.no.eristo.service']

    def _kall(self, kode, kropp=b"<html>404 Not Found (nginx)</html>"):
        with patch('urllib.request.urlopen',
                   side_effect=_http_feil(kode, kropp)), \
                self.assertLogs(_SERVICE_LOGGER, level='ERROR') as logg:
            with self.assertRaises(UserError) as ctx:
                self.service.request_onboarding(
                    self.company, ['skatteetaten:mvamelding'])
        self.assertIn(str(kode), logg.output[0])
        return str(ctx.exception)

    def test_404_forklarer_at_flaten_ikke_er_eksponert(self):
        melding = self._kall(404)
        # Skal peke på URL-en som ble forsøkt — uten den må brukeren gjette
        self.assertIn('onboard-systembruker', melding)
        self.assertIn('ikke tilgjengelig', melding)
        # Og IKKE dumpe nginx-HTML-en, som ikke sier noe
        self.assertNotIn('<html>', melding)

    def test_403_gjengir_tjenestens_begrunnelse(self):
        """party_orgnr-override i prod. Her ER kroppen informativ —
        den skal vises, ikke skjules."""
        melding = self._kall(
            403, b'{"error":"party_override_not_allowed","detail":"kun test"}')
        # Tjenestens egen begrunnelse skal frem ...
        self.assertIn('party_override_not_allowed', melding)
        # ... men assertionen over alene er sann OGSÅ uten 403-grenen,
        # siden den generiske grenen interpolerer samme kropp. Ordlyden
        # er det eneste som skiller dem — uten denne er testen vakuøs.
        self.assertIn('avvist av tjenesten', melding)
        # HTTP-koden er hele poenget med grenen for et mellomledd som
        # svarer 403 med HTML: uten den er statuslinja borte og meldingen
        # like ubrukelig som den 404-grenen ble lagt til for å fjerne.
        self.assertIn('403', melding)

    def test_401_peker_paa_api_key(self):
        melding = self._kall(401, b'{"error":"invalid_api_key"}')
        self.assertIn('API-key', melding)

    def test_ukjent_kode_faller_tilbake_med_kropp(self):
        """Ingen av grenene skal svelge en ukjent feil — 502 fra Altinn
        må fortsatt nå brukeren med tjenestens egen forklaring."""
        melding = self._kall(502, b'{"error":"altinn_error"}')
        self.assertIn('502', melding)
        self.assertIn('altinn_error', melding)
