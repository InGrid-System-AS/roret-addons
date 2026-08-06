"""HTTP-klient mot Eristo Token Service.

Eristo Token Service (Roret Compliance Gateway, api.roret.no) er en
sentral tjeneste som signerer Maskinporten-JWT på vegne av kunde-Odoo.
Hver kunde har en API-key som identifiserer dem mot tjenesten — privat
nøkkel er aldri i kundens Odoo.

Denne abstract-modellen er auth-laget som alle Skatteetaten-integrasjoner
bruker (a-melding, skattekort, MVA, skattemelding). Domene-spesifikke
moduler kaller ``get_access_token(company, scope)`` med riktig scope og
bruker det returnerte tokenet i sin egen Skatteetaten-API-call.
"""
import json
import logging
import re
import socket
import urllib.error
import urllib.request

from odoo import _, api, models
from odoo.exceptions import AccessError, UserError

_logger = logging.getLogger(__name__)

# Minste gateway-versjon disse modulene krever. Gatewayen oppgir sin
# versjon i /eristo-ping (felt: gateway_version).
#
# BUMP NÅR modulene begynner å bruke et endepunkt eller en semantikk som
# eldre gatewayer ikke har. Da får kunden en tydelig advarsel ved «Test
# forbindelse» i stedet for en uforklarlig feil midt i en innsending —
# f.eks. 501 fra /idporten-* når gatewayen mangler ID-porten-klient.
GATEWAY_MIN_VERSION = '1.1.0'

# Token-scopes gatewayen aktiverer PÅ EGET INITIATIV, uten at noen
# onboarding-forespørsel nevner dem. De er de eneste verdiene fra
# /eristo-ping vi tar inn i l10n_no_eristo_active_scopes.
#
# Hvitelista er nødvendig fordi pingens active_scopes er nøklet på
# TOKEN-scope, mens modulenes aktivert-flagg leser ONBOARDING-nøkler, og
# de to kolliderer: MVA onboardes som skatteetaten:mvamelding men får
# token-scopet altinn:instances.write — som er nøyaktig
# _ARSREGNSKAP_SCOPE. Tok vi inn hele lista, ville et MVA-only-selskap
# fått l10n_no_arsregnskap_aktivert = True uten å ha den delegeringen.
#
# Et nytt følgescope i gatewayen krever derfor en linje her. Det er
# tilsiktet: modulene skal vite hva de tar inn, ikke absorbere alt
# gatewayen rapporterer.
GATEWAY_FOLGESCOPES = frozenset({'digdir:dialogporten'})


def folgescopes_fra_ping(active_scopes):
    """Plukk ut følgescopene i et /eristo-ping-svar. Resten skal IKKE inn.

    Pingens active_scopes er nøklet på TOKEN-scope, mens modulenes
    aktivert-flagg leser ONBOARDING-nøkler. Overlappet er ikke tomt:
    altinn:instances.write er både MVA-ens token-scope og årsregnskaps
    onboarding-nøkkel, så en ufiltrert innlesing gir et MVA-only-selskap
    l10n_no_arsregnskap_aktivert = True uten den delegeringen.

    Begge skrivestiene (wizarden ved accepted, og «Test Eristo-
    forbindelse») går gjennom denne, slik at filteret ikke kan gli fra
    hverandre mellom dem.
    """
    return [
        s for s in (str(x).strip() for x in (active_scopes or []))
        if s in GATEWAY_FOLGESCOPES
    ]


def _versjon_tuple(versjon):
    """'1.2.0' -> (1, 2, 0). Ukjent/uparsebar form gir None."""
    try:
        deler = tuple(int(d) for d in str(versjon).strip().split('.'))
    except (AttributeError, ValueError):
        return None
    return deler if len(deler) == 3 else None


def gateway_for_gammel(rapportert, minimum=GATEWAY_MIN_VERSION):
    """True hvis gatewayen er eldre enn modulene krever.

    Ukjent versjon regnes som OK: en gateway fra før feltet fantes skal
    ikke gi falsk alarm — da er det opp til driftsrutinene. Vi advarer
    kun når vi POSITIVT vet at den er for gammel.
    """
    a, b = _versjon_tuple(rapportert), _versjon_tuple(minimum)
    if a is None or b is None:
        return False
    return a < b

# Strict 9-digit Norwegian organization number, with optional 'NO' prefix
# and 'MVA' suffix (typical VAT-formatting).
_ORGNR_RE = re.compile(r'^(NO)?(\d{9})(MVA)?$')


