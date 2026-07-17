"""Tester for fastsettings- OG verdikt-logikken i mottaks-flyten.

To bug-klasser dekkes her:

1. «Betalingsinfo tolket som mottak» (rettet 2026-06-15): meldingen ble
   markert 'mottatt' så snart `betalingsinformasjon` dukket opp — men den
   genereres MENS Tilbakemelding-tasken kjører, FØR meldingen er ferdig-
   behandlet. Fix: krev feedback+kvittering / process.ended.

2. «Avvist tolket som mottatt» (rettet 2026-07-13): selv etter ferdig-
   behandling er ikke meldingen nødvendigvis GODKJENT. Skatteetaten legger et
   `valideringsresultat`-dataelement med verdiktet — 'ingen avvik' = fastsatt,
   'ugyldig skattemelding' = AVVIST. Modulen leste ikke verdiktet og markerte
   en avvist melding (TT02: kvittering M-2026-1800 «Ugyldig mva-melding …»)
   som 'mottatt'. Fix: parse valideringsresultat → state 'mottatt' vs 'avvist'.

Sentralt prinsipp: vi utleder ALDRI 'godkjent' fra fravær av signal. Mangler
verdikt-elementet (eller er det uleselig / feil namespace), utsetter vi — en
avvist melding har også process.ended/EndEvent, så det signalet alene beviser
ikke godkjenning.

Testene mocker Altinn-helperne (ingen nettverk) og verifiserer ren
beslutnings-/verdikt-logikk.
"""
from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from odoo.addons.l10n_no_account_mvamelding.models import (
    l10n_no_mvamelding_submit as submod,
)

_NS = 'no:skatteetaten:fastsetting:avgift:mva:valideringsresultat:v1'

# Ekte valideringsresultat fra TT02 (FREIDIG HARDHUDET TIGER AS, 2026-07-13):
# meldingen ble AVVIST fordi virksomheten ikke var registrert for alminnelig
# næring. Toppnivå-verdiktet er 'ugyldig skattemelding'.
_VR_UGYLDIG = f"""<?xml version='1.0' encoding='UTF-8'?>
<valideringsresultat xmlns="{_NS}">
  <avvikVedMeldingslevering>ugyldig skattemelding</avvikVedMeldingslevering>
  <avvik>
    <stiTilAvvik>//meldingskategori</stiTilAvvik>
    <mvaKode>null</mvaKode>
    <avviksinformasjon>
      <begrunnelse>Virksomheten er ikke registrert i Merverdiavgiftsregisteret.</begrunnelse>
      <avvikstype>ugyldig skattemelding</avvikstype>
      <avvikKode>MVA_PLIKT_OPPGITT_MELDINGSKATEGORI_ALMINNELIG_NAERING_FINNES_IKKE</avvikKode>
    </avviksinformasjon>
  </avvik>
</valideringsresultat>"""

# Godkjent melding: ingen avvik.
_VR_GODKJENT = f"""<?xml version='1.0' encoding='UTF-8'?>
<valideringsresultat xmlns="{_NS}">
  <avvikVedMeldingslevering>ingen avvik</avvikVedMeldingslevering>
</valideringsresultat>"""

# Fastsatt MED merknad: 'avvikende innsendingstype' + ett avvik. Godkjent, men
# merknaden skal bevares i audit-sporet.
_VR_MERKNAD = f"""<?xml version='1.0' encoding='UTF-8'?>
<valideringsresultat xmlns="{_NS}">
  <avvikVedMeldingslevering>avvikende innsendingstype</avvikVedMeldingslevering>
  <avvik>
    <stiTilAvvik>//mvaSpesifikasjonslinje</stiTilAvvik>
    <avviksinformasjon>
      <begrunnelse>Kontroller grunnlaget for kode 3.</begrunnelse>
      <avvikstype>avvikende innsendingstype</avvikstype>
      <avvikKode>MVA_GRUNNLAG_ADVARSEL</avvikKode>
    </avviksinformasjon>
  </avvik>
</valideringsresultat>"""

# Ugyldig, men toppnivå-verdiktet mangler (element utelatt) — bare <avvik>.
_VR_MANGLER_VERDIKT = f"""<?xml version='1.0' encoding='UTF-8'?>
<valideringsresultat xmlns="{_NS}">
  <avvik>
    <stiTilAvvik>//x</stiTilAvvik>
    <avviksinformasjon>
      <begrunnelse>Uleselig verdikt, men et avvik finnes.</begrunnelse>
      <avvikKode>MVA_UKJENT</avvikKode>
    </avviksinformasjon>
  </avvik>
</valideringsresultat>"""

# Riktig struktur, men feil (drevet) namespace → vi forstår ikke dokumentet.
_VR_FEIL_NS = """<?xml version='1.0' encoding='UTF-8'?>
<valideringsresultat xmlns="no:skatteetaten:fastsetting:avgift:mva:valideringsresultat:v2">
  <avvikVedMeldingslevering>ingen avvik</avvikVedMeldingslevering>
</valideringsresultat>"""


