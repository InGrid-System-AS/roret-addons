"""Callback-controller for ID-porten-flow.

Eristo Token Service redirecter brukeren til /idporten/done?session_id=...
&status=... etter at ID-porten-autentisering er fullført. Her:

  1. Vi finner state-bindingen i l10n.no.eristo.idporten.session
  2. Henter Altinn-token fra Eristo Token Service via api_key
  3. Kaller target_model.target_res_id.<callback_method>(
        altinn_token=..., pid=...)
  4. Redirecter brukeren tilbake til record-form

På feil: viser en HTML-feilside med beskrivelse + lenke tilbake.
"""
import logging

from markupsafe import escape

from odoo import http, fields, _
from odoo.exceptions import UserError
from odoo.http import request

_logger = logging.getLogger(__name__)


class L10nNoEristoIdPortenCallback(http.Controller):

    @http.route(
        '/idporten/done',
        type='http', auth='user', methods=['GET'], csrf=False,
    )
    def idporten_done(self, **kwargs):
        """Callback fra Eristo Token Service etter ID-porten-autentisering.

        Query-parametere:
          - session_id: UUID fra Eristo Token Service idporten_sessions-row
          - status: 'authenticated' ved suksess, ellers feilkode
          - error / error_description: ved feil

        Vi MÅ være forsiktig her:
          - state må matche en pending session i vår DB
          - session må ikke være eldre enn expires_at
          - Vi må aldri stole på query-param-data alene; sjekk DB
        """
        session_id = kwargs.get('session_id')
        state = kwargs.get('state')
        status = kwargs.get('status')
        err = kwargs.get('error')
        err_desc = kwargs.get('error_description') or ''

        # Hvis ID-porten/Token Service rapporterte feil
        if err:
            return self._render_error(
                title=_("ID-porten-innlogging feilet"),
                code=err,
                description=err_desc,
            )
        if not session_id:
            return self._render_error(
                title=_("Manglende callback-parameter"),
                code="missing_session_id",
                description=_("Token Service ga ingen session_id."),
            )
        if status != 'authenticated':
            return self._render_error(
                title=_("Uventet callback-status"),
                code=f"status_{status}",
                description=_("Forventet status=authenticated."),
            )

        # Slå opp matching session via state (1:1-binding).
        # Vi lagret state ved start_authorize_flow; Token Service
        # returnerer samme state i callback. State er kryptografisk
        # tilfeldig UUID — sikker mot å gjette/forfalske.
        if not state:
            return self._render_error(
                title=_("Manglende state-parameter"),
                code="missing_state",
                description=_("Token Service ga ingen state — kan ikke binde "
                              "callback til Odoo-record."),
            )
        # NB: IKKE filtrer på request.env.company — det er brukerens
        # default-selskap, ikke nødvendigvis selskapet flyten ble startet
        # for (flerselskaps-DB er MVP-oppsettet). Selskapet hentes fra
        # sesjonen, som ble bundet ved start_authorize_flow.
        session = request.env['l10n.no.eristo.idporten.session'].sudo().search([
            ('state', '=', state),
            ('status', '=', 'pending'),
        ], limit=1)
        if not session:
            return self._render_error(
                title=_("Session ikke funnet"),
                code="session_not_found",
                description=_("State stemmer ikke med noen ventende "
                              "innlogging. Start innsendingen på nytt fra "
                              "Odoo — hver innloggingslenke kan bare brukes "
                              "én gang."),
            )
        # Utløp: hele OIDC-flyten må fullføres innen expires_at.
        if session.expires_at and session.expires_at < fields.Datetime.now():
            session.write({'status': 'expired'})
            return self._render_error(
                title=_("Innloggingen er utløpt"),
                code="session_expired",
                description=_("Det gikk for lang tid mellom start og "
                              "fullføring. Start innsendingen på nytt."),
            )
        # Non-repudiation: kun brukeren som startet flyten kan fullføre
        # den — «utførende person» skal være personen i Odoo-sesjonen.
        if session.initiator_uid and session.initiator_uid.id != request.env.uid:
            session.write({'status': 'failed',
                           'error_code': 'wrong_user'})
            return self._render_error(
                title=_("Feil bruker"),
                code="wrong_user",
                description=_("Denne innloggingen ble startet av en annen "
                              "Odoo-bruker. Start innsendingen på nytt fra "
                              "din egen sesjon."),
            )
        company = session.company_id
        # Bind session_id til lokal session så vi kan korrelere senere
        session.write({'eristo_session_id': session_id})

        # Hent Altinn-token fra Token Service
        try:
            token_data = request.env['l10n.no.eristo.idporten.service'].sudo() \
                .fetch_altinn_token(company, session_id)
        except UserError as e:
            session.write({
                'status': 'failed',
                'error_code': 'token_fetch_failed',
                'error_message': str(e),
            })
            return self._render_error(
                title=_("Kunne ikke hente Altinn-token"),
                code="token_fetch_failed",
                description=str(e),
            )

        # Marker session som authenticated, oppdater pid + status
        session.write({
            'status': 'authenticated',
            'pid': token_data.get('pid'),
        })

        # Kall target-record sin callback-metode
        target_model = session.target_model
        target_id = session.target_res_id
        callback = session.callback_method
        if not target_model or not target_id or not callback:
            return self._render_error(
                title=_("Ugyldig callback-konfigurasjon"),
                code="invalid_target",
                description=_("Session mangler target_model/res_id/callback."),
            )

        target = request.env[target_model].browse(target_id)
        if not target.exists():
            session.write({'status': 'failed', 'error_code': 'target_not_found'})
            return self._render_error(
                title=_("Mål-record ikke funnet"),
                code="target_not_found",
                description=_("Recorden som skulle ta over flowen er slettet."),
            )

        # Re-valider allow-listen ved kjøring (forsvar i dybden — servicen
        # validerte ved opprettelse, men sesjonsraden skal ikke kunne
        # brukes som generisk kjør-metode-primitiv om den manipuleres).
        tillatte = getattr(type(target), '_idporten_callbacks', ())
        method = getattr(target, callback, None)
        if callback not in tillatte or not method or not callable(method):
            session.write({'status': 'failed', 'error_code': 'callback_missing'})
            return self._render_error(
                title=_("Callback-metode mangler"),
                code="callback_missing",
                description=_("Metode %(m)s er ikke registrert som "
                              "ID-porten-callback på %(t)s.",
                              m=callback, t=target_model),
            )

        record_url = f"/odoo/{target_model}/{target_id}"
        # Kjør callback med altinn_token + pid
        try:
            resultat = method(altinn_token=token_data['altinn_token'],
                              pid=token_data.get('pid'))
            session.write({
                'status': 'completed',
                'completed_at': fields.Datetime.now(),
            })
        except UserError as e:
            session.write({
                'status': 'failed',
                'error_code': 'callback_failed',
                'error_message': str(e),
            })
            return self._render_error(
                title=_("Innsending feilet"),
                code="callback_failed",
                description=str(e),
                back_url=record_url,
            )
        except Exception as e:
            _logger.exception("Idporten callback method raised")
            session.write({
                'status': 'failed',
                'error_code': 'callback_exception',
                'error_message': str(e)[:500],
            })
            return self._render_error(
                title=_("Intern feil"),
                code="callback_exception",
                description=str(e)[:500],
                back_url=record_url,
            )

        # Callbackens returnerte notifikasjon (display_notification-action)
        # kan ikke vises via en redirect — legg beskjeden i chatteren så
        # brukeren finner den på recorden. Viktigst for AltinnNotReady-
        # tilfellet, der beskjeden er «klikk Send inn igjen».
        if isinstance(resultat, dict):
            params = resultat.get('params') or {}
            melding = params.get('message')
            if melding and hasattr(target, 'message_post'):
                target.message_post(body=melding)

        # Suksess — deep-link rett til record-formen (/odoo/<modell>/<id>;
        # Odoo 19-routeren tar modellnavn med punktum).
        return request.redirect(record_url)

    def _render_error(self, title, code, description, back_url=None):
        """Render en enkel HTML-feilside med beskjed til brukeren.

        ALT som interpoleres escapes — error/error_description/status
        kommer rett fra query-strengen (angriperkontrollert), og
        beskrivelser kan inneholde upstream-feiltekst.
        """
        title = escape(title)
        code = escape(code)
        description = escape(description)
        back_link = (
            f'<p><a href="{escape(back_url)}">← Tilbake</a></p>'
            if back_url else ''
        )
        html = f"""<!DOCTYPE html>
<html lang="nb"><head><meta charset="utf-8"><title>{title}</title>
<style>
  body {{ font-family: sans-serif; max-width: 640px; margin: 60px auto; padding: 20px; }}
  h1 {{ color: #dc3545; }}
  .code {{ background: #f8f9fa; padding: 8px 12px; border-radius: 4px;
           font-family: monospace; font-size: 0.9em; }}
  .hint {{ color: #6c757d; font-size: 0.9em; margin-top: 24px; }}
</style></head>
<body>
<h1>{title}</h1>
<p>{description}</p>
<p>Feilkode: <span class="code">{code}</span></p>
{back_link}
<p class="hint">Gå tilbake til Odoo og prøv igjen, eller kontakt support.</p>
</body></html>"""
        return request.make_response(html, headers=[
            ('Content-Type', 'text/html; charset=utf-8'),
        ], status=200)
