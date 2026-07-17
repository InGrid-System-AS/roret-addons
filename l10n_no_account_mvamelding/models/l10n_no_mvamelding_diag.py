"""Read-only Altinn-diagnostikk for mva-melding (support-verktøy).

Sender INGENTING inn. Kun GET-kall + dekoding av JWT-claims (uten signatur-
validering) for å fastslå om en innsending henger pga. (a) Skatteetatens
backend som ikke har fastsatt ennå, (b) autorisasjon/scope, eller (c) en feil
Altinn rapporterer i event-loggen. Resultatet skrives til ``last_response``.
"""
import base64
import json
import logging

import requests

from odoo import models

from .l10n_no_mvamelding import SCOPE_INNSENDING
from .l10n_no_mvamelding_submit import _ALTINN_PLATFORM_HOSTS, _resolve_env

_logger = logging.getLogger(__name__)


def _jwt_claims(token):
    """Dekod payload-delen av en JWT (uten signatur-validering) → dict."""
    parts = (token or '').split('.')
    if len(parts) < 2:
        return {}
    pad = parts[1] + '=' * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(pad))
    except (ValueError, TypeError):
        return {}


class L10nNoMvamelding(models.Model):
    _inherit = 'l10n.no.mvamelding'

    def action_diagnose_altinn(self):
        """Samle read-only diagnostikk om innsendingen og skriv til last_response."""
        self.ensure_one()
        eristo = self.env['l10n.no.eristo.service']
        out = []

        # 1. Maskinporten-token-claims: scope + authorization_details forteller
        #    hva systembrukeren faktisk er autorisert for (fastsetting vs kun
        #    innsending). Vi viser KUN claims, aldri selve tokenet.
        try:
            mp = eristo.get_access_token(self.company_id, scope=SCOPE_INNSENDING)
            claims = _jwt_claims(mp)
            relevant = {k: claims.get(k) for k in (
                'scope', 'authorization_details', 'consumer', 'supplier',
                'client_id', 'iss', 'exp') if k in claims}
            out.append("### maskinporten-claims\n"
                       + json.dumps(relevant, ensure_ascii=False, indent=1)[:2500])
        except Exception as e:  # noqa: BLE001
            out.append(f"### maskinporten-claims → EXC {str(e)[:400]}")

        # 2. Altinn-kall (alle GET).
        try:
            token = self._get_altinn_token()
        except Exception as e:  # noqa: BLE001
            out.append(f"### altinn-token → EXC {str(e)[:400]}")
            self.last_response = '\n\n'.join(out)
            return True

        base = self._altinn_instance_base()
        hdr = {'Authorization': f'Bearer {token}', 'Accept': 'application/json'}

        def _get(label, url, summarize_instance=False):
            try:
                r = requests.get(url, headers=hdr, timeout=30)
                body = r.text or ''
                if summarize_instance:
                    try:
                        j = r.json()
                        body = json.dumps({
                            'process': j.get('process'),
                            'status': j.get('status'),
                            'dataTypes': [d.get('dataType')
                                          for d in (j.get('data') or [])],
                        }, ensure_ascii=False)
                    except ValueError:
                        pass
                out.append(f"### {label} → HTTP {r.status_code}\n{body[:2000]}")
            except Exception as e:  # noqa: BLE001
                out.append(f"### {label} → EXC {str(e)[:300]}")

        _get('feedback/status', f'{base}/feedback/status')
        _get('feedback', f'{base}/feedback')
        _get('instance', base, summarize_instance=True)

        # 3. Altinn storage event-logg: viser om Skatteetatens backend har
        #    rørt instansen (process-events, created osv) eller stoppet.
        platform = _ALTINN_PLATFORM_HOSTS[_resolve_env(self.company_id)]
        ev_url = (f"{platform}/storage/api/v1/instances/"
                  f"{self.altinn_instance_owner_party_id}/"
                  f"{self.altinn_instance_guid}/events")
        _get('events', ev_url)

        self.last_response = '\n\n'.join(out)
        return True
