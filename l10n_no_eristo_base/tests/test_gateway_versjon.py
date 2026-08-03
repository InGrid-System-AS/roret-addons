"""Versjonssammenligningen mot Roret Compliance Gateway.

Denne logikken avgjør om kunden får en advarsel om utdatert tjeneste.
Begge feilmodusene er STILLE og derfor dyre:

  * falsk alarm  → kunden lærer å ignorere advarselen, og overser den
                   ekte neste gang
  * manglende alarm → nøyaktig juli-hendelsen: ID-porten-endepunktene
                   fulgte med en repoint til en gateway som manglet
                   ID-porten-klienten, svarte 501, og MVA/skattemelding
                   var nede til noen lette etter det

Testen låser også at sammenligningen er NUMERISK. En refaktorering til
streng-sammenligning ville gjort 1.10.0 eldre enn 1.2.0 — og da varsler
vi om en gateway som i virkeligheten er nyere enn kravet.
"""
from odoo.tests.common import TransactionCase

from ..models.l10n_no_eristo import GATEWAY_MIN_VERSION, gateway_for_gammel


class TestGatewayVersjon(TransactionCase):

    def test_eldre_gateway_gir_advarsel(self):
        self.assertTrue(gateway_for_gammel('1.0.0', minimum='1.1.0'))
        self.assertTrue(gateway_for_gammel('0.9.9', minimum='1.1.0'))

    def test_eksakt_minimum_er_godkjent(self):
        """Minstekravet er inklusivt — lik versjon skal ikke advare."""
        self.assertFalse(gateway_for_gammel('1.1.0', minimum='1.1.0'))

    def test_nyere_gateway_er_godkjent(self):
        self.assertFalse(gateway_for_gammel('1.2.0', minimum='1.1.0'))
        self.assertFalse(gateway_for_gammel('2.0.0', minimum='1.1.0'))

    def test_sammenligningen_er_numerisk_ikke_alfabetisk(self):
        """1.10.0 er NYERE enn 1.2.0. Streng-sammenligning ville sagt
        motsatt og gitt falsk advarsel på en oppdatert gateway."""
        self.assertFalse(gateway_for_gammel('1.10.0', minimum='1.2.0'))
        self.assertTrue(gateway_for_gammel('1.2.0', minimum='1.10.0'))

    def test_ukjent_versjon_gir_aldri_falsk_alarm(self):
        """En gateway fra før feltet fantes oppgir ingen versjon. Da vet
        vi ingenting — og skal tie, ikke gjette."""
        for verdi in (None, '', '   '):
            self.assertFalse(gateway_for_gammel(verdi, minimum='1.1.0'),
                             f"{verdi!r} skulle ikke gi advarsel")

    def test_uparsebar_versjon_gir_aldri_falsk_alarm(self):
        for verdi in ('rart', '2.0', '1.2.3.4', 'v1.2.0', '1.x.0'):
            self.assertFalse(gateway_for_gammel(verdi, minimum='1.1.0'),
                             f"{verdi!r} skulle ikke gi advarsel")

    def test_minstekravet_er_selv_gyldig_semver(self):
        """Vern mot skrivefeil i konstanten: er den uparsebar, slutter
        HELE sjekken å virke — stille."""
        deler = GATEWAY_MIN_VERSION.split('.')
        self.assertEqual(len(deler), 3, GATEWAY_MIN_VERSION)
        self.assertTrue(all(d.isdigit() for d in deler), GATEWAY_MIN_VERSION)
        # Og den må faktisk brukes: en gateway under kravet skal advare
        self.assertTrue(gateway_for_gammel('0.0.1'))
