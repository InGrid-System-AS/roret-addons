"""Altinn 3 innsendings-flyt for mva-melding — helautomatisk.

Speiler den utprøvde skattemelding-flyten, men med MVA-tilpasninger:

  1. Maskinporten-token (scope altinn:instances.write — Altinn-app-flyt)
  2. Exchange → Altinn-token (/authentication/api/v1/exchange/maskinporten)
  3. POST /instances → opprett instans (instanceOwner.organisationNumber)
  4. PUT konvolutt til det FORHÅNDS-OPPRETTEDE data-elementet
     (dataType no.skatteetaten...mvameldinginnsending.v1.0)
  5. POST mva-melding som eget data-element (dataType=mvamelding)
  6. PUT process/next → fullfør utfylling (appen re-validerer; 409 +
     valideringsresultat ved feil)
  7. PUT process/next → fullfør innsending → instansen går til
     tilbakemelding (feedback). HELautomatisk — ingen personlig signering
     (slik Fiken/Tripletex gjør).
  8. Fastsetting fullføres i kvittering-fasen: poll GET …/feedback/status til
     isFeedbackProvided=true, hent så GET …/feedback (instans m/ kvittering).
     Se l10n_no_mvamelding_kvittering.py. Å NÅ feedback-tasken er IKKE det
     samme som fastsatt — meldingen er først levert når kvittering finnes.

Forskjell fra skattemelding: skattemelding stopper ved upload og lar bruker
signere i Altinn-portalen (krever fnr-spor). MVA kan sendes maskinelt av
systembruker (delegert tilgangspakken merverdiavgift), så vi kjører
process/next selv.

NB: TT02-appen skd/mva-melding-innsending-test kjører IKKE fastsettings-
backenden — feedback/status forblir false, så happy-path (kvittering/EndEvent)
kan ikke observeres i test. Verifisert mot prod-flyt; finalize-grenen dekkes
av tests/test_receipt.py.
"""
import json
import logging
import time

import requests
from lxml import etree

import odoo.modules.module
from odoo import _, fields, models
from odoo.exceptions import UserError

from .l10n_no_mvamelding import SCOPE_INNSENDING
from .l10n_no_mvamelding_validate import _NS_VALIDERING

_logger = logging.getLogger(__name__)

_ALTINN_PLATFORM_HOSTS = {
    'test': 'https://platform.tt02.altinn.no',
    'prod': 'https://platform.altinn.no',
}
_ALTINN_APP_HOSTS = {
    'test': 'https://skd.apps.tt02.altinn.no',
    'prod': 'https://skd.apps.altinn.no',
}
# App-slug skiller seg per miljø. Verifisert mot live applicationmetadata
# 2026-06-07: TT02 = mva-melding-innsending-test (200), prod = -v1.
# NB: den gamle -etm2-slugen (fra 2021-eksempelfiler) er dekommisjonert
# (svarer 418 på skd.apps.tt02.altinn.no).
_MVA_APP_ID = {
    # Live TT02-app (dokumentert i api-dokumentasjon). Ressursen er
    # delegable:False, men systembrukeren får tilgang via tilgangspakken
    # `merverdiavgift` (delegert ved onboarding) — det omgår ressurs-flagget.
    'test': 'skd/mva-melding-innsending-test',
    'prod': 'skd/mva-melding-innsending-v1',
}

# dataType-er i Altinn-appen (verifisert mot instans.json 2026-06-05).
_KONVOLUTT_DATATYPE = (
    'no.skatteetaten.fastsetting.avgift.mva.mvameldinginnsending.v1.0'
)
_MVAMELDING_DATATYPE = 'mvamelding'

_HTTP_TIMEOUT = 60
_MAX_PROCESS_ITERATIONS = 5
# Backoff for Altinns kjente race-condition (instans/data-endpoint ikke
# umiddelbart synlig etter create_instance). Empirisk på TT02 for MVA-appen
# kan instansen bruke >25s på å bli GET-bar, så vi gir ~65s totalt. Hvis det
# fortsatt ikke holder, er flyten idempotent: re-kjør 'Send inn' gjenbruker
# den persistede instansen (altinn_instance_guid).
_BACKOFF = [0, 5, 10, 20, 30]
# Backoff for å hente kvittering RETT etter innsending. Skatteetaten genererer
# betalingsinformasjon typisk innen ~30–45s etter at innsendingen er fullført,
# så vi poller kort (~46s totalt) i samme request slik at brukeren ser
# beløp/frist/KID med en gang i stedet for å vente på cron. MVA-innsending er en
# sjelden, bevisst handling, så en kort blokkering her er akseptabelt.
_RECEIPT_BACKOFF = [0, 8, 12, 12, 14]


