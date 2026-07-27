"""Eristo ID-porten Service — generisk klient mot Token Service.

Andre moduler (skattemelding, MVA, betaling osv.) bruker dette mønsteret:

    1. Bruker klikker en knapp som krever ID-porten-autentisering
    2. Modulen kaller:
        action = env['l10n.no.eristo.idporten.service'].start_authorize_flow(
            company=record.company_id,
            target_record=record,
            callback_method='action_submit_with_idporten',
        )
        return action  # ir.actions.act_url som åpner ID-porten i samme vindu

    3. Brukeren autentiserer via BankID/MinID
    4. Eristo Token Service mottar callback, lagrer Altinn-token,
       redirecter brukeren tilbake til /idporten/done?session_id=...
    5. Vår /idporten/done-controller finner riktig record via state,
       henter Altinn-token fra Token Service, og kaller
       record.action_submit_with_idporten(altinn_token=..., pid=...).

Modulene gjør resten — submit-flyten er deres ansvar. Denne modulen
gir kun auth-broen.
"""
import json
import logging
import socket
import urllib.error
import urllib.request

from odoo import _, api, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 15


def _idporten_endpoint(company, function_name):
    """Avled ID-porten Edge Function-URL fra Token Service-URL.

    Eksisterende l10n_no_eristo_token_url peker på maskinporten-token-
    endepunktet. ID-porten-endepunktene ligger på SAMME tjeneste, så vi
    gjør string-replacement.

    Konsekvens: repointes token-URL-en, flytter ID-porten-flyten (MVA og
    skattemelding) med automatisk. Verifiser den eksplisitt ved bytte —
    en tjeneste uten ID-porten-klient konfigurert svarer 501.
    """
    if not company.l10n_no_eristo_token_url:
        raise UserError(_(
            "Eristo Token Service-URL er ikke konfigurert. "
            "Settings → Companies → Skatteetaten-tilkobling."
        ))
    return company.l10n_no_eristo_token_url.replace(
        'maskinporten-token', function_name,
    )