class L10nNoEristoService(models.AbstractModel):
    _name = 'l10n.no.eristo.service'
    _description = 'Eristo Token Service-klient + felles utility-helpers'

    @api.model
    def _orgnr(self, company):
        """Trekk ut 9-sifret orgnr for Skatteetaten/Altinn-kall.

        Bruker company.l10n_no_eristo_test_orgnr som override hvis satt
        (typisk syntetisk Tenor-orgnr for TT02-test der ekte orgnr ikke
        er importert til Skatteetatens registre). Faller tilbake på
        company.vat ellers.

        Aksepterer 'NO123456789MVA', '123456789', 'NO 123 456 789' osv.
        Avviser alt annet (utenlandsk VAT, ufullstendig nummer).
        Skatteetatens API forkaster eventuelt feil-formaterte nummer med
        cryptic feilmeldinger — bedre å fange det her med klar tekst.
        """
        # Test-override (kun satt på staging/test-miljø; tom i prod)
        override = company.l10n_no_eristo_test_orgnr
        if override:
            normalized = re.sub(r'\s+', '', override)
            match = _ORGNR_RE.match(normalized)
            if match:
                return match.group(2)
            raise UserError(_(
                "Selskapet '%(name)s' har ugyldig test-orgnr override "
                "('%(o)s'). Forventet 9 sifre.",
                name=company.display_name, o=override,
            ))
        if not company.vat:
            raise UserError(_(
                "Selskapet '%(name)s' mangler organisasjonsnummer (VAT-felt).",
                name=company.display_name,
            ))
        normalized = re.sub(r'\s+', '', company.vat)
        match = _ORGNR_RE.match(normalized)
        if not match:
            raise UserError(_(
                "Selskapet '%(name)s' har ikke et gyldig 9-sifret norsk "
                "organisasjonsnummer i VAT-feltet ('%(vat)s'). "
                "Forventet format: 'NO123456789MVA' eller '123456789'.",
                name=company.display_name, vat=company.vat,
            ))
        return match.group(2)

    @api.model
    def get_access_token(self, company, scope):
        """Hent Maskinporten access-token via Eristo Token Service.

        ``scope`` MÅ oppgis — kall fra a-melding bruker
        ``skatteetaten:innrapporteringamelding``, skattekort bruker
        ``skatteetaten:skattekorttilarbeidsgiver``, osv.

        Selve JWT-signeringen og Maskinporten-utvekslingen skjer i
        Eristos sentrale token-tjeneste. Privat nøkkel er aldri i
        kundens Odoo.
        """
        if not (company.l10n_no_eristo_token_url
                and company.l10n_no_eristo_api_key):
            raise UserError(_(
                "Eristo Token Service-konfigurasjon mangler. "
                "Settings → Companies → Skatteetaten-tilkobling: "
                "legg inn token-URL + API-key."
            ))
        if not scope:
            raise UserError(_(
                "Internfeil: Eristo-service.get_access_token kalt uten scope."
            ))

        body = json.dumps({'scope': scope}).encode('utf-8')
        req = urllib.request.Request(
            company.l10n_no_eristo_token_url,
            data=body,
            method='POST',
            headers={
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {company.l10n_no_eristo_api_key}',
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                payload = json.loads(resp.read())
            return payload['access_token']
        except urllib.error.HTTPError as e:
            err_body = e.read().decode()[:500]
            _logger.error("Eristo token-service error %s: %s", e.code, err_body)
            if e.code == 401:
                raise UserError(_(
                    "Eristo Token Service: ugyldig eller deaktivert API-key. "
                    "Kontakt Eristo support for å verifisere abonnementet."
                ))
            raise UserError(_(
                "Eristo Token Service feilet (HTTP %(code)s).\n%(body)s",
                code=e.code, body=err_body,
            ))
        except (urllib.error.URLError, socket.timeout) as e:
            _logger.error("Eristo token-service unreachable: %s", e)
            raise UserError(_(
                "Eristo Token Service er ikke tilgjengelig: %(err)s\n\n"
                "Sjekk internettforbindelse og prøv igjen om noen minutter. "
                "Hvis problemet vedvarer, kontakt Eristo support.",
                err=str(e)[:300],
            ))

    @api.model
    def _eristo_ping_url(self, company):
        """Avled ping-endpoint-URL fra token-URL.

        Eristo Token Service-URL ender på 'maskinporten-token'. Vi
        bytter til 'eristo-ping' for å treffe health-check-endepunktet.
        """
        if not company.l10n_no_eristo_token_url:
            raise UserError(_(
                "Eristo Token Service-URL er ikke konfigurert. "
                "Settings → Companies → Skatteetaten-tilkobling."
            ))
        return company.l10n_no_eristo_token_url.replace(
            'maskinporten-token', 'eristo-ping',
        )

    @api.model
    def ping(self, company):
        """Verifiser at API-key + URL fungerer mot Eristo Token Service.

        Kaller /eristo-ping som kun validerer API-key (ingen Maskinporten/
        Altinn-kall). Brukes av "Test Eristo-forbindelse"-knappen for å
        bekrefte tilkobling uten å kreve at noen scope er aktivert.

        Returnerer dict m.:
          - status: 'ok'
          - customer: {name, orgnr, environment, active_scopes (liste)}
          - gateway_version: tjenestens semver, ELLER fraværende hvis
            tjenesten er eldre enn feltet (da vet vi ingenting)
          - gateway_version_advarsel: kun til stede når tjenesten er
            ELDRE enn GATEWAY_MIN_VERSION. Ferdig formulert, brukervendt
            tekst — konsumenter bør vise den, ikke bare logge den.

        Reiser UserError ved feil (401 ugyldig key, timeout, etc.).
        """
        if not (company.l10n_no_eristo_token_url
                and company.l10n_no_eristo_api_key):
            raise UserError(_(
                "Eristo Token Service-konfigurasjon mangler. "
                "Settings → Companies → Skatteetaten-tilkobling: "
                "legg inn token-URL + API-key."
            ))
        url = self._eristo_ping_url(company)
        req = urllib.request.Request(
            url,
            method='GET',
            headers={
                'Authorization': f'Bearer {company.l10n_no_eristo_api_key}',
                'Accept': 'application/json',
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                svar = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            err_body = e.read().decode()[:500]
            _logger.error("Eristo ping error %s: %s", e.code, err_body)
            if e.code == 401:
                raise UserError(_(
                    "Eristo Token Service: ugyldig eller deaktivert API-key. "
                    "Kontakt Eristo support for å verifisere abonnementet."
                ))
            raise UserError(_(
                "Eristo Token Service ping feilet (HTTP %(code)s).\n%(body)s",
                code=e.code, body=err_body,
            ))
        except (urllib.error.URLError, socket.timeout) as e:
            raise UserError(_(
                "Eristo Token Service er ikke tilgjengelig: %(err)s",
                err=str(e)[:300],
            ))

        # Kompatibilitetskontrakt: advar hvis tjenesten er eldre enn
        # modulene krever. Vi BLOKKERER ikke — en for gammel gateway kan
        # fortsatt betjene det meste, og en hard feil her ville tatt ned
        # fungerende flyt. Advarselen gjør at avviket oppdages ved «Test
        # forbindelse», ikke midt i en innsending mot en frist.
        rapportert = svar.get('gateway_version')
        if gateway_for_gammel(rapportert):
            svar['gateway_version_advarsel'] = _(
                "Tjenesten kjører versjon %(har)s, men modulene forventer "
                "minst %(krav)s. Noen funksjoner kan mangle — kontakt "
                "Eristo support for oppgradering.",
                har=rapportert, krav=GATEWAY_MIN_VERSION,
            )
            _logger.warning(
                "Eristo-gateway %s er eldre enn modulkravet %s (selskap %s)",
                rapportert, GATEWAY_MIN_VERSION, company.display_name)
        return svar

    @api.model
    def _eristo_onboard_url(self, company):
        """Avled onboard-systembruker-endpoint-URL fra token-URL.

        Token- og onboard-endepunktene ligger på SAMME tjeneste, så
        vi avleder onboard-URL ved enkel string-replacement — trygt
        siden deployments er kontrollert av Eristo.

        MERK: alle avledede endepunkter (onboard, bank, ID-porten) følger
        automatisk med når token-URL-en endres. Ved repoint av en kunde
        må derfor HVER av dem verifiseres, ikke bare token-kallet.
        """
        if not company.l10n_no_eristo_token_url:
            raise UserError(_(
                "Eristo Token Service-URL er ikke konfigurert. "
                "Settings → Companies → Skatteetaten-tilkobling."
            ))
        return company.l10n_no_eristo_token_url.replace(
            'maskinporten-token', 'onboard-systembruker',
        )

    @api.model
    def request_onboarding(self, company, scopes, party_orgnr=None):
        """Opprett en Altinn 3 systembruker-request for kunden.

        Returnerer dict med:
          - altinn_request_id: UUID fra Altinn
          - external_ref: vår intern-ref, lagres i token-tjenesten
          - confirm_url: URL kunden klikker for å godkjenne i Altinn-portalen
          - status: 'pending'
          - scopes: hvilke scopes som er forespurt

        ``scopes`` er liste av Maskinporten-scope-strings, eks.
        ['skatteetaten:formueinntekt/skattemelding']. Hver modul kaller
        med scopes relevant for sin tjeneste.

        ``party_orgnr`` (optional) overstyrer selskapets orgnr — typisk
        kun brukt for test-scenarier der vi forespør på vegne av et
        Skatteetaten-test-orgnr istedenfor produksjons-selskapet.
        """
        if not company.l10n_no_eristo_api_key:
            raise UserError(_(
                "Eristo Token Service-konfigurasjon mangler API-key."
            ))
        if not scopes:
            raise UserError(_("Internfeil: request_onboarding kalt uten scopes."))

        url = self._eristo_onboard_url(company)
        body = {'scopes': list(scopes)}
        if party_orgnr:
            body['party_orgnr'] = party_orgnr
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode('utf-8'),
            method='POST',
            headers={
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {company.l10n_no_eristo_api_key}',
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            err_body = e.read().decode()[:1000]
            _logger.error("Eristo onboarding-service error %s: %s", e.code, err_body)
            if e.code == 400:
                raise UserError(_(
                    "Onboarding-forespørsel avvist:\n%(body)s",
                    body=err_body,
                ))
            if e.code == 401:
                raise UserError(_(
                    "Eristo Token Service: ugyldig eller deaktivert API-key."
                ))
            if e.code == 403:
                # Typisk party_orgnr-override utenfor test-miljø, der
                # tjenestens egen kropp forklarer hvorfor. Koden tas med:
                # en 403 fra et mellomledd har HTML-kropp, og da er
                # statuslinja det eneste holdepunktet.
                raise UserError(_(
                    "Onboarding avvist av tjenesten (HTTP %(code)s):"
                    "\n%(body)s",
                    code=e.code, body=err_body,
                ))
            if e.code == 404:
                # Tjenesten kan ha onboarding-flaten stengt mot offentlig
                # nett (nginx svarer 404 uten å røpe at endepunktet
                # finnes). Uten denne grenen fikk brukeren «HTTP 404» med
                # en HTML-kropp og ingen anelse om hva som var galt.
                raise UserError(_(
                    "Onboarding-endepunktet er ikke tilgjengelig på "
                    "%(url)s.\n\nTjenesten eksponerer det ikke mot "
                    "internett, eller URL-en peker feil. Kontakt "
                    "leverandøren — aktivering av scopes må da gjøres "
                    "på tjenesten.",
                    url=url,
                ))
            raise UserError(_(
                "Eristo onboarding-service feilet (HTTP %(code)s).\n%(body)s",
                code=e.code, body=err_body,
            ))
        except (urllib.error.URLError, socket.timeout) as e:
            raise UserError(_(
                "Eristo Onboarding Service ikke tilgjengelig: %(err)s",
                err=str(e)[:300],
            ))

    @api.model
    def check_onboarding_status(self, company, external_ref):
        """Sjekk status på en pending onboarding-request.

        Returnerer dict m. minst {status, external_ref}. Når status er
        'accepted' har Eristo Token Service oppdatert customer-raden,
        og kunden kan begynne å bruke tjenesten.

        Status-verdier:
          - 'pending': kunde har ikke godkjent enda (eller akkurat klikket)
          - 'accepted': kunde har godkjent i Altinn-portalen — klar til bruk
          - 'rejected': kunde avviste i Altinn-portalen
          - 'timeout': Altinn timed out request-en (typisk etter 30 dager)
        """
        url = self._eristo_onboard_url(company)
        url_with_ref = f"{url}?ref={external_ref}"
        req = urllib.request.Request(
            url_with_ref,
            method='GET',
            headers={
                'Authorization': f'Bearer {company.l10n_no_eristo_api_key}',
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            err_body = e.read().decode()[:500]
            if e.code == 404:
                raise UserError(_(
                    "Onboarding-request '%(ref)s' finnes ikke i Eristo Token Service.",
                    ref=external_ref,
                ))
            raise UserError(_(
                "Status-sjekk feilet (HTTP %(code)s).\n%(body)s",
                code=e.code, body=err_body,
            ))
        except (urllib.error.URLError, socket.timeout) as e:
            raise UserError(_(
                "Eristo Onboarding Service ikke tilgjengelig: %(err)s",
                err=str(e)[:300],
            ))

    @api.model
    def get_access_token_for_company(self, company_id, scope):
        """RPC-callable variant brukt av debug/probe-tools.

        SIKKERHET: Krever base.group_system fordi tokenet kan brukes til
        å sende a-meldinger eller hente sensitiv informasjon. Sudo-bypass
        uten sjekk ville la en hvilken som helst bruker hente tokenet via
        XML-RPC.
        """
        if not self.env.user.has_group('base.group_system'):
            raise AccessError(_(
                "Bare brukere med 'Settings'-rettigheter kan hente "
                "Maskinporten-token via Eristo Token Service."
            ))
        company = self.env['res.company'].browse(company_id).sudo()
        return self.get_access_token(company, scope)