class AltinnNotReadyError(UserError):
    """Instansen er opprettet, men ennå ikke operativ (404 på data/process
    rett etter create). TT02 bruker ~60–120s på å gjøre en ny instans
    tilgjengelig. Signaliserer til action_submit/cron at innsendingen skal
    fullføres ASYNKRONT (status 'submitting' → cron) i stedet for å feile og
    tvinge brukeren til å klikke 'Send inn' på nytt."""


def _resolve_env(company):
    env_key = company.l10n_no_eristo_environment or 'test'
    if env_key not in _ALTINN_APP_HOSTS:
        raise UserError(_(
            "Ukjent Altinn-miljø '%(env)s' for selskap %(name)s.",
            env=env_key, name=company.name,
        ))
    return env_key


class L10nNoMvamelding(models.Model):
    _inherit = 'l10n.no.mvamelding'

    # Allow-list for ID-porten-broen: kun disse metodene kan registreres
    # som callback i start_authorize_flow (håndheves i service + controller).
    _idporten_callbacks = ('_submit_med_idporten',)

    # ---------- offentlig action ----------

    def action_submit(self):
        """Send mva-meldingen til Skatteetaten via Altinn.

        Innsendingsmodus (company.l10n_no_mva_submit_mode):
          - idporten (default): brukeren autentiseres med BankID og
            innsendingen skjer med PERSON-token. Skatteetatens
            behandlingsløp henter per 2026-07 ikke systembruker-
            innsendinger (A/B-verifisert i TT02 — se
            docs/mva-systembruker-funn.md), så dette er eneste flyt som
            faktisk fastsettes i dag.
          - systembruker: helautomatisk (gjenaktiveres når Skatteetaten
            bekrefter støtte — resten av koden er identisk).
        """
        self.ensure_one()
        # Allerede sendt inn → ikke re-advance process/next (ville gitt 4xx
        # eller advance et uventet steg). Hent heller kvittering.
        if self.state == 'submitted':
            return self.action_fetch_receipt()
        if self.company_id.l10n_no_mva_submit_mode == 'idporten':
            # Gjelder også 'submitting': fullføring krever nytt person-token
            # (kort levetid) — ny BankID-runde, idempotens-guardene i
            # _altinn_finish_submission tar resten.
            self._precheck_innsending(tillat_submitting=True)
            return self.env['l10n.no.eristo.idporten.service'] \
                .start_authorize_flow(
                    company=self.company_id,
                    target_record=self,
                    callback_method='_submit_med_idporten',
                )
        # 'submitting' = en innsending som venter på at instansen blir
        # operativ → fullfør den (systembruker-token).
        if self.state == 'submitting':
            return self._continue_submission()
        self._precheck_innsending()
        altinn_token = self._get_altinn_token()
        return self._utfor_innsending(altinn_token)

    def _submit_med_idporten(self, altinn_token=None, pid=None):
        """Callback fra /idporten/done etter BankID-innlogging.

        Kalles KUN av idporten-controlleren (l10n_no_eristo_idporten) med
        Altinn-PERSON-tokenet — privat (underscore) med vilje, så den
        ikke kan nås via RPC med vilkårlig token/pid (chatter-sporet er
        revisjonsbevis for hvem som sendte inn). Selve innsendings-
        sekvensen er identisk med systembruker-flyten.
        """
        self.ensure_one()
        if not altinn_token:
            raise UserError(_(
                "ID-porten-innloggingen ga ikke noe Altinn-token. "
                "Prøv 'Send inn' på nytt."
            ))
        if self.state == 'submitted':
            return self.action_fetch_receipt()
        self._precheck_innsending(tillat_submitting=True)
        if pid:
            # Sporbarhet (hvem sendte inn) uten fødselsnummer i klartekst.
            # Terskel 8: kortere identifikatorer maskeres helt — pid[-4:]
            # av en 4-tegns verdi ville vært hele verdien i klartekst.
            self.message_post(body=_(
                "MVA-melding sendt inn via ID-porten av innlogget person "
                "(fnr ***%(suffix)s).",
                suffix=pid[-4:] if len(pid) >= 8 else '****',
            ))
        return self._utfor_innsending(altinn_token)

    def _precheck_innsending(self, tillat_submitting=False):
        """Felles forhåndssjekker for begge innsendingsmoduser."""
        self.ensure_one()
        # Validering skjer i Altinn-appen ved process/next (409 + avvik), så
        # vi sender direkte fra 'generated'. 'avvist' tillates for retry etter
        # at bruker har rettet bilag og regenerert XML.
        tillatt = ('generated', 'avvist') + (
            ('submitting',) if tillat_submitting else ())
        if self.state not in tillatt:
            raise UserError(_(
                "Generer XML (status='generated') før innsending. "
                "Status: %(s)s.", s=self.state,
            ))
        if not (self.mvamelding_xml and self.konvolutt_xml):
            raise UserError(_("Mangler XML. Kjør 'Generer XML' først."))
        # R021 avviser meldingen (UGYLDIG_SKATTEMELDING) når en fradragskode
        # har motsatt fortegn uten merknad. Vi vet at den vil bli avvist, så
        # vi stopper her — ellers koster feilen brukeren en BankID-runde og
        # et avvisningsbrev fra Skatteetaten.
        mangler = self._r021_koder_uten_merknad()
        if mangler:
            # Skill de to årsakene. Legger brukeren inn merknaden ETTER at
            # XML-en ble generert, ligger den på recorden men ikke i
            # payloaden — da er «legg inn en merknad» misvisende, for det
            # har de nettopp gjort. Riktig råd er å regenerere.
            har_merknad = {
                m.mva_kode for m in self.merknad_ids
                if (m.beskrivelse or '').strip()
            }
            stale = [k for k in mangler if k in har_merknad]
            if stale:
                raise UserError(_(
                    "Merknaden for mva-kode %(koder)s er lagt inn, men "
                    "XML-en ble generert før den. Klikk 'Generer XML' på "
                    "nytt, så kommer merknaden med i innsendingen.",
                    koder=', '.join(sorted(set(stale), key=int)),
                ))
            raise UserError(_(
                "Mva-kode %(koder)s har motsatt fortegn (fradrag som netto "
                "øker avgiften) uten merknad. Skatteetaten avviser meldingen "
                "med regel R021 hvis den sendes slik.\n\n"
                "Legg til en merknad under fanen «Merknader» som forklarer "
                "hvorfor fortegnet er motsatt — typisk at terminen "
                "tilbakefører inngående merverdiavgift som ikke var "
                "fradragsberettiget, og at tilbakeføringen overstiger "
                "terminens egne fradrag.",
                koder=', '.join(sorted(set(mangler), key=int)),
            ))
        # Forhåndssjekk aktivering — ellers ville innsendingen feilet senere
        # med en kryptisk token-/Altinn-feil. Gi en klar, handlingsrettet
        # melding. Aktivering (systembruker-delegeringen) kreves i begge
        # moduser: kvittering hentes alltid med systembruker-token.
        if not self.company_id.l10n_no_mvamelding_aktivert:
            raise UserError(_(
                "Selskapet '%(name)s' er ikke aktivert for MVA-melding ennå.\n\n"
                "Bruk knappen 'Aktiver MVA-melding' (eller Innstillinger → "
                "Selskaper → %(name)s → Aktiver MVA-melding). Daglig leder må "
                "deretter godkjenne systemtilgangen i Altinn før innsending "
                "er mulig.", name=self.company_id.name,
            ))

    def _utfor_innsending(self, altinn_token):
        """Innsendingssekvensen — felles for person- og systembruker-token."""
        self.ensure_one()
        # Opprett instansen. Fang konvolutt-element-IDen (in-memory) for
        # put_konvolutt så vi slipper det lesende GET /instances.
        konvolutt_element = self.altinn_konvolutt_element_guid
        if not self.altinn_instance_guid:
            konvolutt_element = (
                self._altinn_create_instance(altinn_token) or konvolutt_element
            )

        # Last opp + fullfør. Hvis instansen ennå ikke er operativ (TT02
        # bruker ~60–120s) → marker 'submitting'. I systembruker-modus
        # fullfører cron-en automatisk; i idporten-modus må brukeren klikke
        # 'Send inn' igjen (person-tokenet kan ikke gjenskapes av cron).
        try:
            self._altinn_finish_submission(altinn_token, konvolutt_element)
        except AltinnNotReadyError:
            self.write({'state': 'submitting'})
            return self._notify_submitting()

        if self.state == 'avvist':
            return self._notify_avvist()
        return self._after_submit_fetch(altinn_token)

    def _continue_submission(self):
        """Fullfør en 'submitting'-innsending (fra retry-klikk eller cron).

        Kun systembruker-modus — i idporten-modus rutes retry-klikket via
        authorize-flyten i action_submit, og cron-en hopper over.
        """
        self.ensure_one()
        altinn_token = self._get_altinn_token()
        try:
            self._altinn_finish_submission(
                altinn_token, self.altinn_konvolutt_element_guid)
        except AltinnNotReadyError:
            return self._notify_submitting()
        if self.state == 'avvist':
            return self._notify_avvist()
        return self._after_submit_fetch(altinn_token)

    def _after_submit_fetch(self, altinn_token):
        """Rett etter vellykket innsending: forsøk ÉN kvittering-henting med en
        gang. Skatteetaten har ofte betalingsinformasjon klar i løpet av
        sekunder, så de fleste brukere ser beløp/frist/KID umiddelbart i stedet
        for å vente på cron-en. Feiler stille (savepoint) → status forblir
        'submitted' og cron-en henter senere.
        """
        self.ensure_one()
        received = False
        for delay in _RECEIPT_BACKOFF:
            if delay:
                time.sleep(delay)
            try:
                with self.env.cr.savepoint():
                    received = self._do_fetch_receipt(altinn_token)
            except Exception as e:  # noqa: BLE001
                _logger.info(
                    "Mvamelding %s: auto-kvittering-forsøk feilet (%s)",
                    self.id, str(e)[:200])
                received = False
            if received:
                break
        if received:
            # _do_fetch_receipt returnerer True for BÅDE godkjent og avvist —
            # verdiktet ligger i self.state. Ikke vis grønn suksess for en
            # melding Skatteetaten avviste.
            if self.state == 'avvist':
                return self._notify_avvist()
            return self._notify_received()
        return self._notify_submitted()

    def _get_altinn_token(self):
        self.ensure_one()
        eristo = self.env['l10n.no.eristo.service']
        mp_token = eristo.get_access_token(
            self.company_id, scope=SCOPE_INNSENDING)
        return self._exchange_to_altinn_token(mp_token)

    def _altinn_finish_submission(self, altinn_token, konvolutt_element=None):
        """Last opp konvolutt + mva-melding og kjør process/next (idempotent).

        Hvert steg hopper over om det allerede er gjort (guard på data-GUID),
        så metoden kan trygt re-kjøres av cron til instansen er operativ.
        """
        self.ensure_one()
        if not self.altinn_konvolutt_data_guid:
            self._altinn_put_konvolutt(altinn_token, konvolutt_element)
        if not self.altinn_mvamelding_data_guid:
            self._altinn_post_mvamelding(altinn_token)
        self._altinn_advance_process(altinn_token)

    def _notify_submitted(self):
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'success',
                'title': _("MVA-melding sendt inn"),
                'message': _(
                    "Mva-meldingen er sendt til Skatteetaten via Altinn og "
                    "venter på tilbakemelding. Status settes til «Mottatt» "
                    "automatisk når Skatteetaten har fastsatt meldingen og "
                    "kvitteringen er hentet."
                ),
                'sticky': True,
                'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
            },
        }

    def _notify_submitting(self):
        self.ensure_one()
        if self.company_id.l10n_no_mva_submit_mode == 'idporten':
            # Person-tokenet kan ikke gjenskapes av cron — brukeren må
            # selv fullføre med et nytt klikk (og ny BankID).
            message = _(
                "Altinn har ikke klargjort innsendingen ennå (tar vanligvis "
                "et par minutter). Klikk 'Send inn' igjen om litt — du får "
                "en ny ID-porten-innlogging, og innsendingen fortsetter der "
                "den slapp."
            )
        else:
            message = _(
                "Meldingen er mottatt og fullføres automatisk så snart "
                "Altinn har klargjort innsendingen (vanligvis innen et "
                "par minutter). Du trenger ikke gjøre noe mer — status "
                "oppdateres til 'Sendt inn' av seg selv."
            )
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'info',
                'title': _("Innsending startet"),
                'message': message,
                'sticky': True,
                'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
            },
        }

    def _notify_avvist(self):
        """Notifikasjon når Skatteetaten avviste meldingen (409 → avvist)."""
        self.ensure_one()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'danger',
                'title': _("Avvist av Skatteetaten"),
                'message': _(
                    "Mva-meldingen ble avvist (%(n)s avvik). Se fanen «Avvik» "
                    "for detaljer, rett bilagene og send inn på nytt.",
                    n=self.avvik_count if self.avvik_count > 0 else '?',
                ),
                'sticky': True,
                'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
            },
        }

    def _cron_complete_submissions(self):
        """Fullfør 'submitting'-innsendinger når instansen er blitt operativ.

        Kjøres hyppig (hvert minutt). Plukker meldinger som venter på at
        Altinn-instansen skal bli klar og prøver å laste opp + fullføre.
        AltinnNotReadyError → fortsatt ikke klar, prøv igjen neste tikk.
        """
        # Test-rammeverket forbyr commit/rollback på test-cursoren; i drift
        # vil vi ha commit per record så fullførte innsendinger overlever
        # en senere krasj i samme cron-kjøring.
        testing = odoo.modules.module.current_test
        pending = self.search([('state', '=', 'submitting')])
        for rec in pending:
            if rec.company_id.l10n_no_mva_submit_mode == 'idporten':
                # Person-innsending: cron-en har ikke (og skal ikke ha)
                # brukerens token — fullføring krever nytt 'Send inn'-klikk.
                # Å fullføre med systembruker-token ville dessuten gitt en
                # instans Skatteetaten ikke behandler. Logg synlig: records
                # kan stå her fra FØR modusbyttet (oppgradering), der
                # brukeren ble lovet automatisk fullføring.
                _logger.warning(
                    "Mvamelding %s står i 'submitting' i idporten-modus — "
                    "fullføres IKKE av cron; bruker må klikke 'Send inn' "
                    "(ny BankID).", rec.id)
                continue
            try:
                altinn_token = rec._get_altinn_token()
                rec._altinn_finish_submission(
                    altinn_token, rec.altinn_konvolutt_element_guid)
                if not testing:
                    rec.env.cr.commit()
            except AltinnNotReadyError:
                if not testing:
                    rec.env.cr.rollback()
                _logger.info(
                    "Mvamelding %s: instans ennå ikke klar, prøver neste tikk.",
                    rec.id)
            except Exception as e:  # noqa: BLE001
                if not testing:
                    rec.env.cr.rollback()
                _logger.warning(
                    "Mvamelding %s: feil ved auto-fullføring: %s",
                    rec.id, str(e)[:300])

    # ---------- token-exchange ----------

    def _exchange_to_altinn_token(self, maskinporten_token):
        """Bytt Maskinporten-token mot Altinn-token (samme som skattemelding)."""
        self.ensure_one()
        env_key = _resolve_env(self.company_id)
        platform = _ALTINN_PLATFORM_HOSTS[env_key]
        url = f'{platform}/authentication/api/v1/exchange/maskinporten'
        try:
            resp = requests.get(
                url,
                headers={'Authorization': f'Bearer {maskinporten_token}',
                         'Accept': 'application/jwt'},
                timeout=_HTTP_TIMEOUT,
            )
        except requests.RequestException as e:
            raise UserError(_(
                "Altinn ikke tilgjengelig: %(err)s", err=str(e)[:300],
            ))
        if resp.status_code >= 400:
            raise UserError(_(
                "Altinn token-exchange feilet (HTTP %(code)s).\n%(body)s\n\n"
                "Vanligste årsak: systembruker ikke godkjent for MVA-melding "
                "i Altinn — be daglig leder godkjenne onboarding-forespørselen.",
                code=resp.status_code, body=resp.text[:1000],
            ))
        token = resp.text.strip().strip('"')
        if not token or '.' not in token:
            raise UserError(_(
                "Altinn token-exchange returnerte uventet respons:\n%(body)s",
                body=resp.text[:300],
            ))
        return token

    # ---------- Altinn-steg ----------

    def _altinn_create_instance(self, altinn_token):
        """POST /instances — opprett tom mva-melding-instans."""
        self.ensure_one()
        company = self.company_id
        env_key = _resolve_env(company)
        app_host = _ALTINN_APP_HOSTS[env_key]
        app_id = _MVA_APP_ID[env_key]
        eristo = self.env['l10n.no.eristo.service']
        orgnr = eristo._orgnr(company)

        url = f'{app_host}/{app_id}/instances'
        payload = json.dumps({
            'instanceOwner': {'organisationNumber': orgnr},
            'appId': app_id,
        }).encode('utf-8')
        try:
            resp = requests.post(
                url, data=payload,
                headers={'Authorization': f'Bearer {altinn_token}',
                         'Content-Type': 'application/json',
                         'Accept': 'application/json'},
                timeout=_HTTP_TIMEOUT,
            )
        except requests.RequestException as e:
            raise UserError(_(
                "Altinn ikke tilgjengelig: %(err)s", err=str(e)[:300],
            ))
        if resp.status_code >= 400:
            _logger.error("Altinn create_instance HTTP %s: %s",
                          resp.status_code, resp.text[:2000])
            raise UserError(_(
                "Kunne ikke opprette Altinn-instans (HTTP %(code)s).\n%(body)s",
                code=resp.status_code, body=resp.text[:1000],
            ))
        try:
            data = json.loads(resp.text)
        except json.JSONDecodeError:
            raise UserError(_(
                "Altinn returnerte ikke gyldig JSON ved instance-oppretting:\n"
                "%(body)s", body=resp.text[:1000],
            ))
        instance_id = data.get('id') or ''
        if '/' not in instance_id:
            raise UserError(_(
                "Altinn returnerte instans uten gyldig id-format:\n%(body)s",
                body=resp.text[:1000],
            ))
        party_id, guid = instance_id.split('/', 1)
        # Fang konvolutt-element-IDen fra OPPRETT-svaret. Altinn pre-oppretter
        # det (tomme) konvolutt-data-elementet ved instansiering og inkluderer
        # det i 'data'-arrayet her — så vi slipper et separat GET /instances
        # (som race-er 404 i ~60s på TT02 og tvinger bruker til å klikke igjen).
        konvolutt_element_guid = next(
            (d.get('id') for d in (data.get('data') or [])
             if d.get('dataType') == _KONVOLUTT_DATATYPE),
            None,
        )
        self._persist_altinn_instance(party_id, guid, konvolutt_element_guid)
        _logger.info(
            "Mvamelding %s: opprettet Altinn-instans %s/%s (konvolutt-element %s)",
            self.id, party_id, guid, konvolutt_element_guid or 'ikke i svar',
        )
        # Returner element-IDen så action_submit kan gi den direkte (in-memory)
        # til put_konvolutt i SAMME kall — re-lesing fra DB via separat-cursor
        # er ikke pålitelig innen samme transaksjon.
        return konvolutt_element_guid

    def _altinn_instance_base(self):
        self.ensure_one()
        env_key = _resolve_env(self.company_id)
        app_host = _ALTINN_APP_HOSTS[env_key]
        app_id = _MVA_APP_ID[env_key]
        return (f'{app_host}/{app_id}/instances/'
                f'{self.altinn_instance_owner_party_id}/'
                f'{self.altinn_instance_guid}')

    def _altinn_get_instance(self, altinn_token):
        """GET instans-metadata (med backoff for race-condition)."""
        self.ensure_one()
        base = self._altinn_instance_base()
        last = None
        for delay in _BACKOFF:
            if delay:
                time.sleep(delay)
            try:
                resp = requests.get(
                    base,
                    headers={'Authorization': f'Bearer {altinn_token}',
                             'Accept': 'application/json'},
                    timeout=_HTTP_TIMEOUT,
                )
            except requests.RequestException as e:
                last = str(e)
                continue
            if resp.status_code < 400:
                try:
                    return resp.json()
                except json.JSONDecodeError:
                    last = 'ikke-JSON respons'
                    continue
            last = f"HTTP {resp.status_code}: {resp.text[:200]}"
            if resp.status_code != 404:
                break
        # Etter ~65s fortsatt 404 → Altinns kjente race etter create_instance.
        # Instansen er persistert; fullfør asynkront via cron.
        raise AltinnNotReadyError(_(
            "Altinn-instansen er ennå ikke klar (GET 404). Siste: %(err)s",
            err=last or 'ukjent',
        ))

    def _altinn_put_konvolutt(self, altinn_token, konvolutt_element_guid=None):
        """PUT konvolutt-XML til det forhånds-opprettede data-elementet.

        Bruker konvolutt-element-IDen fra opprett-svaret når den finnes (gitt
        direkte in-memory fra action_submit, eller fra persistert felt ved
        retry) → ingen GET, ingen 404-race. Faller tilbake til et GET
        /instances kun hvis opprett-svaret manglet data-arrayet.
        """
        self.ensure_one()
        data_guid = konvolutt_element_guid or self.altinn_konvolutt_element_guid
        if not data_guid:
            instance = self._altinn_get_instance(altinn_token)
            data_guid = next(
                (d.get('id') for d in (instance.get('data') or [])
                 if d.get('dataType') == _KONVOLUTT_DATATYPE),
                None,
            )
        if not data_guid:
            raise UserError(_(
                "Fant ikke forhånds-opprettet konvolutt-data-element "
                "(dataType=%(dt)s) i Altinn-instansen.",
                dt=_KONVOLUTT_DATATYPE,
            ))
        url = f'{self._altinn_instance_base()}/data/{data_guid}'
        resp = self._altinn_data_call(
            'put', url, altinn_token, self.konvolutt_xml,
            content_type='application/xml',
            filename=f'mvaMeldingInnsending-{self.aar}-{self.periode}.xml',
        )
        self.altinn_konvolutt_data_guid = data_guid
        _logger.info("Mvamelding %s: PUT konvolutt OK (%s)", self.id, data_guid)
        return resp

    def _altinn_post_mvamelding(self, altinn_token):
        """POST mva-melding-XML som eget data-element (dataType=mvamelding)."""
        self.ensure_one()
        url = (f'{self._altinn_instance_base()}'
               f'/data?dataType={_MVAMELDING_DATATYPE}')
        resp = self._altinn_data_call(
            'post', url, altinn_token, self.mvamelding_xml,
            content_type='text/xml',
            filename=f'mvaMelding-{self.aar}-{self.periode}.xml',
        )
        try:
            data = json.loads(resp)
        except json.JSONDecodeError:
            raise UserError(_(
                "Altinn returnerte ikke gyldig JSON ved mva-melding-upload:\n"
                "%(body)s", body=resp[:1000],
            ))
        data_guid = data.get('id')
        if not data_guid:
            raise UserError(_(
                "Altinn mva-melding-upload returnerte respons uten data-id:\n"
                "%(body)s", body=resp[:1000],
            ))
        self.altinn_mvamelding_data_guid = data_guid
        _logger.info("Mvamelding %s: POST mva-melding OK (%s)",
                     self.id, data_guid)

    def _altinn_data_call(self, method, url, altinn_token, xml_body,
                          content_type, filename):
        """Felles PUT/POST av et XML data-element, med 404-backoff."""
        self.ensure_one()
        headers = {
            'Authorization': f'Bearer {altinn_token}',
            'Content-Type': content_type,
            'Content-Disposition': f'attachment; filename="{filename}"',
            'Accept': 'application/json',
        }
        body = xml_body.encode('utf-8')
        func = getattr(requests, method)
        last = None
        for delay in _BACKOFF:
            if delay:
                time.sleep(delay)
            try:
                resp = func(url, data=body, headers=headers,
                            timeout=_HTTP_TIMEOUT)
            except requests.RequestException as e:
                raise UserError(_(
                    "Altinn ikke tilgjengelig: %(err)s", err=str(e)[:300],
                ))
            if resp.status_code < 400:
                return resp.text
            last = (resp.status_code, resp.text[:2000])
            _logger.warning("Altinn %s %s: HTTP %s — %s",
                            method.upper(), url, resp.status_code,
                            resp.text[:200])
            if resp.status_code != 404:
                break
        code, err = last or (0, '')
        if code == 404:
            # Instansen er ennå ikke operativ → fullfør asynkront (cron).
            raise AltinnNotReadyError(_(
                "Altinn-instansen er ennå ikke klar (HTTP 404 ved %(m)s data).",
                m=method.upper(),
            ))
        raise UserError(_(
            "Altinn %(m)s data feilet (HTTP %(code)s).\n%(body)s",
            m=method.upper(), code=code, body=err[:1000],
        ))

    def _altinn_advance_process(self, altinn_token):
        """PUT process/next til innsending er fullført (helautomatisk).

        MVA-appen har to bruker-steg (utfylling → bekreftelse) som vi
        fullfører selv, deretter går instansen til tilbakemelding/feedback
        (Skatteetatens backend). 409 = valideringsfeil ved fullføring av
        utfyllingssteget → vi parser valideringsresultat og setter 'avvist'.
        """
        self.ensure_one()
        base = self._altinn_instance_base()
        url = f'{base}/process/next'
        last_task = self.altinn_process_current_task
        for iteration in range(_MAX_PROCESS_ITERATIONS):
            try:
                resp = requests.put(
                    url, data=b'',
                    headers={'Authorization': f'Bearer {altinn_token}',
                             'Content-Type': 'application/json',
                             'Accept': 'application/json'},
                    timeout=_HTTP_TIMEOUT,
                )
            except requests.RequestException as e:
                raise UserError(_(
                    "Altinn ikke tilgjengelig: %(err)s", err=str(e)[:300],
                ))
            if resp.status_code == 409:
                # Kryss-validering ved fullføring av utfylling feilet.
                self._handle_process_409(resp.text)
                return
            if resp.status_code == 404:
                # Instansen er ennå ikke operativ → fullfør asynkront (cron).
                raise AltinnNotReadyError(_(
                    "Altinn-instansen er ennå ikke klar (process/next 404)."))
            if resp.status_code >= 400:
                _logger.error("Altinn process/next HTTP %s (iter %d): %s",
                              resp.status_code, iteration, resp.text[:2000])
                raise UserError(_(
                    "Process/next feilet (HTTP %(code)s, iter %(i)d).\n%(body)s",
                    code=resp.status_code, i=iteration, body=resp.text[:800],
                ))
            # En tom/ikke-JSON 2xx-respons er IKKE bevis på fullføring — vi
            # nekter å markere en skatteinnsending som fullført uten en
            # tolkbar prosess-tilstand.
            try:
                data = json.loads(resp.text) if resp.text else None
            except json.JSONDecodeError:
                data = None
            if not isinstance(data, dict):
                raise UserError(_(
                    "Process/next ga uventet (ikke-JSON / tom) respons "
                    "(HTTP %(code)s) — kan ikke bekrefte at innsendingen "
                    "er fullført:\n%(body)s",
                    code=resp.status_code, body=(resp.text or '')[:500],
                ))
            current = (data.get('currentTask') or {})
            current_task = current.get('elementId')
            current_type = (current.get('altinnTaskType')
                            or current.get('taskType'))
            ended_iso = data.get('ended')
            feedback = current_type == 'feedback'
            # Positivt fullførings-signal: prosess endt ELLER nådd feedback-
            # tasken (Skatteetatens backend tar over). currentTask=None uten
            # disse er tvetydig — vi logger og antar fullført (sjelden), men
            # foretrekker de positive signalene.
            if ended_iso or feedback:
                self.write({
                    'state': 'submitted',
                    'submitted_at': fields.Datetime.now(),
                    'altinn_process_current_task': current_task or 'EndEvent',
                })
                _logger.info("Mvamelding %s: innsending fullført etter %d "
                             "iterasjon(er) (ended=%s, feedback=%s)",
                             self.id, iteration + 1, bool(ended_iso), feedback)
                return
            if current_task is None:
                _logger.warning(
                    "Mvamelding %s: process/next ga ingen currentTask men "
                    "verken 'ended' eller feedback — antar fullført. data=%s",
                    self.id, json.dumps(data)[:300])
                self.write({
                    'state': 'submitted',
                    'submitted_at': fields.Datetime.now(),
                    'altinn_process_current_task': 'EndEvent',
                })
                return
            if current_task and current_task != last_task:
                self.altinn_process_current_task = current_task
                last_task = current_task
        raise UserError(_(
            "Process/next nådde max iterasjoner (%(n)d) uten å fullføre. "
            "Siste task: %(t)s. Sjekk Altinn-instansen manuelt.",
            n=_MAX_PROCESS_ITERATIONS, t=last_task,
        ))

    def _handle_process_409(self, body):
        """Parse valideringsresultat fra 409 og sett 'avvist'."""
        self.ensure_one()
        # avvik_count=-1 = "kunne ikke tolke avvik" (skiller fra 0 = ingen).
        summary, avvik_count, human = ('ugyldig mva-melding', -1, body[:1000])
        try:
            root = etree.fromstring(body.encode('utf-8'))
            actual_ns = etree.QName(root).namespace
            if actual_ns == _NS_VALIDERING:
                summary, avvik_count, human = \
                    self._parse_valideringsresultat(body)
            else:
                _logger.warning(
                    "Mvamelding %s: 409-body har uventet namespace %s "
                    "(forventet %s) — kan ikke tolke avvik strukturert.",
                    self.id, actual_ns, _NS_VALIDERING)
        except etree.XMLSyntaxError as e:
            _logger.warning(
                "Mvamelding %s: 409-body er ikke gyldig XML: %s",
                self.id, str(e)[:200])
        self.write({
            'state': 'avvist',
            'valideringsresultat_xml': body,
            'avvik_count': avvik_count,
            'last_response': human,
        })
        self.message_post(body=_(
            "✗ Skatteetaten avviste mva-meldingen ved innsending "
            "(%(s)s, %(n)d avvik):\n%(h)s",
            s=summary, n=avvik_count, h=human,
        ))
        raise UserError(_(
            "Skatteetaten avviste mva-meldingen ved fullføring "
            "(%(n)d avvik):\n%(h)s", n=avvik_count, h=human[:800],
        ))

    # ---------- idempotency ----------

    def _persist_altinn_instance(self, party_id, instance_guid,
                                 konvolutt_element_guid=None):
        """Lagre instance-/konvolutt-GUID på recorden (vanlig self.write).

        IKKE separat cursor: i native-hub-flyten opprettes MVA-meldingen i
        SAMME transaksjon som innsendingen, og en separat cursor ville ikke
        sett den ennå-ucommittede recorden (UPDATE ... WHERE id = ny → 0 rader
        → GUID tapt → cron kunne ikke fullføre). Med den asynkrone modellen
        (404 → 'submitting', ingen rollback) committer self.write sammen med
        transaksjonen og virker for både ny-i-txn (native-hub) og eksisterende
        (standalone) records. Ved en hard feil rulles GUID tilbake samtidig med
        en ev. ny MVA-melding — ren tilstand, ingen foreldreløs Altinn-referanse
        å gjenbruke.
        """
        self.ensure_one()
        self.write({
            'altinn_instance_owner_party_id': party_id,
            'altinn_instance_guid': instance_guid,
            'altinn_konvolutt_element_guid': konvolutt_element_guid,
        })