class L10nNoEristoIdPortenService(models.AbstractModel):
    _name = 'l10n.no.eristo.idporten.service'
    _description = 'Eristo ID-porten Service (genrisk OIDC-bro)'

    @api.model
    def start_authorize_flow(self, company, target_record, callback_method):
        """Start ID-porten-flow for en gitt record + callback.

        Args:
            company: res.company (henter token-URL og api_key herfra)
            target_record: Odoo-record som skal motta callback (eks.
                l10n.no.skattemelding-record). MÅ ha en metode med navn
                ``callback_method``.
            callback_method: navn på metode som vil bli kalt på record-en
                etter at brukeren er tilbake fra ID-porten. Signatur:
                    record.<callback_method>(altinn_token=..., pid=...)

        Returns:
            dict – ir.actions.act_url som åpner ID-porten authorize-URL
            i samme vindu. (Brukeren navigeres dit; etter autentisering
            redirecter Digdir → Eristo Token Service → kundens Odoo
            /idporten/done.)
        """
        # Allow-list: mål-modellen må EKSPLISITT deklarere callbacken i
        # klasseattributtet _idporten_callbacks. Uten dette ville en
        # RPC-nåbar start_authorize_flow vært en generisk «autentiser og
        # kjør vilkårlig metode»-primitiv.
        tillatte = getattr(type(target_record), '_idporten_callbacks', ())
        if callback_method not in tillatte:
            raise UserError(_(
                "Metoden '%(m)s' på %(t)s er ikke registrert som "
                "ID-porten-callback (mangler i _idporten_callbacks).",
                m=callback_method, t=target_record._name,
            ))
        if not company.l10n_no_eristo_api_key:
            raise UserError(_(
                "Eristo Token Service: API-key mangler på selskapet. "
                "Settings → Companies → Skatteetaten-tilkobling."
            ))

        # Bygg return_url som peker tilbake til kundens Odoo.
        # Vi inkluderer record-info som queryparam (ctx) slik at vår
        # controller vet hvilken record som skal ta over etter callback.
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url')
        if not base_url:
            raise UserError(_(
                "Odoo 'web.base.url' er ikke satt — kan ikke bygge "
                "callback-URL for ID-porten."
            ))
        # ctx er bevisst kort/opaque — selve bind-mappingen lagres i DB.
        return_url = f"{base_url}/idporten/done"

        # Kall Token Service idporten-authorize-init
        endpoint = _idporten_endpoint(company, 'idporten-authorize-init')
        body = json.dumps({'return_url': return_url}).encode('utf-8')
        req = urllib.request.Request(
            endpoint,
            data=body,
            method='POST',
            headers={
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {company.l10n_no_eristo_api_key}',
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
                payload = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            err_body = e.read().decode()[:500]
            _logger.error(
                "idporten-authorize-init HTTP %s: %s", e.code, err_body,
            )
            raise UserError(_(
                "Kunne ikke starte ID-porten-flow (HTTP %(c)s):\n%(b)s",
                c=e.code, b=err_body,
            ))
        except (urllib.error.URLError, socket.timeout) as e:
            raise UserError(_(
                "Eristo Token Service ikke tilgjengelig: %(e)s",
                e=str(e)[:300],
            ))

        authorize_url = payload.get('authorize_url')
        state = payload.get('state')
        if not authorize_url or not state:
            raise UserError(_(
                "Eristo Token Service returnerte uventet respons: "
                "%(p)s", p=str(payload)[:300],
            ))

        # Lagre session-binding lokalt. initiator_uid settes eksplisitt:
        # create skjer via sudo, så create_uid ville blitt superuser —
        # og callbacken skal kun kunne fullføres av brukeren som startet.
        self.env['l10n.no.eristo.idporten.session'].sudo().create({
            'eristo_session_id': '',  # populeres når Token Service redirecter
            'state': state,
            'target_model': target_record._name,
            'target_res_id': target_record.id,
            'callback_method': callback_method,
            'company_id': company.id,
            'status': 'pending',
            'initiator_uid': self.env.uid,
        })

        return {
            'type': 'ir.actions.act_url',
            'url': authorize_url,
            'target': 'self',
        }

    @api.model
    def fetch_altinn_token(self, company, eristo_session_id):
        """Hent Altinn-token fra Eristo Token Service.

        Kalles av /idporten/done-controlleren etter at brukeren er
        tilbake fra ID-porten. Returnerer dict med altinn_token + pid.
        """
        if not company.l10n_no_eristo_api_key:
            raise UserError(_("API-key mangler"))
        endpoint = _idporten_endpoint(company, 'idporten-fetch-token')
        body = json.dumps({'session_id': eristo_session_id}).encode('utf-8')
        req = urllib.request.Request(
            endpoint,
            data=body,
            method='POST',
            headers={
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {company.l10n_no_eristo_api_key}',
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
                payload = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            err_body = e.read().decode()[:1000]
            _logger.error(
                "idporten-fetch-token HTTP %s: %s", e.code, err_body,
            )
            try:
                err_data = json.loads(err_body)
            except (json.JSONDecodeError, ValueError):
                err_data = {}
            raise UserError(_(
                "Kunne ikke hente Altinn-token (HTTP %(c)s).\n"
                "Feilkode: %(code)s\n%(b)s",
                c=e.code,
                code=err_data.get('error', 'unknown'),
                b=err_data.get('hint') or err_body,
            ))
        except (urllib.error.URLError, socket.timeout) as e:
            raise UserError(_(
                "Eristo Token Service ikke tilgjengelig: %(e)s",
                e=str(e)[:300],
            ))

        altinn_token = payload.get('altinn_token')
        pid = payload.get('pid')
        if not altinn_token:
            raise UserError(_(
                "Eristo Token Service returnerte uten altinn_token: %(p)s",
                p=str(payload)[:300],
            ))
        return {
            'altinn_token': altinn_token,
            'pid': pid,
            'expires_at': payload.get('expires_at'),
        }
