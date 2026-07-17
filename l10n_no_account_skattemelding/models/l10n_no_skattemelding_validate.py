"""Skatteetaten skattemelding HTTP-klient.

Tre endepunkter brukes:

  GET /api/skattemelding/v2/{aar}/{orgnr}
    — Henter gjeldende skattemelding-utkast. Vi parser respons for å
      få partsnummer + dokumentidentifikator som senere XML-bygging
      og /valider-kall trenger.

  POST /api/skattemelding/v2/valider/{aar}/{orgnr}
    — Validerer XML mot publisert utkast. Krever dokumentreferanseTilGjeldendeDokument.

  POST /api/skattemelding/v2/validertest/{aar}/{orgnr}
    — Validerer XML uten utkast-referanse. Brukes for iterativ utvikling
      før vi rører Altinn-flyten.

Spec: github.com/Skatteetaten/skattemeldingen/blob/master/docs/api-v2/README.md
"""
import base64
import json
import logging
import re
import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

_SKATTEMELDING_HOSTS = {
    'test': 'https://api-test.sits.no',
    'prod': 'https://api.skatteetaten.no',
}
# API-base per Skatteetatens spec:
# https://github.com/Skatteetaten/skattemeldingen/blob/master/docs/api-v2/README.md
# Det 406-svaret vi fikk 2026-05-08 inneholdt "instance":"/api/formueinntekt/..."
# i feilmeldingen, men det er Skatteetatens INTERNE rute-resolution. Den
# offentlige API-pathen er /api/skattemelding/v2/* — validertest-endpointet
# eksisterer kun under denne pathen.
_SKATTEMELDING_API_BASE = '/api/skattemelding/v2'
_SKATTEMELDING_SCOPE = 'skatteetaten:formueinntekt/skattemelding'

# Timeouts: GET er rask, POST kan ta tid på XSD-validering hos Skatteetaten.
_HTTP_GET_TIMEOUT = 30
_HTTP_POST_TIMEOUT = 60


def _resolve_host(company):
    """Returner Skatteetaten-host basert på company-environment.

    Raiser klar UserError istedenfor KeyError hvis verdien ikke er
    'test' eller 'prod' (typo i config, eller fremtidig miljø).
    """
    env_key = company.l10n_no_eristo_environment or 'test'
    if env_key not in _SKATTEMELDING_HOSTS:
        raise UserError(_(
            "Ukjent Skatteetaten-miljø '%(env)s' for selskap %(name)s. "
            "Forventet 'test' eller 'prod'. Sjekk konfigurasjon på selskapet.",
            env=env_key, name=company.name,
        ))
    return _SKATTEMELDING_HOSTS[env_key]