@tagged('post_install', '-at_install')
class TestMvameldingReceipt(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.sudo().l10n_no_eristo_test_orgnr = '310200808'
        cls.mva = cls.env['l10n.no.mvamelding'].create({
            'company_id': cls.company.id,
            'aar': 2026,
            'periode': '2',
            'state': 'submitted',
            'altinn_instance_owner_party_id': '51542399',
            'altinn_instance_guid': '00000000-0000-0000-0000-000000000001',
        })

    def _run(self, status, instance, vr_xml=None):
        """Kjør _do_fetch_receipt med Altinn-helperne mocket.

        status  = isFeedbackProvided (True/False/None)
        instance = dict returnert av både feedback- og instans-GET
        vr_xml  = XML som _download_data returnerer for valideringsresultat-
                  elementet (None → download returnerer None)
        """
        Model = type(self.mva)
        patches = [
            patch.object(Model, '_altinn_feedback_status', return_value=status),
            patch.object(Model, '_altinn_get_feedback', return_value=instance),
            patch.object(Model, '_altinn_get_instance', return_value=instance),
            patch.object(Model, '_fetch_betalingsinformasjon',
                         return_value=None),
            patch.object(Model, '_altinn_storage_base',
                         return_value='https://storage.example/x'),
            # Vedleggs-bygging (nettverk + ir.attachment) stubbes til no-op så
            # vi tester ren verdikt-/beslutnings-logikk.
            patch.object(Model, '_bygg_altinn_vedlegg',
                         return_value=([], True)),
            # _download_data brukes KUN til å hente valideringsresultat her.
            patch.object(Model, '_download_data', return_value=vr_xml),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        return self.mva._do_fetch_receipt('faketoken')

    # ---- bug-klasse 1: ikke ferdigbehandlet ----

    def test_betalingsinfo_alone_is_not_mottatt(self):
        """REGRESJON: betalingsinformasjon uten kvittering/ended → IKKE mottatt."""
        instance = {
            'data': [{'dataType': 'betalingsinformasjon', 'id': 'b1'},
                     {'dataType': 'mvamelding', 'id': 'm1'}],
            'process': {'currentTask': {'altinnTaskType': 'feedback'},
                        'ended': None},
        }
        result = self._run(status=False, instance=instance)
        self.assertFalse(result)
        self.assertEqual(self.mva.state, 'submitted')
        self.assertFalse(self.mva.kvittering_archived_at)

    # ---- bug-klasse 2: verdikt (godkjent vs avvist) ----

    def test_ugyldig_valideringsresultat_is_avvist_not_mottatt(self):
        """REGRESJON (TT02 M-2026-1800): valideringsresultat 'ugyldig
        skattemelding' → state 'avvist', ALDRI 'mottatt'."""
        instance = {
            'data': [{'dataType': 'betalingsinformasjon', 'id': 'b1'},
                     {'dataType': 'valideringsresultat', 'id': 'v1'},
                     {'dataType': 'kvittering', 'id': 'k1'},
                     {'dataType': 'mvamelding', 'id': 'm1'}],
            'process': {'ended': '2026-07-13T12:24:06Z', 'endEvent': 'EndEvent_1'},
        }
        result = self._run(status=True, instance=instance, vr_xml=_VR_UGYLDIG)
        self.assertTrue(result)  # ferdigbehandlet — slutt å polle
        self.assertEqual(self.mva.state, 'avvist')
        self.assertFalse(self.mva.mottatt_at)
        # Verdikt-beviset er lagret for UI-et (fanen «Avvik / valideringsresultat»).
        self.assertTrue(self.mva.valideringsresultat_xml)
        self.assertEqual(self.mva.avvik_count, 1)

    def test_ingen_avvik_valideringsresultat_is_mottatt(self):
        """valideringsresultat 'ingen avvik' → godkjent → mottatt (rent, uten
        at valideringsresultat-fanen dukker opp)."""
        instance = {
            'data': [{'dataType': 'valideringsresultat', 'id': 'v1'},
                     {'dataType': 'kvittering', 'id': 'k1'},
                     {'dataType': 'mvamelding', 'id': 'm1'}],
            'process': {'ended': '2026-07-13T12:24:06Z', 'endEvent': 'EndEvent_1'},
        }
        result = self._run(status=True, instance=instance, vr_xml=_VR_GODKJENT)
        self.assertTrue(result)
        self.assertEqual(self.mva.state, 'mottatt')
        self.assertTrue(self.mva.mottatt_at)
        self.assertFalse(self.mva.valideringsresultat_xml)

    def test_avvikende_merknad_is_mottatt_and_stored(self):
        """Fastsatt MED merknad ('avvikende ...', avvik>0) → mottatt, men
        merknaden bevares (valideringsresultat_xml + avvik_count)."""
        instance = {
            'data': [{'dataType': 'valideringsresultat', 'id': 'v1'},
                     {'dataType': 'kvittering', 'id': 'k1'}],
            'process': {'ended': '2026-07-13T12:24:06Z', 'endEvent': 'EndEvent_1'},
        }
        result = self._run(status=True, instance=instance, vr_xml=_VR_MERKNAD)
        self.assertTrue(result)
        self.assertEqual(self.mva.state, 'mottatt')
        self.assertTrue(self.mva.valideringsresultat_xml)
        self.assertEqual(self.mva.avvik_count, 1)

    def test_missing_top_verdict_with_avvik_is_avvist(self):
        """Toppnivå-verdikt mangler men <avvik> finnes → behandles som avvist
        (aldri mottatt fra fravær av signal)."""
        instance = {
            'data': [{'dataType': 'valideringsresultat', 'id': 'v1'},
                     {'dataType': 'kvittering', 'id': 'k1'}],
            'process': {'ended': '2026-07-13T12:24:06Z', 'endEvent': 'EndEvent_1'},
        }
        result = self._run(status=True, instance=instance,
                           vr_xml=_VR_MANGLER_VERDIKT)
        self.assertTrue(result)
        self.assertEqual(self.mva.state, 'avvist')

    # ---- utsettelse: verdikt kan ikke avgjøres ennå ----

    def test_finalized_without_valideringsresultat_defers(self):
        """Feedback gitt + kvittering, MEN valideringsresultat-elementet mangler
        → verdikt ukjent → utsett (IKKE mottatt). process.ended alene beviser
        ikke godkjenning (en avvist melding har det også)."""
        instance = {
            'data': [{'dataType': 'betalingsinformasjon', 'id': 'b1'},
                     {'dataType': 'kvittering', 'id': 'k1'},
                     {'dataType': 'mvamelding', 'id': 'm1'}],
            'process': {'ended': '2026-06-15T10:00:00Z', 'endEvent': 'EndEvent_1'},
        }
        result = self._run(status=True, instance=instance)
        self.assertFalse(result)
        self.assertEqual(self.mva.state, 'submitted')
        self.assertFalse(self.mva.kvittering_archived_at)

    def test_process_ended_without_valideringsresultat_defers(self):
        """Backup-signal process.ended alene, uten valideringsresultat → utsett
        (tidligere ble dette feilaktig markert mottatt)."""
        instance = {
            'data': [{'dataType': 'kvittering', 'id': 'k1'}],
            'process': {'ended': '2026-06-15T10:00:00Z'},
        }
        result = self._run(status=None, instance=instance)
        self.assertFalse(result)
        self.assertEqual(self.mva.state, 'submitted')

    def test_valideringsresultat_present_but_download_fails_defers(self):
        """valideringsresultat-element finnes men nedlasting feiler (None) →
        verdikt ukjent → utsett."""
        instance = {
            'data': [{'dataType': 'valideringsresultat', 'id': 'v1'},
                     {'dataType': 'kvittering', 'id': 'k1'}],
            'process': {'ended': '2026-07-13T12:24:06Z', 'endEvent': 'EndEvent_1'},
        }
        result = self._run(status=True, instance=instance, vr_xml=None)
        self.assertFalse(result)
        self.assertEqual(self.mva.state, 'submitted')
        self.assertFalse(self.mva.kvittering_archived_at)

    def test_wrong_namespace_defers(self):
        """valideringsresultat under et drevet/ukjent namespace → vi forstår det
        ikke → utsett i stedet for å gjette godkjent."""
        instance = {
            'data': [{'dataType': 'valideringsresultat', 'id': 'v1'},
                     {'dataType': 'kvittering', 'id': 'k1'}],
            'process': {'ended': '2026-07-13T12:24:06Z', 'endEvent': 'EndEvent_1'},
        }
        result = self._run(status=True, instance=instance, vr_xml=_VR_FEIL_NS)
        self.assertFalse(result)
        self.assertEqual(self.mva.state, 'submitted')

    # ---- submit-stien: rett-etter-innsending-poll må også respektere avvist ----

    def test_after_submit_fetch_avvist_notifies_avvist(self):
        """REGRESJON: _after_submit_fetch må vise avvist-varsel (ikke grønn
        'mottatt') når rett-etter-innsending-pollen lander på et avvist verdikt.
        _do_fetch_receipt returnerer True for BÅDE mottatt og avvist."""
        Model = type(self.mva)

        def fake_fetch(record, _token):
            record.state = 'avvist'
            return True

        patches = [
            patch.object(Model, '_do_fetch_receipt', fake_fetch),
            patch.object(submod.time, 'sleep', lambda *a, **k: None),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])

        action = self.mva._after_submit_fetch('faketoken')
        self.assertEqual(action['params']['type'], 'danger')
        self.assertEqual(self.mva.state, 'avvist')
