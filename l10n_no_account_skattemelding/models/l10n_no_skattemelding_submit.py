"""Altinn 3 innsendings-flyt for skattemelding upersonlig.

Etter at validertest har returnert 0 avvik (state='validated') sender vi
skattemeldingen til Skatteetatens Altinn 3-app `skd/formueinntekt-
skattemelding-v2`. Flow:

  1. Maskinporten-token (via Eristo Token Service) for scope
     skatteetaten:formueinntekt/skattemelding
  2. Exchange → Altinn-token via /authentication/api/v1/exchange/maskinporten
  3. POST /instances → opprett instans m. partyId + appId + inntektsaar
  4. POST /instances/{partyId}/{guid}/data?dataType=skattemeldingUpersonlig
     med konvolutt-XML som body
  5. PUT /instances/{partyId}/{guid}/process/next → flytt instans gjennom
     prosess-stegene (Task_1 → Task_2 → EndEvent). Vi kaller next så lenge
     prosessen ikke er endet — typisk 2 PUT-er totalt.
  6. Senere: poll GET /instances/{partyId}/{guid} for mottakskvittering

Spec: github.com/Skatteetaten/skattemeldingen/blob/master/docs/api-v2/README.md
Altinn-spec: docs.altinn.studio/api/apps/data-api

Auth-headers:
  - POST /instances + data: Authorization: Bearer <Altinn-token>
  - GET /instances/...: Authorization: Bearer <Altinn-token>

Idempotency: Vi setter `dataValues.inntektsaar` ved oppretting — Skatteetaten
deduplicerer ikke selv på dette, så hvis et POST /instances feiler etter at
instansen er opprettet (men før vi fikk lagret guid), kan kunden ende opp
med duplikat-instans. Vi lagrer derfor altinn_instance_guid i egen cursor
slik at vi finner igjen partial-success-instanser ved retry.
"""
import base64
import hashlib
import json
import logging
import time

import requests

from odoo import _, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Altinn platform-hosts (authentication, token-exchange)
_ALTINN_PLATFORM_HOSTS = {
    'test': 'https://platform.tt02.altinn.no',
    'prod': 'https://platform.altinn.no',
}

# Skatteetatens app-hosts (skd-orgnr 974761076 publiserer apps her)
_ALTINN_APP_HOSTS = {
    'test': 'https://skd.apps.tt02.altinn.no',
    'prod': 'https://skd.apps.altinn.no',
}

# AppId per Skatteetatens spec for skattemelding upersonlig (AS)
_SKATTEMELDING_APP_ID = 'skd/formueinntekt-skattemelding-v2'
# DataType-navn for Altinn-appens interne form-state (P2-E fra code-review:
# en-felt-rename om Altinn endrer schema-versjon i v3).
_SKATTEMELDINGSAPP_DATATYPE = 'Skattemeldingsapp_v2'

# DataType for konvolutt-uploads i app-en — verifisert mot
# /skd/formueinntekt-skattemelding-v2/api/v1/applicationmetadata 2026-05-11:
#   ['Skattemeldingsapp_v2', 'ref-data-as-pdf',
#    'skattemeldingOgNaeringsspesifikasjon', 'skattemelding-vedlegg',
#    'revisor-bekreftelse', 'revisor-vedlegg', 'tilbakemelding']
# Konvolutten vi POSTer er den indre skattemelding+naeringsspes-payloaden,
# så dataType=skattemeldingOgNaeringsspesifikasjon.
_DATATYPE_KONVOLUTT = 'skattemeldingOgNaeringsspesifikasjon'

_SKATTEMELDING_SCOPE = 'skatteetaten:formueinntekt/skattemelding'

# Timeouts — Altinn er litt tregere enn Skatteetaten på instance-oppretting
_HTTP_TIMEOUT = 60

# Hvor mange PUT process/next-iterasjoner vi tillater før vi gir opp.
# Empirisk: Skatteetatens app har 2 task-steg (Task_1 → Task_2 → EndEvent)
# så 3 iterasjoner gir en buffer hvis prosess-modellen endres senere.
_MAX_PROCESS_ITERATIONS = 5


def _resolve_altinn_platform(company):
    env_key = company.l10n_no_eristo_environment or 'test'
    if env_key not in _ALTINN_PLATFORM_HOSTS:
        raise UserError(_(
            "Ukjent Altinn-miljø '%(env)s' for selskap %(name)s.",
            env=env_key, name=company.name,
        ))
    return _ALTINN_PLATFORM_HOSTS[env_key]


def _resolve_altinn_app(company):
    env_key = company.l10n_no_eristo_environment or 'test'
    if env_key not in _ALTINN_APP_HOSTS:
        raise UserError(_(
            "Ukjent Altinn-app-miljø '%(env)s' for selskap %(name)s.",
            env=env_key, name=company.name,
        ))
    return _ALTINN_APP_HOSTS[env_key]