class L10nNoSkattemelding(models.Model):
    _inherit = 'l10n.no.skattemelding'

    def action_l10n_no_skattemelding_fetch_partsnummer(self):
        """Hent partsnummer + dokumentidentifikator via hentGjeldende-endpoint.

        Skatteetaten har en intern parts-ID for hvert AS som kreves i XML-en
        (xsd:long, typisk 10 sifre — ikke samme som orgnr). Vi henter den
        fra deres GET-endpoint og lagrer på record-en.

        Hvis endpoint returnerer 404 betyr det at Skatteetaten ikke har et
        utkast for året enda — da må partsnummer settes manuelt fra
        Skatteetatens portal.
        """
        self.ensure_one()
        company = self.company_id
        host = _resolve_host(company)
        eristo = self.env['l10n.no.eristo.service']
        orgnr = eristo._orgnr(company)
        token = eristo.get_access_token(company, scope=_SKATTEMELDING_SCOPE)

        url = f'{host}{_SKATTEMELDING_API_BASE}/{self.inntektsaar}/{orgnr}'
        try:
            resp = requests.get(
                url,
                headers={
                    'Accept': 'application/xml',
                    'Authorization': f'Bearer {token}',
                },
                timeout=_HTTP_GET_TIMEOUT,
            )
        except requests.RequestException as e:
            raise UserError(_(
                "Skatteetaten ikke tilgjengelig: %(err)s",
                err=str(e)[:300],
            ))
        # Force UTF-8: requests defaulter til Latin-1 for text/xml uten
        # Content-Type charset. Skatteetaten sender UTF-8 (deklarert i
        # XML-headeren). Uten dette får norske tegn mojibake.
        resp.encoding = 'utf-8'
        if resp.status_code >= 400:
            err_body = resp.text[:1000]
            if resp.status_code == 404:
                raise UserError(_(
                    "Skatteetaten har ikke utkast for %(aar)d for orgnr %(orgnr)s. "
                    "Du må enten vente til utkast publiseres eller fylle inn "
                    "partsnummer manuelt fra Skatteetatens portal "
                    "(skatteetaten.no → Logg inn → Skattemelding).",
                    aar=self.inntektsaar, orgnr=orgnr,
                ))
            raise UserError(_(
                "Kunne ikke hente skattemelding (HTTP %(code)s).\n%(body)s",
                code=resp.status_code, body=err_body,
            ))
        resp_body = resp.text

        partsnummer, dokumentid = self._extract_parts_from_response(resp_body)
        if not partsnummer:
            raise UserError(_(
                "Skatteetaten responderte men partsnummer ble ikke funnet i "
                "respons-strukturen. Rapporter dette til Eristo support med "
                "siste_respons-innholdet (lagret nedenfor).\n\nRespons:\n%(body)s",
                body=resp_body[:1000],
            ))

        vals = {
            'partsnummer': partsnummer,
            'last_response': resp_body,
        }
        if dokumentid:
            vals['dokumentidentifikator'] = dokumentid
        self.write(vals)

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'success',
                'title': _("Partsnummer hentet"),
                'message': _(
                    "partsnummer=%(p)s%(d)s. Klar for å generere XML.",
                    p=partsnummer,
                    d=(_(", dokumentidentifikator=%(d)s", d=dokumentid)
                       if dokumentid else ''),
                ),
                'sticky': False,
                'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
            },
        }

    def _extract_parts_from_response(self, resp_body):
        """Trekk ut partsnummer + dokumentidentifikator fra hentGjeldende-svar.

        Skatteetatens response (verifisert 2026-05-08) er XML-konvolutt:
          <skattemeldingOgNaeringsspesifikasjonResponse>
            <dokumenter>
              <skattemeldingdokument>
                <type>skattemelding</type>
                <encoding>UTF-8</encoding>
                <content>BASE64...</content>
              </skattemeldingdokument>
            </dokumenter>
            <dokumentidentifikator>SKI-xxx</dokumentidentifikator>
            ...
          </skattemeldingOgNaeringsspesifikasjonResponse>

        Vi parser konvolutten med lxml, base64-dekoder content, og regex-er
        ut <partsnummer> fra inner-XML. resp_body kan også være JSON
        (fallback for hvis API endrer format) — da brukes dict-traversal.
        """
        partsnummer = None
        dokumentid = None

        if not resp_body:
            return None, None

        # Forsøk JSON først hvis det åpenbart starter med {
        if isinstance(resp_body, dict):
            parsed = resp_body
        elif isinstance(resp_body, str) and resp_body.lstrip().startswith('{'):
            try:
                parsed = json.loads(resp_body)
            except json.JSONDecodeError:
                parsed = None
        else:
            parsed = None

        if isinstance(parsed, dict):
            dokumenter = parsed.get('dokumenter') or {}
            sme_dok = (dokumenter.get('skattemeldingdokument')
                       or dokumenter.get('skattemelding'))
            if isinstance(sme_dok, dict):
                dokumentid = (sme_dok.get('id')
                              or sme_dok.get('dokumentidentifikator'))
                content_b64 = sme_dok.get('content')
                if content_b64:
                    try:
                        xml = base64.b64decode(content_b64).decode(
                            'utf-8', errors='ignore',
                        )
                        m = re.search(r'<partsnummer>(\d+)</partsnummer>', xml)
                        if m:
                            partsnummer = m.group(1)
                    except (ValueError, UnicodeDecodeError):
                        pass
            if not partsnummer:
                partsnummer = parsed.get('partsnummer')
            return partsnummer, dokumentid

        # XML-respons (faktisk format mot Skatteetaten 2026)
        if isinstance(resp_body, str):
            # dokumentidentifikator: faktisk respons bruker <id>X</id>
            # innenfor <skattemeldingdokument>, IKKE
            # <dokumentidentifikator>. Format verifisert mot Skatteetaten
            # forespoersel-response 2026-05-15.
            m_dokid = re.search(
                r'<(?:[a-zA-Z0-9]+:)?skattemeldingdokument>\s*'
                r'<(?:[a-zA-Z0-9]+:)?id>([^<]+)</(?:[a-zA-Z0-9]+:)?id>',
                resp_body,
            )
            if not m_dokid:
                # Fallback: prøv legacy <dokumentidentifikator>-tag for
                # eldre Skatteetaten-respons-format
                m_dokid = re.search(
                    r'<(?:[a-zA-Z0-9]+:)?dokumentidentifikator>'
                    r'([^<]+)'
                    r'</(?:[a-zA-Z0-9]+:)?dokumentidentifikator>',
                    resp_body,
                )
            if m_dokid:
                dokumentid = m_dokid.group(1).strip()

            # Finn første <content>...</content> = base64 inner-skattemelding
            m_content = re.search(
                r'<(?:[a-zA-Z0-9]+:)?content>([^<]+)</(?:[a-zA-Z0-9]+:)?content>',
                resp_body,
            )
            if m_content:
                try:
                    inner_xml = base64.b64decode(
                        m_content.group(1).strip(),
                    ).decode('utf-8', errors='ignore')
                    m_parts = re.search(
                        r'<(?:[a-zA-Z0-9]+:)?partsnummer>(\d+)'
                        r'</(?:[a-zA-Z0-9]+:)?partsnummer>',
                        inner_xml,
                    )
                    if m_parts:
                        partsnummer = m_parts.group(1)
                except (ValueError, UnicodeDecodeError):
                    pass

        return partsnummer, dokumentid

    def action_l10n_no_skattemelding_build_xml(self):
        """Generer alle 3 XML-er fra dagens regnskapsdata.

        Phase 1: minimum-XML (uten resultat/balanse). Phase 2 vil hente
        fra account.move med NS 4102-mapping.
        """
        self.ensure_one()
        xml_svc = self.env['l10n.no.skattemelding.xml.service']
        # Build_skattemelding_xml + build_naeringsspesifikasjon_xml kan
        # raise UserError (manglende partsnummer mm.) — la den boble opp.
        sme_xml = xml_svc.build_skattemelding_xml(self)
        nsp_xml = xml_svc.build_naeringsspesifikasjon_xml(self)

        # Reset state: hvis vi er i 'feilet' eller hvor som helst non-final,
        # bygging av ny XML betyr "starter validering på nytt".
        new_state = self.state
        if self.state in ('draft', 'feilet', 'built', 'validated'):
            new_state = 'built'

        # Hvis vi allerede har lastet opp til Altinn (state='uploaded'),
        # signaliser at lokal XML har drevet fra Altinn-utkastet.
        # Brukeren får banner + "Send oppdatert utkast"-knapp.
        # State beholdes som 'uploaded' fordi instansen fortsatt finnes
        # i Altinn — vi vil bare oppdatere data-elementet i den.
        write_vals = {
            'skattemelding_xml': sme_xml,
            'naeringsspesifikasjon_xml': nsp_xml,
            'state': new_state,
            'xml_generated_at': fields.Datetime.now(),
        }
        if self.state == 'uploaded':
            write_vals['requires_resend'] = True
        self.write(write_vals)
        # Konvolutten må også bygges — feiler den, må vi vite det tidlig.
        self.konvolutt_xml = xml_svc.build_konvolutt_xml(self)

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'success',
                'title': _("XML generert"),
                'message': _("Skattemelding + næringsspesifikasjon + konvolutt XML "
                             "klar. Bruk 'Valider mot Skatteetaten' for å sjekke "
                             "før innsending."),
                'sticky': False,
                'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
            },
        }

    def action_l10n_no_skattemelding_validate(self):
        """Send XML til Skatteetaten validertest-endpoint og parse svaret.

        Skatteetaten returnerer en respons med eventuelle avvik. Vi parser
        og lagrer last_response så bruker kan inspisere; state oppdateres
        til 'validated' (0 avvik) eller 'feilet' (1+ blokkerende avvik).
        """
        self.ensure_one()
        if not self.skattemelding_xml or not self.naeringsspesifikasjon_xml:
            raise UserError(_(
                "Ingen XML å validere — kjør 'Generer XML' først."
            ))
        if not self.konvolutt_xml:
            xml_svc = self.env['l10n.no.skattemelding.xml.service']
            self.konvolutt_xml = xml_svc.build_konvolutt_xml(self)

        company = self.company_id
        host = _resolve_host(company)
        eristo = self.env['l10n.no.eristo.service']
        orgnr = eristo._orgnr(company)
        token = eristo.get_access_token(company, scope=_SKATTEMELDING_SCOPE)

        # validertest brukes så vi ikke trenger dokumentreferanseTilGjeldendeDokument.
        # Når Phase 5 har Altinn-flyt klar, bytter vi til 'valider' med ekte
        # dokumentidentifikator hentet fra hentGjeldende.
        url = f'{host}{_SKATTEMELDING_API_BASE}/validertest/{self.inntektsaar}/{orgnr}'
        try:
            resp = requests.post(
                url,
                data=self.konvolutt_xml.encode('utf-8'),
                headers={
                    'Content-Type': 'application/xml',
                    'Accept': 'application/xml',
                    'Authorization': f'Bearer {token}',
                },
                timeout=_HTTP_POST_TIMEOUT,
            )
        except requests.RequestException as e:
            raise UserError(_(
                "Skatteetaten ikke tilgjengelig: %(err)s",
                err=str(e)[:300],
            ))
        # Force UTF-8: requests defaulter til Latin-1 for text/xml uten
        # Content-Type charset. Skatteetaten sender UTF-8.
        resp.encoding = 'utf-8'
        if resp.status_code >= 400:
            err_body = resp.text[:4000]
            _logger.error(
                "Skattemelding-validering feilet HTTP %s: %s",
                resp.status_code, err_body,
            )
            self.write({
                'state': 'feilet',
                'last_response': err_body,
            })
            raise UserError(_(
                "Validering feilet (HTTP %(code)s).\n%(body)s",
                code=resp.status_code, body=err_body[:1000],
            ))
        resp_body = resp.text

        # Skatteetaten returnerer XML per spec (avvikVedValidering /
        # avvikEtterBeregning-konvolutter). _count_avvik_in_response
        # tar rå body og håndterer både XML og JSON-dict.
        avvik_count = self._count_avvik_in_response(resp_body)

        # avvik_count = None betyr ukjent respons-format. Vi nekter å si
        # "validert OK" da — bedre å la bruker inspisere selv enn å lyve.
        if avvik_count is None:
            self.write({
                'state': 'feilet',
                'last_response': resp_body,
            })
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    # 'danger' (rød) matcher state='feilet' — 'warning' (gul)
                    # gjorde brukeren usikker på om det egentlig var en feil.
                    'type': 'danger',
                    'title': _("Ukjent respons-format fra Skatteetaten"),
                    'message': _(
                        "Validering returnerte 200 OK men responsstrukturen "
                        "matcher ingen kjente formater. Skattemeldingen er "
                        "satt til 'feilet'. Inspiser 'Validering'-fanen og "
                        "rapporter til Eristo support hvis dette gjentar seg."
                    ),
                    'sticky': True,
                    'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
                },
            }

        new_state = 'validated' if avvik_count == 0 else 'feilet'
        self.write({
            'state': new_state,
            'last_response': resp_body,
        })

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'success' if avvik_count == 0 else 'warning',
                'title': (
                    _("Validering OK — 0 avvik") if avvik_count == 0
                    else _("Validering: %(n)d avvik", n=avvik_count)
                ),
                'message': (
                    _("Skatteetaten godtok payload. Klar for innsending via Altinn.")
                    if avvik_count == 0
                    else _("Sjekk 'Validering'-fanen for detaljer om avvikene.")
                ),
                'sticky': True,
                'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
            },
        }

    def _count_avvik_in_response(self, resp_body):
        """Tell antall feil-elementer i validerings-svaret.

        Returnerer:
          - 0  → eksplisitt godkjent (resultatAvValidering=validertOK)
          - n>0 → n feil funnet (avvik + veiledning med betjeningsstrategi=
                   faktiskFeil + når validertMedFeil men ingen elementer
                   matcher: minst 1 for ikke å miste signalet)
          - None → respons-format matcher ingen kjent struktur (caller skal
                   IKKE behandle dette som suksess)

        Skatteetatens spec sier responsen er XML med:
          - <resultatAvValidering> ∈ {validertOK | validertMedFeil}
          - <avvikVedValidering>/<avvik> — blokkerende valideringsfeil
          - <avvikEtterBeregning>/<avvik> — blokkerende beregningsfeil
          - <veiledningEtterKontroll>/<veiledning> — råd ELLER faktiske
            feil (sjekk <betjeningsstrategi>: 'faktiskFeil' vs
            'merknadStandard')

        E2E mot TT02 viste at Skatteetaten kan sette validertMedFeil
        UTEN <avvik>-elementer, men med <veiledning>...<betjeningsstrategi>
        faktiskFeil</betjeningsstrategi>. Vi må derfor lese
        resultatAvValidering først — det er det autoritative signalet.
        """
        if not resp_body:
            return None

        # JSON/dict-respons (legacy fallback)
        if isinstance(resp_body, dict):
            return self._count_avvik_in_dict(resp_body)
        if isinstance(resp_body, str) and resp_body.lstrip().startswith('{'):
            try:
                return self._count_avvik_in_dict(json.loads(resp_body))
            except json.JSONDecodeError:
                return None

        # XML-respons (faktisk format)
        if not isinstance(resp_body, str):
            return None
        body = resp_body.lstrip()
        if not body.startswith('<'):
            return None

        # Verifiser at vi har en gjenkjennelig respons-rot. Hvis ikke,
        # er det sannsynligvis HTML-feilside og vi returnerer None.
        if 'skattemeldingOgNaeringsspesifikasjonResponse' not in body:
            return None

        # Tell <avvik>-elementer (blokkerende). Skatteetaten kan emitte
        # med eller uten ns-prefiks. Selv-lukkende <avvik/> teller også.
        avvik_count = len(re.findall(
            r'<(?:[a-zA-Z0-9]+:)?avvik(?:\s[^>]*)?(?:/>|>)',
            body,
        ))

        # Tell <veiledning>-elementer som har betjeningsstrategi=faktiskFeil.
        # Dette er reelle feil emittet via veilednings-mekanismen heller enn
        # avvik. Pure 'merknadStandard'-veiledninger ignoreres her (de er
        # rene råd; man kan submitte uten å fikse).
        faktisk_feil_count = len(re.findall(
            r'<(?:[a-zA-Z0-9]+:)?betjeningsstrategi>\s*faktiskFeil\s*'
            r'</(?:[a-zA-Z0-9]+:)?betjeningsstrategi>',
            body,
        ))

        # Autoritativt signal: resultatAvValidering. Hvis validertOK, så er
        # alt OK uavhengig av hva vi telte (eventuelle veiledninger er rene
        # råd). Hvis validertMedFeil, returner minst 1 selv om vi ikke fant
        # individuelle elementer — for ikke å lyve som 'validert OK'.
        result_match = re.search(
            r'<(?:[a-zA-Z0-9]+:)?resultatAvValidering>\s*'
            r'(validertOK|validertMedFeil)\s*'
            r'</(?:[a-zA-Z0-9]+:)?resultatAvValidering>',
            body,
        )
        if result_match:
            if result_match.group(1) == 'validertOK':
                return 0
            # validertMedFeil — returner total (minst 1 så caller ikke lyver)
            return max(1, avvik_count + faktisk_feil_count)

        # Ingen resultatAvValidering — fall tilbake på antall avvik
        return avvik_count + faktisk_feil_count

    def _count_avvik_in_dict(self, parsed):
        """Tell avvik i JSON-dict (legacy fallback for hvis API endrer format)."""
        if not isinstance(parsed, dict):
            return None

        status = parsed.get('status')
        if isinstance(status, str) and status.lower() in ('ok', 'godkjent', 'validert'):
            return 0

        for key in ('resultatAvValidering', 'avvik', 'valideringsfeil', 'feil'):
            v = parsed.get(key)
            if isinstance(v, list):
                return len(v)
            if isinstance(v, dict):
                inner = v.get('avvik')
                if isinstance(inner, list):
                    return len(inner)
                if not v:
                    return 0

        return None