class L10nNoSkattemelding(models.Model):
    _inherit = 'l10n.no.skattemelding'

    # Innsendings-spor (utvider feltene definert i l10n_no_skattemelding.py)
    submitted_at = fields.Datetime(
        string="Sendt inn",
        copy=False,
        help="Tidspunkt for PUT process/next som flyttet instansen "
             "til EndEvent (Skatteetaten har akseptert mottak).",
    )
    mottatt_at = fields.Datetime(
        string="Kvittering mottatt",
        copy=False,
        help="Tidspunkt for når mottakskvittering ble hentet fra Altinn.",
    )
    altinn_process_current_task = fields.Char(
        string="Altinn-task",
        copy=False,
        help="Current process step i Altinn-instansen (Task_1, "
             "Task_2, EndEvent). Oppdateres ved hver PUT process/next.",
    )
    kvittering_archived_at = fields.Datetime(
        string="Kvittering arkivert",
        copy=False,
        help="Tidspunkt for når kvittering ble arkivert som ir.attachment "
             "+ chatter-post. Brukes som idempotency-vakt slik at gjentatte "
             "cron-tikk ikke skaper duplikate chatter-poster eller vedlegg "
             "(P1 #5).",
    )

    # ---------- Token-exchange ----------

    def _exchange_to_altinn_token(self, maskinporten_token):
        """Bytt Maskinporten-token mot Altinn-token.

        Altinn aksepterer ikke Maskinporten-tokens direkte på app-endpoints —
        de må byttes via platform/authentication/api/v1/exchange/maskinporten
        som returnerer en JWT signert av Altinn for bruk mot apps.

        Spec: docs.altinn.studio/authentication/maskinporten-token-exchange
        """
        self.ensure_one()
        platform = _resolve_altinn_platform(self.company_id)
        url = f'{platform}/authentication/api/v1/exchange/maskinporten'
        try:
            resp = requests.get(
                url,
                headers={
                    'Authorization': f'Bearer {maskinporten_token}',
                    # Altinn returnerer Altinn-tokenet som plain text i body
                    # (ikke JSON). Accept-header gjør ikke noe — vi leser body
                    # som-er.
                    'Accept': 'application/jwt',
                },
                timeout=_HTTP_TIMEOUT,
            )
        except requests.RequestException as e:
            raise UserError(_(
                "Altinn ikke tilgjengelig: %(err)s", err=str(e)[:300],
            ))
        if resp.status_code >= 400:
            raise UserError(_(
                "Altinn token-exchange feilet (HTTP %(code)s).\n%(body)s\n\n"
                "Vanligste årsaker: (1) Maskinporten-tokenet mangler "
                "authorization_details for korrekt systembruker — sjekk at "
                "kunden har godkjent skattemelding-onboarding i Altinn-"
                "portalen. (2) Selskapets organisasjon er ikke registrert "
                "i Altinn (alle norske AS er det automatisk).",
                code=resp.status_code, body=resp.text[:1000],
            ))
        body = resp.text.strip()

        # Altinn returnerer JWT som plain text. Vi stripper " hvis det skulle
        # være quotet (noen Altinn-miljøer returnerte tidligere "JWT" med
        # quotes — defensiv parsing).
        token = body.strip().strip('"')
        if not token or '.' not in token:
            raise UserError(_(
                "Altinn token-exchange returnerte uventet respons:\n%(body)s",
                body=body[:300],
            ))
        return token

    # ---------- Diagnose: test forbindelse ----------

    def action_l10n_no_skattemelding_test_connection(self):
        """Verifiser at hele auth-kjeden virker uten å sende inn noe.

        Kjører tre sjekker:
          1. Hent Maskinporten-token via Eristo Token Service med
             skattemelding-scope.
          2. Bytt mot Altinn-token via /authentication/exchange.
          3. Test at Skatteetaten-API svarer (GET hentGjeldende).

        Returnerer en notification med detaljert status. Account-manager
        kan kjøre denne FØR de prøver å generere XML for å avdekke
        konfigurasjonsfeil tidlig (manglende scope, ikke-godkjent
        systembruker, manglende partsnummer-tilgang, etc.).
        """
        self.ensure_one()
        company = self.company_id
        eristo = self.env['l10n.no.eristo.service']

        # Hjelpe-helper: bygg melding med liste-format
        def _format_result(steps, ok):
            lines = []
            for label, status, detail in steps:
                marker = '✓' if status == 'ok' else ('⚠' if status == 'warn' else '✗')
                lines.append(f"{marker} {label}")
                if detail:
                    lines.append(f"   → {detail}")
            return '\n'.join(lines)

        steps = []

        # Steg 1: Maskinporten-token
        try:
            mp_token = eristo.get_access_token(
                company, scope=_SKATTEMELDING_SCOPE,
            )
            steps.append((
                'Maskinporten-token', 'ok',
                _("Token hentet via Eristo Token Service "
                  "(scope=%(s)s)", s=_SKATTEMELDING_SCOPE),
            ))
        except Exception as e:
            # Wide-catch er bevisst (P2 #4): test-forbindelse skal
            # rapportere ENHVER feil med en brukervennlig melding,
            # ikke crashe. exc_info=True for stack trace i log.
            _logger.warning(
                "Test forbindelse: maskinporten-token feilet: %s",
                str(e)[:200], exc_info=True,
            )
            steps.append((
                'Maskinporten-token', 'fail',
                _("Kunne ikke hente token: %(err)s\n"
                  "Sjekk at selskapet har aktivert Skattemelding-tjenesten "
                  "(Selskap → Skattemelding-fane → 'Aktiver Skattemelding').",
                  err=str(e)[:200]),
            ))
            return self._notify_test_result(steps, ok=False)

        # Steg 2: Altinn token-exchange
        try:
            altinn_token = self._exchange_to_altinn_token(mp_token)
            steps.append((
                'Altinn token-exchange', 'ok',
                _("Maskinporten-token vekslet til Altinn-token (JWT)"),
            ))
        except Exception as e:
            # Wide-catch er bevisst (P2 #4) — se kommentaren over.
            _logger.warning(
                "Test forbindelse: altinn token-exchange feilet: %s",
                str(e)[:300], exc_info=True,
            )
            steps.append((
                'Altinn token-exchange', 'fail',
                _("Kunne ikke veksle til Altinn-token: %(err)s\n"
                  "Vanligste årsak: systembruker er ikke godkjent for "
                  "Skattemelding i Altinn — be daglig leder logge inn på "
                  "altinn.no og godkjenne forespørselen.",
                  err=str(e)[:300]),
            ))
            return self._notify_test_result(steps, ok=False)

        # Steg 3: Skatteetaten API svar
        try:
            from . import l10n_no_skattemelding_validate as _v
            host = _v._resolve_host(company)
            orgnr = eristo._orgnr(company)
            url = (f"{host}{_v._SKATTEMELDING_API_BASE}/"
                   f"{self.inntektsaar}/{orgnr}")
            resp = requests.get(
                url,
                headers={
                    'Accept': 'application/xml',
                    'Authorization': f'Bearer {mp_token}',
                },
                timeout=_HTTP_TIMEOUT,
            )
            if resp.status_code < 400:
                steps.append((
                    'Skatteetaten-API', 'ok',
                    _("API svarer og utkast for %(aar)d er tilgjengelig.",
                      aar=self.inntektsaar),
                ))
            else:
                # Fang opp full body for diagnose. Skatteetaten emitter
                # JSON-feiltype + melding ved 403/4xx — vi vil se det.
                err_body = resp.text[:600] if resp.text else '(tom body)'
                # Logg orgnr + token-claims (kun len/hash, ikke selve token)
                # for ekstra kontekst
                token_hash = hashlib.sha256(mp_token.encode()).hexdigest()[:12]
                # Parse JWT-payload (uten signatur-validering — kun for diagnose)
                jwt_claims_preview = ''
                try:
                    parts = mp_token.split('.')
                    if len(parts) >= 2:
                        # JWT-base64url har ikke padding
                        payload_b64 = parts[1] + '=' * (4 - len(parts[1]) % 4)
                        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
                        # Vis kun de mest relevante claims
                        relevant = {k: claims.get(k) for k in [
                            'iss', 'aud', 'scope', 'consumer',
                            'authorization_details', 'client_id',
                            'client_orgno', 'exp',
                        ] if k in claims}
                        jwt_claims_preview = json.dumps(
                            relevant, ensure_ascii=False)[:500]
                except (ValueError, json.JSONDecodeError, TypeError,
                        UnicodeDecodeError):
                    # JWT-parsing er kun for diagnose — failures her er
                    # ikke kritiske, sluker stille slik at vi kan vise
                    # HTTP-feilen til brukeren.
                    pass
                # Kort brukervennlig melding + full diagnostikk lagres i
                # last_response (synlig i Validering-fanen for support).
                # Bruker ser ikke JWT-claims/URL/token-hash i notification.
                short_msg = self._short_skatteetaten_error_msg(
                    resp.status_code, err_body)
                full_diag = (
                    f"=== Test forbindelse — Skatteetaten 403/4xx ===\n"
                    f"HTTP-kode: {resp.status_code}\n"
                    f"URL: {url}\n"
                    f"Orgnr: {orgnr} | Inntektsår: {self.inntektsaar}\n"
                    f"Token-hash (SHA-256, 12 første tegn): {token_hash}\n"
                    f"JWT-claims (relevante):\n{jwt_claims_preview}\n"
                    f"Respons-body:\n{err_body}"
                )
                # Lagre full diagnose i last_response så support kan se den
                self.write({'last_response': full_diag})
                if resp.status_code == 404:
                    steps.append((
                        'Skatteetaten-API', 'warn',
                        _("Utkast for %(aar)d eksisterer ikke ennå (404). "
                          "Dette er normalt før Skatteetaten har publisert "
                          "utkastet for inntektsåret. Tekniske detaljer "
                          "lagret i Validering-fanen.",
                          aar=self.inntektsaar),
                    ))
                else:
                    steps.append((
                        'Skatteetaten-API', 'fail',
                        short_msg + _("\n\nTekniske detaljer lagret i "
                                      "Validering-fanen for support."),
                    ))
                    return self._notify_test_result(steps, ok=False)
        except Exception as e:
            # Test forbindelse er en diagnostisk operasjon — vi vil
            # produsere en brukervennlig feilmelding for hvilken som
            # helst exception type (P2 #4). exc_info=True for stack
            # trace i log.
            _logger.warning(
                "Test forbindelse: uventet feil under API-test: %s",
                str(e)[:200], exc_info=True,
            )
            steps.append((
                'Skatteetaten-API', 'fail',
                _("Feil under API-test: %(err)s", err=str(e)[:200]),
            ))
            return self._notify_test_result(steps, ok=False)

        return self._notify_test_result(steps, ok=True)

    def _short_skatteetaten_error_msg(self, code, body):
        """Bygg en kort brukervennlig feilmelding fra Skatteetaten-respons.

        Skatteetaten returnerer typisk en kort feilkode-streng i body
        (f.eks. 'Skatteplikt ikke registrert'). Vi viser den, ikke
        de tekniske detaljene.
        """
        body_stripped = (body or '').strip()
        if code == 403:
            if 'Skatteplikt ikke registrert' in body_stripped:
                return _(
                    "Skatteetaten har ikke registrert skatteplikt for "
                    "selskapet for dette inntektsåret. "
                    "Mulige årsaker:\n"
                    "  • Selskapet er ikke ennå registrert som skattepliktig\n"
                    "  • Inntektsåret er ikke åpnet i Skatteetatens system\n"
                    "  • For testmiljø: orgnr er ikke på Tenor-testdata-listen"
                )
            return _("Skatteetaten avviste forespørselen (HTTP 403). "
                     "Kontakt support.")
        if code == 401:
            return _("Autentisering avvist (HTTP 401). Token er ikke gyldig "
                     "for skatteetaten:formueinntekt/skattemelding-scope.")
        if code == 404:
            return _("Skatteetaten har ikke utkast publisert for dette "
                     "inntektsåret ennå (HTTP 404).")
        if code == 500:
            return _("Skatteetaten har en intern feil (HTTP 500). "
                     "Prøv igjen om noen minutter.")
        return _("HTTP %(c)s fra Skatteetaten — uventet feil.", c=code)

    def _notify_test_result(self, steps, ok):
        marker = '✓' if ok else '✗'
        lines = []
        for label, status, detail in steps:
            m = '✓' if status == 'ok' else ('⚠' if status == 'warn' else '✗')
            lines.append(f"{m} {label}")
            if detail:
                lines.append(f"   → {detail}")
        msg = '\n'.join(lines)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'success' if ok else 'warning',
                'title': (
                    _("%(m)s Forbindelse OK — klar for innsending", m=marker)
                    if ok else
                    _("%(m)s Forbindelse-test feilet", m=marker)
                ),
                'message': msg,
                'sticky': True,
            },
        }

    # ---------- Submit-action ----------

    def action_l10n_no_skattemelding_open_submit_wizard(self):
        """Åpne bekreftelse-wizard før submit.

        Erstatter den statiske confirm="..."-attributtet på knappen med
        en dynamisk dialog som viser kontekst (selskap, år, veiledninger).
        """
        self.ensure_one()
        if self.state not in ('validated', 'uploaded'):
            raise UserError(_(
                "Kan kun sende skattemeldinger som er validert. "
                "Nåværende status: %(s)s.", s=self.state,
            ))
        wizard = self.env['l10n.no.skattemelding.submit.confirm'].create({
            'skattemelding_id': self.id,
        })
        return {
            'type': 'ir.actions.act_window',
            'name': _("Bekreft innsending"),
            'res_model': 'l10n.no.skattemelding.submit.confirm',
            'view_mode': 'form',
            'res_id': wizard.id,
            'target': 'new',
        }

    def action_l10n_no_skattemelding_submit(self):
        """Send skattemelding-utkast til Altinn for bruker-signering.

        Skatteetaten krever 'spor til utførende' (PID/fnr) — at en fysisk
        person har signert. Bransjestandard (Visma, Tripletex, Maestro) er
        en HYBRID flyt:

          1. Vi (system): bygger XML, oppretter Altinn-instans, laster opp
             som UTKAST via Maskinporten + systembruker.
          2. Bruker: går til altinn.no, logger inn med BankID, sjekker
             utkastet, klikker "Send inn" → Altinn registrerer bruker-fnr
             som 'utfører' og videresender til Skatteetaten.
          3. Vi (system): poller Altinn for status. Når brukeren har
             signert oppdaterer vi state til 'submitted'. Når Skatteetaten
             har bekreftet → 'mottatt'.

        Vi gjør IKKE process/next selv — det er signering-handlingen som
        bruker MÅ gjøre i Altinn-portalen.

        Krever state='validated' eller 'uploaded'.
        """
        self.ensure_one()
        if self.state not in ('validated', 'uploaded'):
            raise UserError(_(
                "Kan kun sende skattemeldinger som er validert (status='validated' "
                "eller 'uploaded'). Nåværende status: %(s)s. Kjør "
                "'Valider mot Skatteetaten' først.", s=self.state,
            ))
        if not self.konvolutt_xml:
            raise UserError(_(
                "Konvolutt-XML mangler. Bygg XML og valider på nytt før innsending."
            ))
        if not self.partsnummer:
            raise UserError(_(
                "Partsnummer mangler — kjør 'Hent partsnummer' først."
            ))

        # Standard Maskinporten + systembruker (gjenbruker eksisterende
        # flyt for create-instance og upload-data). Bytte til
        # ID-porten-token er IKKE nødvendig her — Maskinporten kan
        # opprette utkast som bruker senere signerer i Altinn-portalen.
        company = self.company_id
        eristo = self.env['l10n.no.eristo.service']
        maskinporten_token = eristo.get_access_token(
            company, scope=_SKATTEMELDING_SCOPE,
        )
        altinn_token = self._exchange_to_altinn_token(maskinporten_token)

        # Steg 1: opprett instans (eller gjenbruk eksisterende)
        if not self.altinn_instance_guid:
            self._altinn_create_instance(altinn_token)
        else:
            _logger.info(
                "Skattemelding %s: gjenbruker eksisterende Altinn-instans %s/%s",
                self.id, self.altinn_instance_owner_party_id,
                self.altinn_instance_guid,
            )

        # Steg 1b: ALLTID forsøk å oppdatere Skattemeldingsapp_v2 med
        # korrekt inntektsår — også på retry. Tidligere lå denne inne i
        # _altinn_create_instance, slik at retry (med eksisterende
        # instance_guid) hoppet over PUT-en og Skattemeldingsapp_v2
        # forble på default inntektsaar=0. Bugrapport 2026-05-18:
        # observert på alle tre prod-innsendinger (Gamify, Roret, InGrid).
        self._ensure_skattemeldingsapp_updated(altinn_token)

        # Steg 2: last opp konvolutt-data hvis vi ikke allerede har gjort det
        if not self.altinn_data_guid:
            self._altinn_upload_data(altinn_token)

        # State er nå 'uploaded' (satt av _altinn_upload_data).
        # Vi STOPPER her — process/next er bruker-handling i Altinn-portalen.

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'success',
                'title': _("Skattemelding klar i Altinn"),
                'message': _(
                    "Skattemeldingen er lastet opp som utkast i Altinn. "
                    "Klikk 'Åpne i Altinn' for å logge inn med BankID, "
                    "sjekke utkastet og signere/sende inn."
                ),
                'sticky': True,
                'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
            },
        }

    def action_l10n_no_skattemelding_open_in_altinn(self):
        """Åpne Altinn-instansen i ny tab så brukeren kan signere.

        Format for Altinn 3-app-instans UI (verifisert E2E 2026-05-12):
          test: https://skd.apps.tt02.altinn.no/skd/formueinntekt-skattemelding-v2/
                #/instance/{partyId}/{guid}
          prod: https://skd.apps.altinn.no/skd/formueinntekt-skattemelding-v2/
                #/instance/{partyId}/{guid}

        VIKTIG: Hash-fragment (#/instance/...) er KRITISK — uten det
        returnerer Altinn JSON-respons istedenfor SPA-UI. URL uten
        hash-fragment er API-endepunkt for backend-clients (Maskinporten).

        Bruker blir redirected til ID-porten for innlogging, deretter
        landed direkte på instansen — kan signere/sende inn fra portalen.
        """
        self.ensure_one()
        if not self.altinn_instance_guid:
            raise UserError(_(
                "Skattemeldingen er ikke lastet opp til Altinn ennå. "
                "Klikk 'Send til Altinn for signering' først."
            ))
        company = self.company_id
        app_host = _resolve_altinn_app(company)
        url = (
            f"{app_host}/{_SKATTEMELDING_APP_ID}/"
            f"#/instance/{self.altinn_instance_owner_party_id}/"
            f"{self.altinn_instance_guid}"
        )
        return {
            'type': 'ir.actions.act_url',
            'url': url,
            'target': 'new',
        }
        # Bruker ser kvittering-banner når de er tilbake på record-formen.

    def action_l10n_no_skattemelding_resend_to_altinn(self):
        """Send oppdatert utkast til Altinn (DELETE + POST data-element).

        Brukes når brukeren har endret bilag i Odoo etter at utkastet er
        lastet opp til Altinn, og ønsker å oppdatere Altinn-utkastet før
        signering. Standard 'preview-loop' bransje-flyt:

          1. Last opp utkast til Altinn
          2. Logg inn i Altinn → preview
          3. Oppdage feil → rett i Odoo, kjør 'Generer XML' på nytt
          4. Klikk denne knappen → erstatter data-element i Altinn
          5. Refresh Altinn-UI → preview oppdatert versjon
          6. Loop 2-5 til alt stemmer
          7. Signer

        Sekvens:
          - DELETE + POST data-element via _altinn_replace_data
          - Toggle-PUT på Skattemeldingsapp_v2 for å invalidere UI-cache

        Vi kjører IKKE re-validering mot Skatteetaten automatisk her —
        brukeren kan klikke 'Valider mot Skatteetaten' manuelt før resend
        hvis de vil dobbeltsjekke. Auto-validering ville lagt til 30+ sek
        på operasjonen.

        Krever state='uploaded' og at brukeren har rebygget XML
        (requires_resend=True) ELLER vil sende på ny uavhengig av flagg
        (vi blokkerer ikke — ufarlig å re-sende med uendret XML).
        """
        self.ensure_one()
        if self.state != 'uploaded':
            raise UserError(_(
                "Kan kun re-sende når skattemeldingen er på state 'uploaded'. "
                "Nåværende status: %(s)s. Bruk 'Send til Altinn for signering' "
                "for første gangs upload.", s=self.state,
            ))
        if not self.altinn_data_guid:
            raise UserError(_(
                "Ingen Altinn-data-GUID på recorden. Kjør 'Send til Altinn "
                "for signering' først."
            ))
        if not self.konvolutt_xml:
            raise UserError(_(
                "Mangler konvolutt-XML. Kjør 'Generer XML' først."
            ))

        company = self.company_id
        eristo = self.env['l10n.no.eristo.service']
        maskinporten_token = eristo.get_access_token(
            company, scope=_SKATTEMELDING_SCOPE,
        )
        altinn_token = self._exchange_to_altinn_token(maskinporten_token)

        # Steg 1: DELETE + POST nytt data-element
        self._altinn_replace_data(altinn_token)

        # Steg 2: Toggle-PUT Skattemeldingsapp_v2 for UI-cache-invalidation.
        # Uten dette risikerer vi at brukeren ser gammel preview-versjon
        # etter refresh (samme bug som Eristo 2026-05-18).
        try:
            self._toggle_skattemeldingsapp_v2(altinn_token)
        except Exception as e:  # noqa: BLE001
            # Cache-bust er best-effort — hovedoperasjonen lyktes uansett
            _logger.warning(
                "Skattemelding %s: cache-bust etter resend feilet (ignored): %s",
                self.id, str(e)[:200],
            )

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Oppdatert utkast sendt til Altinn"),
                'message': _(
                    "Skattemelding-utkastet i Altinn er erstattet med "
                    "nyeste XML fra Odoo. Hard reload Altinn-fanen "
                    "(Ctrl+Shift+R / Cmd+Shift+R) for å se oppdatert "
                    "preview, og signer."
                ),
                'type': 'success',
                'sticky': True,
                'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
            },
        }

    def _toggle_skattemeldingsapp_v2(self, altinn_token):
        """Helper: gjør toggle-PUT (true → false) på Skattemeldingsapp_v2.

        Trukket ut fra action_..._refresh_altinn_app_state slik at både
        knappen og resend-flow kan gjenbruke samme logikk.
        """
        self.ensure_one()
        company = self.company_id
        app_host = _resolve_altinn_app(company)

        inst_resp = requests.get(
            (f'{app_host}/{_SKATTEMELDING_APP_ID}/instances/'
             f'{self.altinn_instance_owner_party_id}/'
             f'{self.altinn_instance_guid}'),
            headers={'Authorization': f'Bearer {altinn_token}',
                     'Accept': 'application/json'},
            timeout=_HTTP_TIMEOUT,
        )
        inst_resp.raise_for_status()
        inst_data = inst_resp.json()
        app_state_guid = next(
            (d.get('id') for d in (inst_data.get('data') or [])
             if d.get('dataType') == _SKATTEMELDINGSAPP_DATATYPE),
            None,
        )
        if not app_state_guid:
            raise RuntimeError(
                f"Fant ikke {_SKATTEMELDINGSAPP_DATATYPE}-data-element"
            )
        url = (
            f'{app_host}/{_SKATTEMELDING_APP_ID}/instances/'
            f'{self.altinn_instance_owner_party_id}/'
            f'{self.altinn_instance_guid}/data/{app_state_guid}'
        )
        put_headers = {
            'Authorization': f'Bearer {altinn_token}',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }
        for value in (True, False):
            body = json.dumps({
                'inntektsaar': self.inntektsaar,
                'skalBekreftesAvRevisor': value,
            }).encode('utf-8')
            resp = requests.put(
                url, data=body, headers=put_headers, timeout=_HTTP_TIMEOUT,
            )
            resp.raise_for_status()
            time.sleep(1)

    def action_l10n_no_skattemelding_refresh_altinn_app_state(self):
        """Tving toggle av Skattemeldingsapp_v2 for å invalidere UI-cache.

        Bruksområde: hvis Altinn-UIet viser 'Inntektsår: 0 — Feil format
        eller verdi' selv om innsendingen er lastet opp. Symptom på at
        Altinn UI-cache ikke har blitt invalidert (lastChanged er gammel).

        Delegerer til _toggle_skattemeldingsapp_v2-helper, som også
        gjenbrukes av resend-action.
        """
        self.ensure_one()
        if not self.altinn_instance_guid:
            raise UserError(_(
                "Skattemeldingen er ikke lastet opp til Altinn ennå."
            ))
        company = self.company_id
        eristo = self.env['l10n.no.eristo.service']
        maskinporten_token = eristo.get_access_token(
            company, scope=_SKATTEMELDING_SCOPE,
        )
        altinn_token = self._exchange_to_altinn_token(maskinporten_token)
        try:
            self._toggle_skattemeldingsapp_v2(altinn_token)
        except (requests.RequestException, RuntimeError, ValueError) as e:
            raise UserError(_(
                "Toggle-PUT mot Skattemeldingsapp_v2 feilet: %(err)s",
                err=str(e)[:300],
            )) from e

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Altinn-app-state oppdatert"),
                'message': _(
                    "Skattemeldingsapp_v2 ble toggle-PUT-et "
                    "(skalBekreftesAvRevisor: true → false). Refresh "
                    "Altinn-UI'et (hard reload, Ctrl+Shift+R) for å se "
                    "oppdatert inntektsår."
                ),
                'type': 'success',
                'sticky': False,
            },
        }

    # ---------- HTTP-helpers (én per Altinn-steg) ----------

    def _altinn_create_instance(self, altinn_token):
        """POST /instances — opprett tom skattemelding-instans.

        Vi bruker organisationNumber direkte i instanceOwner istedenfor å
        slå opp Altinn-partyId først. Altinn slår selv opp partyId fra orgnr
        internt, og vi unngår /register/parties/lookup-endepunktet som krever
        Ocp-Apim-Subscription-Key (API Management gateway).

        Altinn returnerer instans-id som "partyId/instanceGuid" — vi lagrer
        partyId i altinn_instance_owner_party_id og bruker den i påfølgende
        kall (upload data, process/next).
        """
        self.ensure_one()
        company = self.company_id
        app_host = _resolve_altinn_app(company)
        eristo = self.env['l10n.no.eristo.service']
        orgnr = eristo._orgnr(company)

        url = f'{app_host}/{_SKATTEMELDING_APP_ID}/instances'
        payload = json.dumps({
            'instanceOwner': {'organisationNumber': orgnr},
            'appId': _SKATTEMELDING_APP_ID,
            'dataValues': {'inntektsaar': str(self.inntektsaar)},
        }).encode('utf-8')

        try:
            resp = requests.post(
                url,
                data=payload,
                headers={
                    'Authorization': f'Bearer {altinn_token}',
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                },
                timeout=_HTTP_TIMEOUT,
            )
        except requests.RequestException as e:
            raise UserError(_(
                "Altinn ikke tilgjengelig: %(err)s", err=str(e)[:300],
            ))
        if resp.status_code >= 400:
            err_body = resp.text[:2000]
            _logger.error(
                "Altinn create_instance feilet HTTP %s: %s",
                resp.status_code, err_body,
            )
            raise UserError(_(
                "Kunne ikke opprette Altinn-instans (HTTP %(code)s).\n%(body)s",
                code=resp.status_code, body=err_body[:1000],
            ))
        body = resp.text

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            raise UserError(_(
                "Altinn returnerte ikke gyldig JSON ved instance-oppretting:\n"
                "%(body)s", body=body[:1000],
            ))

        # Altinn returnerer "id": "partyId/instanceGuid" — split for å lagre separat
        instance_id_str = data.get('id') or ''
        if '/' not in instance_id_str:
            raise UserError(_(
                "Altinn returnerte instans uten gyldig id-format:\n%(body)s",
                body=body[:1000],
            ))
        ret_party_id, instance_guid = instance_id_str.split('/', 1)

        # Lagre i ny cursor for å overleve evt. rollback senere i flyten —
        # speiler a-melding-mønsteret. Hvis upload feiler etter dette har
        # vi fortsatt GUID-en så vi ikke oppretter duplikat ved retry.
        #
        # NB: vi gjør IKKE self.write() etterpå — det ville trigge
        # SERIALIZATION_FAILURE i Postgres siden new_cr nettopp committet
        # samme row. Istedenfor invalidater vi cachen og lar Odoo lese
        # de fersk-committede verdiene fra DB ved neste read.
        self._persist_altinn_instance(
            party_id=ret_party_id,
            instance_guid=instance_guid,
        )
        self.invalidate_recordset([
            'altinn_instance_owner_party_id',
            'altinn_instance_guid',
        ])
        _logger.info(
            "Skattemelding %s: opprettet Altinn-instans %s/%s",
            self.id, ret_party_id, instance_guid,
        )

        # Skattemeldingsapp_v2 PUT-en er nå flyttet til _ensure_skatte-
        # meldingsapp_updated som kalles fra main flow (slik at retry
        # også kjører den, ikke bare første gang). Se action_l10n_no_
        # skattemelding_open_submit_wizard (linje ~497).

    def _ensure_skattemeldingsapp_updated(self, altinn_token):
        """Sørg for at Skattemeldingsapp_v2 har korrekt inntektsår.

        Skattemeldingsapp_v2 er Altinn-appens interne form-state som UI'et
        leser fra. Default ved auto-creation har inntektsaar=0, som blokkerer
        signering med "Inntektsår: 0 — Feil format eller verdi".

        Flyt:
          1. Hent instans-metadata (GET /instances/{party}/{guid}) for å finne
             Skattemeldingsapp_v2-data-element-guid'en
          2. Sjekk om dataen allerede har riktig inntektsår (GET data)
          3. Hvis ikke: PUT JSON m. retry-backoff [0,1,2,4]s
          4. Verifiser etter PUT at endringen tok effekt (GET tilbake)
          5. Hvis fortsatt feil: post warning til mail.thread så bruker vet

        Bug funnet 2026-05-18 (alle tre prod-innsendinger Gamify+Roret+
        InGrid): tidligere ble PUT-en kun forsøkt fra _altinn_create_
        instance. Når brukeren fikk feil og retried, hoppet vi over
        create_instance (gjenbrukte eksisterende guid) — og dermed
        også PUT-en. Skattemeldingsapp_v2 forble på default inntektsaar=0.

        HISTORIKK:
          - 9128e06 (05-14): strict XML PUT
          - 6e13032 (05-14): best-effort etter TT02-empiri
          - a2e067e (05-17): strict-mode XML — viste seg å returnere 400
          - 0f42648 (05-18): best-effort JSON — silent fail
          - DENNE (05-18): kall fra main flow + retry + verify

        Skattemeldingsapp_v2 wire-format (JSON, contentType=application/xml
        internt men API tar JSON):
          {"inntektsaar": <int>, "skalBekreftesAvRevisor": <bool>}
        """
        self.ensure_one()
        if not (self.altinn_instance_owner_party_id and self.altinn_instance_guid):
            _logger.info(
                "Skattemelding %s: ingen Altinn-instans ennå — hopper over "
                "Skattemeldingsapp_v2-update", self.id,
            )
            return
        # P1-B fra code-review: valider inntektsaar opp-front så vi ikke
        # gjør runde-tur til Altinn før vi blir at konvertering crasher
        if not self.inntektsaar:
            _logger.warning(
                "Skattemelding %s: inntektsaar er ikke satt — hopper over "
                "Skattemeldingsapp_v2-update", self.id,
            )
            return
        try:
            target_year = int(self.inntektsaar)
        except (TypeError, ValueError):
            _logger.warning(
                "Skattemelding %s: inntektsaar=%r er ikke gyldig integer",
                self.id, self.inntektsaar,
            )
            return
        company = self.company_id
        app_host = _resolve_altinn_app(company)

        # Steg 1: GET instans-metadata for å finne app-state-data-guid'en
        instance_url = (
            f'{app_host}/{_SKATTEMELDING_APP_ID}/instances/'
            f'{self.altinn_instance_owner_party_id}/'
            f'{self.altinn_instance_guid}'
        )
        try:
            inst_resp = requests.get(
                instance_url,
                headers={'Authorization': f'Bearer {altinn_token}',
                         'Accept': 'application/json'},
                timeout=_HTTP_TIMEOUT,
            )
        except requests.RequestException as e:
            _logger.warning(
                "Skattemelding %s: Skattemeldingsapp_v2 — kunne ikke "
                "hente instans-metadata: %s", self.id, str(e)[:200],
            )
            return
        if inst_resp.status_code >= 400:
            _logger.warning(
                "Skattemelding %s: instans-metadata GET HTTP %s: %s",
                self.id, inst_resp.status_code, (inst_resp.text or '')[:200],
            )
            return
        # P1-A fra code-review: wrap JSON-parsing for å unngå crash hvis
        # Altinn returnerer HTTP 200 m. ikke-JSON body (gateway/maint-side)
        try:
            inst_data = inst_resp.json()
        except ValueError as e:
            _logger.warning(
                "Skattemelding %s: instans-metadata GET returnerte ikke-"
                "JSON body: %s. Hopper over Skattemeldingsapp_v2-update.",
                self.id, str(e)[:200],
            )
            return
        app_state_guid = next(
            (d.get('id') for d in (inst_data.get('data') or [])
             if d.get('dataType') == _SKATTEMELDINGSAPP_DATATYPE),
            None,
        )
        if not app_state_guid:
            _logger.warning(
                "Skattemelding %s: fant ikke %s-data-element i instansen",
                self.id, _SKATTEMELDINGSAPP_DATATYPE,
            )
            return

        url = (
            f'{app_host}/{_SKATTEMELDING_APP_ID}/instances/'
            f'{self.altinn_instance_owner_party_id}/'
            f'{self.altinn_instance_guid}/data/{app_state_guid}'
        )

        # NB: Vi gjør IKKE idempotency short-circuit her (selv om data-
        # elementet allerede har korrekt inntektsår). Bakgrunn fra Eristo-
        # innsending 2026-05-18: Altinn auto-extracter inntektsaar fra
        # konvolutten ved create_instance, så data-elementet kunne ha
        # `{"inntektsaar":2025}` allerede. MEN Altinn-UIet har sin egen
        # cache som er uavhengig av data-feltet. Uten en PUT som endrer
        # `lastChanged`-timestampet, viser UIet fortsatt "Inntektsår: 0
        # — Feil format eller verdi" og blokkerer signering.
        #
        # Tidligere P2-A short-circuit (fjernet 2026-05-18 etter Eristo-
        # incident) sparte 7s, men brakk UI-cache-invalidering — netto
        # negativt for brukeren. PUT er idempotent på server-siden, så
        # det er trygt å alltid kjøre den.

        # Steg 2: PUT m. retry-backoff [0,1,2,4]s for å håndtere race-
        # condition der Altinn ennå ikke har materialisert data-elementet
        body = json.dumps({
            'inntektsaar': target_year,
            'skalBekreftesAvRevisor': False,
        }).encode('utf-8')
        put_headers = {
            'Authorization': f'Bearer {altinn_token}',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }
        last_status = None
        last_body = ''
        for attempt, delay in enumerate([0, 1, 2, 4], start=1):
            if delay:
                time.sleep(delay)
            try:
                resp = requests.put(
                    url, data=body, headers=put_headers, timeout=_HTTP_TIMEOUT,
                )
            except requests.RequestException as e:
                _logger.warning(
                    "Skattemelding %s: Skattemeldingsapp_v2 PUT transport-"
                    "feil forsøk %d/4: %s", self.id, attempt, str(e)[:200],
                )
                continue
            last_status = resp.status_code
            last_body = (resp.text or '')[:300]
            if resp.status_code < 400:
                _logger.info(
                    "Skattemelding %s: PUT Skattemeldingsapp_v2 OK "
                    "(forsøk %d, HTTP %s)", self.id, attempt, resp.status_code,
                )
                break
            _logger.warning(
                "Skattemelding %s: PUT forsøk %d/4 HTTP %s — retrying",
                self.id, attempt, resp.status_code,
            )
        else:
            # Alle 4 forsøk feilet
            _logger.error(
                "Skattemelding %s: PUT Skattemeldingsapp_v2 mislyktes etter "
                "4 forsøk (siste HTTP %s): %s",
                self.id, last_status, last_body,
            )
            self.message_post(body=_(
                "⚠ Kunne ikke oppdatere Skattemeldingsapp_v2 etter 4 "
                "forsøk (HTTP %(code)s). Altinn-UI'et vil vise "
                "'Inntektsår: 0 — Feil format eller verdi' og blokkere "
                "signering. Kontakt support for manuell fix.",
                code=last_status,
            ))
            return

        # Steg 3: VERIFISER at endringen tok effekt
        try:
            verify_resp = requests.get(
                url,
                headers={'Authorization': f'Bearer {altinn_token}',
                         'Accept': 'application/json'},
                timeout=_HTTP_TIMEOUT,
            )
        except requests.RequestException:
            _logger.warning(
                "Skattemelding %s: post-PUT verify-GET failed (ignored)",
                self.id,
            )
            return
        if verify_resp.status_code < 400:
            try:
                got = verify_resp.json()
            except ValueError:
                # Ikke-JSON respons → kan ikke verifisere, men PUT lyktes
                # ifølge HTTP 2xx — anta OK og fortsett uten warning
                return
            got_year = got.get('inntektsaar')
            if got_year == target_year:
                _logger.info(
                    "Skattemelding %s: Skattemeldingsapp_v2 verifisert "
                    "m. inntektsår=%s", self.id, target_year,
                )
            elif got_year is None:
                # P1-C fra code-review: hvis inntektsaar-key mangler helt
                # i responsen, kan vi ikke verifisere. Logger info — PUT
                # lyktes HTTP-mssig så data kan være OK, bare uforventet
                # response-format.
                _logger.info(
                    "Skattemelding %s: Skattemeldingsapp_v2 PUT lyktes men "
                    "verify-respons manglet inntektsaar-key (mulig endret "
                    "response-format). Stoler på HTTP-success.", self.id,
                )
            else:
                _logger.error(
                    "Skattemelding %s: Skattemeldingsapp_v2 PUT så ut "
                    "til å lykkes, men GET tilbake ga inntektsaar=%s "
                    "(forventet %s)",
                    self.id, got_year, target_year,
                )
                self.message_post(body=_(
                    "⚠ Skattemeldingsapp_v2 PUT-en lyktes (HTTP 2xx) "
                    "men verifikasjon ga inntektsår=%(got)s istedenfor "
                    "%(want)s. Altinn-UI'et kan vise feil verdi.",
                    got=got_year, want=target_year,
                ))

    def _altinn_lookup_party_id(self, altinn_token, platform_host):
        """Slå opp Altinn-partyId for selskapet.

        Altinn refererer organisasjoner ved sin egen interne partyId. For
        et AS slår vi opp på orgnr via /register/api/v1/parties/lookup.
        """
        self.ensure_one()
        eristo = self.env['l10n.no.eristo.service']
        orgnr = eristo._orgnr(self.company_id)

        # Altinn-spec: POST /register/api/v1/parties/lookup med PascalCase-felt
        # OrgNo (Ssn er alternativ for personer). ASP.NET Core binder
        # case-insensitive så orgNo ville også fungert, men vi følger spec.
        url = f'{platform_host}/register/api/v1/parties/lookup'
        payload = json.dumps({'OrgNo': orgnr}).encode('utf-8')
        try:
            resp = requests.post(
                url,
                data=payload,
                headers={
                    'Authorization': f'Bearer {altinn_token}',
                    'Content-Type': 'application/json',
                    'Accept': 'application/json',
                },
                timeout=_HTTP_TIMEOUT,
            )
        except requests.RequestException as e:
            raise UserError(_(
                "Altinn ikke tilgjengelig: %(err)s", err=str(e)[:300],
            ))
        if resp.status_code >= 400:
            raise UserError(_(
                "Altinn party-oppslag feilet (HTTP %(code)s) for orgnr "
                "%(orgnr)s.\n%(body)s",
                code=resp.status_code, orgnr=orgnr,
                body=resp.text[:1000],
            ))
        try:
            data = resp.json()
        except json.JSONDecodeError:
            raise UserError(_(
                "Altinn returnerte ikke gyldig JSON for party-oppslag."
            ))
        party_id = data.get('partyId')
        if not party_id:
            raise UserError(_(
                "Altinn fant ingen partyId for orgnr %(orgnr)s — er selskapet "
                "registrert i Enhetsregisteret?", orgnr=orgnr,
            ))
        return party_id

    def _altinn_upload_data(self, altinn_token):
        """POST data-element (konvolutt-XML) til instansen.

        Body = rå konvolutt-XML, Content-Type=application/xml. Altinn
        returnerer data-elementets ID i 'id'-feltet av responsen.

        Auto-retry på 404: Altinn-instansen er nettopp opprettet og kan
        ta noen sekunder å bli tilgjengelig på data-endpointet (kjent
        race condition i Altinn TT02). Vi prøver opptil 4 ganger med
        eksponentielt økende backoff (0s, 1s, 2s, 4s = ~7s totalt) før
        vi ber brukeren om å klikke en gang til.

        Designvalg (P1 #1): Vi holder oss til <10 sek for å unngå å
        blokkere Odoo-worker over lang tid. Empirisk fra TT02 er 99%
        av instansene synlige innen 5 sek; resten kan recovere ved
        at brukeren klikker 'Send til Altinn for signering' en gang
        til — vi gjenbruker eksisterende altinn_instance_guid.
        """
        self.ensure_one()
        company = self.company_id
        app_host = _resolve_altinn_app(company)
        url = (
            f'{app_host}/{_SKATTEMELDING_APP_ID}/instances/'
            f'{self.altinn_instance_owner_party_id}/'
            f'{self.altinn_instance_guid}/data?dataType={_DATATYPE_KONVOLUTT}'
        )
        # Altinn data-API krever Content-Disposition-header for binary
        # data-elementer (verifisert mot TT02 2026-05-11 — HTTP 400 uten).
        # Vi sender raw XML som attachment med filnavn skattemelding.xml.
        filename = f'skattemelding-{self.inntektsaar}.xml'
        headers = {
            'Authorization': f'Bearer {altinn_token}',
            # Skatteetaten-appens dataType skattemeldingOgNaerings-
            # spesifikasjon aksepterer kun text/xml — ikke application/xml.
            # Permitted content types liste returnert i 400 fra TT02.
            'Content-Type': 'text/xml',
            'Content-Disposition': f'attachment; filename="{filename}"',
            'Accept': 'application/json',
        }

        body = None
        last_error = None
        # 4 forsøk over ~7 sek: 0s, 1s, 2s, 4s. Eksponentiell backoff
        # for å håndtere kjent race-condition i Altinn (data-endpoint
        # er ikke umiddelbart synlig etter instans-opprettelse).
        # Empirisk fra TT02: 99% av instansene er synlige innen 5 sek.
        # Backoff utvidet 2026-05-19 fra [0,1,2,4]=7s til [0,3,7,15]=25s
        # etter 4 prod-innsendinger (Gamify, Roret, InGrid, Eristo) der
        # 7-sek-vinduet alltid var for kort — alle 4 måtte klikke "Send
        # til Altinn" minst 2 ganger. Altinn trenger empirisk 10-20 sek
        # for å materialisere dataType-endpointene etter create_instance.
        # 25 sek totalt-vindu er trygt innenfor Odoo HTTP-timeout.
        backoff_schedule = [0, 3, 7, 15]
        total_attempts = len(backoff_schedule)
        for attempt, delay in enumerate(backoff_schedule, start=1):
            if delay:
                time.sleep(delay)
            try:
                resp = requests.post(
                    url, data=self.konvolutt_xml.encode('utf-8'),
                    headers=headers, timeout=_HTTP_TIMEOUT,
                )
            except requests.RequestException as e:
                raise UserError(_(
                    "Altinn ikke tilgjengelig: %(err)s", err=str(e)[:300],
                ))
            if resp.status_code < 400:
                body = resp.text
                break  # success — exit retry-loop
            err_body = resp.text[:2000] if resp.text else ''
            last_error = (resp.status_code, err_body)
            _logger.warning(
                "Altinn upload_data forsøk %d/%d: HTTP %s. Body: %s",
                attempt, total_attempts, resp.status_code, err_body[:200],
            )
            # 404 er race condition — retry. Andre HTTP-feil er ikke
            # transient, så avbryt umiddelbart.
            if resp.status_code != 404:
                break

        if body is None:
            # Alle forsøk feilet
            code, err_body = last_error or (0, '')
            if code == 404:
                # Brukervennlig melding — unngå teknisk jargon som
                # "race-condition". Brukeren skal kun gjøre én ting:
                # klikke samme knapp en gang til.
                raise UserError(_(
                    "Altinn er ennå ikke klar til å motta dataen "
                    "(ventet ~25 sekunder). Dette er uvanlig, men ikke "
                    "alvorlig.\n\n"
                    "Klikk 'Send til Altinn for signering' en gang til "
                    "— vi gjenbruker samme instans automatisk. Hvis det "
                    "fortsatt feiler, kontakt support."
                ))
            raise UserError(_(
                "Kunne ikke laste opp konvolutt-XML til Altinn (HTTP %(code)s).\n"
                "%(body)s", code=code, body=err_body[:1000],
            ))

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            raise UserError(_(
                "Altinn returnerte ikke gyldig JSON ved data-upload:\n%(body)s",
                body=body[:1000],
            ))
        data_guid = data.get('id')
        if not data_guid:
            raise UserError(_(
                "Altinn upload returnerte respons uten data-id:\n%(body)s",
                body=body[:1000],
            ))
        self.write({
            'altinn_data_guid': data_guid,
            'state': 'uploaded',
        })
        _logger.info(
            "Skattemelding %s: lastet opp data-element %s",
            self.id, data_guid,
        )

    def _altinn_replace_data(self, altinn_token):
        """Erstatt eksisterende data-element med ny konvolutt-XML.

        Brukes når brukeren har rettet bilag i Odoo etter at utkastet er
        lastet opp til Altinn — typisk fordi de oppdager feil i Altinn-
        preview-en og ønsker å sende inn på nytt.

        Sekvens (DELETE + POST, ikke PUT):
          1. DELETE /instances/{party}/{guid}/data/{data_guid}
          2. POST  /instances/{party}/{guid}/data?dataType=...
          3. Oppdater altinn_data_guid med ny GUID
          4. Reset requires_resend = False

        Rasjonale for DELETE+POST framfor PUT:
          - Skatteetatens dataType skattemeldingOgNaeringsspesifikasjon
            forventer POST for nye data-elementer (verifisert TT02 2026-05-11).
          - DELETE + POST gir oss en helt fersk GUID, noe som dokumenterer
            audit-trail i Altinn (forrige versjon blir slettet, ny opprettes
            med ny timestamp).
          - Alternativt PUT på eksisterende GUID kunne teknisk fungert,
            men dokumentasjonen rundt det er uklar for binary data-elementer.

        Sikkerhetsventil: krever state='uploaded' og altinn_data_guid satt.
        Sjekk i _altinn_create_instance-flow trengs ikke — instansen
        eksisterer per definisjon hvis vi har et data_guid.
        """
        self.ensure_one()
        if not self.altinn_data_guid:
            raise UserError(_(
                "Ingen eksisterende Altinn-data-GUID å erstatte. "
                "Bruk 'Send til Altinn for signering' for første gangs upload."
            ))
        if not self.konvolutt_xml:
            raise UserError(_(
                "Mangler konvolutt-XML. Kjør 'Generer XML' først."
            ))

        company = self.company_id
        app_host = _resolve_altinn_app(company)
        base_url = (
            f'{app_host}/{_SKATTEMELDING_APP_ID}/instances/'
            f'{self.altinn_instance_owner_party_id}/'
            f'{self.altinn_instance_guid}'
        )
        old_data_guid = self.altinn_data_guid

        # Steg 1: DELETE eksisterende data-element
        del_url = f'{base_url}/data/{old_data_guid}'
        try:
            del_resp = requests.delete(
                del_url,
                headers={
                    'Authorization': f'Bearer {altinn_token}',
                    'Accept': 'application/json',
                },
                timeout=_HTTP_TIMEOUT,
            )
        except requests.RequestException as e:
            raise UserError(_(
                "Kunne ikke slette gammelt data-element i Altinn: %(err)s",
                err=str(e)[:300],
            )) from e
        # 404 er OK — data-elementet er allerede borte. 204/200 er OK.
        if del_resp.status_code not in (200, 204, 404):
            raise UserError(_(
                "DELETE av data-element feilet (HTTP %(code)s):\n%(body)s",
                code=del_resp.status_code,
                body=(del_resp.text or '')[:500],
            ))
        _logger.info(
            "Skattemelding %s: DELETE av data-element %s OK (HTTP %s)",
            self.id, old_data_guid, del_resp.status_code,
        )

        # Steg 2: POST ny konvolutt-XML
        post_url = f'{base_url}/data?dataType={_DATATYPE_KONVOLUTT}'
        filename = f'skattemelding-{self.inntektsaar}.xml'
        post_headers = {
            'Authorization': f'Bearer {altinn_token}',
            'Content-Type': 'text/xml',
            'Content-Disposition': f'attachment; filename="{filename}"',
            'Accept': 'application/json',
        }
        try:
            post_resp = requests.post(
                post_url, data=self.konvolutt_xml.encode('utf-8'),
                headers=post_headers, timeout=_HTTP_TIMEOUT,
            )
        except requests.RequestException as e:
            raise UserError(_(
                "Kunne ikke laste opp ny konvolutt-XML til Altinn: %(err)s",
                err=str(e)[:300],
            )) from e
        if post_resp.status_code >= 400:
            raise UserError(_(
                "POST av ny data-element feilet (HTTP %(code)s):\n%(body)s",
                code=post_resp.status_code,
                body=(post_resp.text or '')[:1000],
            ))

        try:
            data = json.loads(post_resp.text)
        except json.JSONDecodeError:
            raise UserError(_(
                "Altinn returnerte ikke gyldig JSON ved re-upload:\n%(body)s",
                body=post_resp.text[:1000],
            ))
        new_data_guid = data.get('id')
        if not new_data_guid:
            raise UserError(_(
                "Altinn re-upload returnerte respons uten data-id:\n%(body)s",
                body=post_resp.text[:1000],
            ))

        self.write({
            'altinn_data_guid': new_data_guid,
            'requires_resend': False,
        })
        _logger.info(
            "Skattemelding %s: erstattet data-element %s → %s",
            self.id, old_data_guid, new_data_guid,
        )

    def _altinn_advance_process(self, altinn_token):
        """PUT process/next — flytt instansen gjennom prosess-stegene.

        Skatteetaten-app-en har 2 task-steg + EndEvent. Vi looper
        process/next-PUT inntil prosessen er endet eller vi har gjort
        _MAX_PROCESS_ITERATIONS forsøk (sikkerhetsventil).

        Skatteetatens skd/formueinntekt-skattemelding-v2 har Task_3 som
        "feedback"-type — instansen venter da på respons fra Skatteetatens
        backend. User kan IKKE advance feedback-tasks (gir HTTP 403).
        Innsendingen er fullført når vi treffer en feedback-task.

        Vi GET'er instansen først for å sjekke gjeldende task-type. Hvis
        den allerede er på feedback (f.eks. ved retry), setter vi state
        submitted uten å kalle PUT. Dette håndterer idempotent retry.

        Hver iterasjon:
          - PUT /process/next
          - Sjekk respons: ended OR currentTask=None OR feedback → ferdig
          - Ellers: oppdater altinn_process_current_task og loop
        """
        self.ensure_one()
        company = self.company_id
        app_host = _resolve_altinn_app(company)
        base = (
            f'{app_host}/{_SKATTEMELDING_APP_ID}/instances/'
            f'{self.altinn_instance_owner_party_id}/'
            f'{self.altinn_instance_guid}'
        )
        url = f'{base}/process/next'

        # Pre-check: GET instansen for å se om den allerede er på
        # feedback-task eller endt — i så fall skip PUT-loopen.
        if self._altinn_check_already_submitted(altinn_token, base):
            return

        ended = False
        last_task = self.altinn_process_current_task
        for iteration in range(_MAX_PROCESS_ITERATIONS):
            try:
                resp = requests.put(
                    url,
                    data=b'',  # tom body — Altinn forventer det
                    headers={
                        'Authorization': f'Bearer {altinn_token}',
                        'Content-Type': 'application/json',
                        'Accept': 'application/json',
                    },
                    timeout=_HTTP_TIMEOUT,
                )
            except requests.RequestException as e:
                raise UserError(_(
                    "Altinn ikke tilgjengelig: %(err)s", err=str(e)[:300],
                ))
            if resp.status_code >= 400:
                err_body = resp.text[:2000]
                _logger.error(
                    "Altinn process/next feilet HTTP %s (iter %d): %s",
                    resp.status_code, iteration, err_body,
                )
                raise UserError(_(
                    "Process/next feilet (HTTP %(code)s) ved iterasjon "
                    "%(i)d.\n%(body)s\n\nNåværende task: %(task)s",
                    code=resp.status_code, i=iteration,
                    body=err_body[:800],
                    task=last_task or 'ukjent',
                ))
            body = resp.text

            try:
                data = json.loads(body) if body else {}
            except json.JSONDecodeError:
                data = {}

            current_task_info = (data.get('currentTask') or {})
            current_task = current_task_info.get('elementId')
            current_task_type = current_task_info.get('altinnTaskType') \
                or current_task_info.get('taskType')
            ended_iso = data.get('ended')

            # Altinn returnerer "ended" som ISO-timestamp når prosessen
            # er ferdig (helt avsluttet). currentTask blir også None.
            #
            # Skatteetatens skd/formueinntekt-skattemelding-v2 har Task_3
            # som "feedback"-type — instansen venter da på respons fra
            # Skatteetatens backend (Skatteetaten kjører validering +
            # beregning asynkront og pusher feedback). User kan IKKE
            # advance feedback-tasks — det er Skatteetatens jobb.
            # Innsendingen er fullført når vi når en feedback-task.
            feedback_task = current_task_type == 'feedback'
            if ended_iso or current_task is None or feedback_task:
                ended = True
                self.write({
                    'state': 'submitted',
                    'submitted_at': fields.Datetime.now(),
                    'altinn_process_current_task': current_task or 'EndEvent',
                })
                _logger.info(
                    "Skattemelding %s: prosess endet etter %d iterasjon(er) "
                    "(ended=%s)", self.id, iteration + 1, ended_iso,
                )
                break

            # Prosess fortsetter — oppdater current task og loop
            if current_task and current_task != last_task:
                self.altinn_process_current_task = current_task
                last_task = current_task
            _logger.debug(
                "Skattemelding %s: process iter %d, current_task=%s",
                self.id, iteration, current_task,
            )

        if not ended:
            raise UserError(_(
                "Process/next nådde max iterasjoner (%(n)d) uten å ende. "
                "Siste task: %(t)s. Sjekk Altinn-instansen manuelt og "
                "rapporter til Eristo support.",
                n=_MAX_PROCESS_ITERATIONS, t=last_task,
            ))

    def _altinn_check_already_submitted(self, altinn_token, base_url):
        """GET /instances/{partyId}/{guid} — sjekk om allerede submitted.

        Brukes som pre-check i _altinn_advance_process for å håndtere
        idempotent retry. Hvis instansen allerede er på feedback-task
        eller process.ended er satt, behandler vi det som submitted og
        unngår å trigge 403 fra PUT process/next.

        Returnerer True hvis state ble satt til submitted (skip videre PUT).
        """
        self.ensure_one()
        try:
            resp = requests.get(
                base_url,
                headers={
                    'Authorization': f'Bearer {altinn_token}',
                    'Accept': 'application/json',
                },
                timeout=_HTTP_TIMEOUT,
            )
        except requests.RequestException:
            # Pre-check feiler ikke flow-en — bare anta vi må advance
            return False
        if resp.status_code >= 400:
            return False
        try:
            data = resp.json()
        except json.JSONDecodeError:
            return False
        process = data.get('process') or {}
        current_task_info = process.get('currentTask') or {}
        current_task = current_task_info.get('elementId')
        current_task_type = (current_task_info.get('altinnTaskType')
                             or current_task_info.get('taskType'))
        ended_iso = process.get('ended')
        feedback = current_task_type == 'feedback'
        if ended_iso or current_task is None or feedback:
            self.write({
                'state': 'submitted',
                'submitted_at': fields.Datetime.now(),
                'altinn_process_current_task': current_task or 'EndEvent',
            })
            _logger.info(
                "Skattemelding %s: pre-check oppdaget allerede submitted "
                "(task=%s, type=%s, ended=%s)",
                self.id, current_task, current_task_type, ended_iso,
            )
            return True
        return False

    # Kvittering-fetch + arkivering + cron-polling er flyttet til
    # l10n_no_skattemelding_kvittering.py (P3 #8 — split for å holde
    # filene under ~1000 linjer per ansvarsområde).

    # ---------- Idempotency-helper ----------

    def _persist_altinn_instance(self, party_id, instance_guid):
        """Lagre instance-GUID i ny cursor — overlever rollback.

        Hvis upload_data eller advance_process feiler etter at instansen er
        opprettet, vil Odoo rulle tilbake hele transaksjonen. Da mister vi
        koblingen til Altinn-instansen og kunne ende opp med duplikat ved
        retry. Vi committer derfor GUID-en i en separat cursor med samme
        pattern som a-melding bruker for idempotency_key.
        """
        self.ensure_one()
        with self.pool.cursor() as new_cr:
            new_cr.execute(
                """
                UPDATE l10n_no_skattemelding
                   SET altinn_instance_owner_party_id = %s,
                       altinn_instance_guid = %s
                 WHERE id = %s
                """,
                (party_id, instance_guid, self.id),
            )
            # cursor.commit() implisitt via with-statement
