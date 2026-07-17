"""Kvittering-fetch + arkivering for skattemelding-innsending.

Etter at bruker har signert i Altinn-portalen og Skatteetaten har
behandlet innsendingen, må vi:
  1. Hente kvittering (Altinn /instances/...)
  2. Hente Skatteetatens tilbakemelding.xml (vedtak godkjent/avvist)
  3. Arkivere alt som ir.attachment + post i chatter
  4. Polle automatisk via cron

Splittet ut fra l10n_no_skattemelding_submit.py (P3 #8) for å holde
hver fil under ~1000 linjer. Submit-flyt (token-exchange, instance-
creation, upload, process/next) bor fortsatt i submit-filen.
"""
import base64
import json
import logging
from datetime import date, timedelta

import requests
from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import format_datetime

from .l10n_no_skattemelding_submit import (
    _HTTP_TIMEOUT,
    _SKATTEMELDING_APP_ID,
    _SKATTEMELDING_SCOPE,
    _resolve_altinn_app,
    _resolve_altinn_platform,
)

_logger = logging.getLogger(__name__)


class L10nNoSkattemelding(models.Model):
    _inherit = 'l10n.no.skattemelding'

    # ---------- Receipt-polling ----------

    def action_l10n_no_skattemelding_fetch_receipt(self):
        """Hent mottakskvittering fra Altinn etter innsending.

        Etter at prosessen er endet (state='submitted') kan vi GET
        instansen og hente metadata om kvittering. Endpoint returnerer
        full instans m. processState + dataElements.
        """
        self.ensure_one()
        if self.state not in ('uploaded', 'submitted', 'mottatt'):
            raise UserError(_(
                "Kan kun hente kvittering for innsendte meldinger. "
                "Nåværende status: %(s)s", s=self.state,
            ))
        if not self.altinn_instance_guid:
            raise UserError(_(
                "Mangler Altinn instance-GUID — kan ikke hente kvittering."
            ))

        company = self.company_id
        eristo = self.env['l10n.no.eristo.service']
        maskinporten_token = eristo.get_access_token(
            company, scope=_SKATTEMELDING_SCOPE,
        )
        altinn_token = self._exchange_to_altinn_token(maskinporten_token)

        app_host = _resolve_altinn_app(company)
        url = (
            f'{app_host}/{_SKATTEMELDING_APP_ID}/instances/'
            f'{self.altinn_instance_owner_party_id}/'
            f'{self.altinn_instance_guid}'
        )
        try:
            resp = requests.get(
                url,
                headers={
                    'Authorization': f'Bearer {altinn_token}',
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
                "Kunne ikke hente kvittering (HTTP %(code)s).\n%(body)s",
                code=resp.status_code, body=resp.text[:1000],
            ))
        body = resp.text

        # Tolking av Altinn-prosesstilstand:
        #   - currentTask.elementId='Task_1' → bruker har ikke signert ennå
        #     (state forblir 'uploaded')
        #   - currentTask=null + process.ended IKKE satt → feedback-fase
        #     (Skatteetaten behandler — state='submitted')
        #   - process.ended satt → ferdig (state='mottatt')
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = {}
        process = data.get('process') or {}
        current_task_info = process.get('currentTask') or {}
        current_task = current_task_info.get('elementId')
        process_ended = process.get('ended')

        # P1 #2: skriv også til dedikert felt altinn_instance_json slik at
        # vi slipper å parse last_response. last_response beholdes for
        # diagnostisk visning (Validering-fanen).
        vals = {'last_response': body, 'altinn_instance_json': body}
        if process_ended:
            # Ferdig — bruker har signert OG Skatteetaten har behandlet
            if self.state != 'mottatt':
                vals['state'] = 'mottatt'
                vals['mottatt_at'] = fields.Datetime.now()
                # Auto-lås regnskapsåret når Skatteetaten har akseptert.
                # Wrap i try/except: hvis lock-write feiler (eks: bruker
                # uten company-write-rett, eller eksisterende lock-date
                # er senere), skal ikke det blokkere kvittering-arkivering.
                try:
                    self._auto_lock_fiscalyear()
                except Exception as e:
                    _logger.warning(
                        "Skattemelding %s: kunne ikke auto-låse "
                        "regnskapsår (kvittering arkiveres uansett): %s",
                        self.id, e,
                    )
            if not self.submitted_at:
                vals['submitted_at'] = fields.Datetime.now()
        elif current_task is None:
            # I feedback-fase: bruker har signert, venter på Skatteetaten
            if self.state in ('uploaded', 'validated'):
                vals['state'] = 'submitted'
                vals['submitted_at'] = fields.Datetime.now()
        elif current_task == 'Task_1':
            # Fortsatt på første task — bruker har ikke signert ennå
            # State forblir 'uploaded' (ingen endring her)
            pass
        if current_task:
            vals['altinn_process_current_task'] = current_task
        self.write(vals)

        # Auto-fetch tilbakemelding.xml hvis prosess er endet og det finnes
        # et tilbakemelding-dataElement. Dette gir Skatteetatens faktiske
        # vedtak (godkjent/avvist) som er kritisk for korrekt UX.
        if process_ended:
            has_tilbakemelding = any(
                d.get('dataType') == 'tilbakemelding'
                for d in (data.get('data') or [])
            )
            if has_tilbakemelding and not self.tilbakemelding_xml:
                try:
                    self.action_l10n_no_skattemelding_fetch_tilbakemelding()
                except Exception as e:
                    # Auto-fetch er best-effort — bruker kan trigge manuell
                    # fetch fra GUI. exc_info=True for full stack trace
                    # i log slik at support kan diagnostisere (P2 #4).
                    _logger.warning(
                        "Skattemelding %s: kunne ikke auto-hente "
                        "tilbakemelding: %s", self.id, str(e)[:200],
                        exc_info=True,
                    )

            # Auto-arkivér kvittering som ir.attachment + post i chatter
            # når Skatteetaten har behandlet innsendingen. Dette gir
            # regnskapsfører dokumentasjon for revisjon og audit-trail.
            try:
                self._archive_kvittering_and_post()
            except Exception as e:
                # Arkivering er best-effort audit-trail — feil her må
                # ikke blokkere kvittering-fetch. exc_info=True for
                # diagnostikk (P2 #4).
                _logger.warning(
                    "Skattemelding %s: kunne ikke arkivere kvittering: %s",
                    self.id, str(e)[:200],
                    exc_info=True,
                )

        # Bestem notification-type basert på Skatteetatens vedtak
        # (etter at compute har kjørt på den nye last_response)
        self.invalidate_recordset(['skatteetaten_endelig_status'])
        endelig = self.skatteetaten_endelig_status if process_ended else 'pending'
        notification_type = {
            'godkjent': 'success',
            'avvist': 'danger',
            'ukjent': 'warning',
            'pending': 'warning',
        }.get(endelig, 'warning')
        title = {
            'godkjent': _("Skattemelding godkjent"),
            'avvist': _("Skattemelding avvist"),
            'ukjent': _("Mottatt — status ukjent"),
            'pending': _("Kvittering ikke klar ennå"),
        }.get(endelig)
        message = {
            'godkjent': _("Skatteetaten har godkjent innsendingen "
                          "(resultatAvValidering=validertOK)."),
            'avvist': _("Skatteetaten avviste innsendingen. Se "
                        "Validering-fanen for årsak og full tilbakemelding."),
            'ukjent': _("Altinn har arkivert instansen, men vi kunne ikke "
                        "tolke Skatteetatens vedtak. Sjekk Validering-fanen."),
            'pending': _("Altinn-instans eksisterer men prosessen er ikke "
                         "endet. Prøv igjen om noen minutter."),
        }.get(endelig)

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': notification_type,
                'title': title,
                'message': message,
                'sticky': True,
                'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
            },
        }

    # ---------- Auto-lås av regnskapsår ved 'mottatt' ----------

    def _auto_lock_fiscalyear(self):
        """Lås selskapets fiscalyear_lock_date + tax_lock_date til 31.12
        av inntektsåret når skattemeldingen er mottatt av Skatteetaten.

        Sikrer revisjonsspor: ingen kan retroaktivt endre regnskapsdata
        for året vi nettopp har rapportert. Idempotent — kjører bare
        hvis eksisterende lock-date er TIDLIGERE enn 31.12 inntektsår.

        Hvis selskapet allerede har en LATER lock-date (eks: 2026-12-31
        låst tidligere), gjør vi ingen endring — vi skal ikke åpne opp.
        """
        self.ensure_one()
        company = self.company_id
        if not company:
            return
        year = self.inntektsaar
        target_lock = date(year, 12, 31)
        vals = {}
        existing_fy = company.fiscalyear_lock_date
        if not existing_fy or existing_fy < target_lock:
            vals['fiscalyear_lock_date'] = target_lock
        existing_tax = company.tax_lock_date
        if not existing_tax or existing_tax < target_lock:
            vals['tax_lock_date'] = target_lock
        if vals:
            company.sudo().write(vals)
            _logger.info(
                "Skattemelding %s mottatt — auto-låste regnskapsår for "
                "%s til %s (fiscalyear/tax_lock_date)",
                self.id, company.name, target_lock,
            )

    # ---------- Kvittering-arkivering + chatter ----------

    def _archive_kvittering_and_post(self):
        """Lagre kvittering som ir.attachment + post i chatter.

        Når Skatteetaten har behandlet (godkjent/avvist), opprettes:
          1. Vedlegg: skattemelding-{aar}.xml (det vi sendte inn)
          2. Vedlegg: tilbakemelding-{aar}.xml (Skatteetatens vedtak)
          3. Vedlegg: altinn-instans-{guid}.json (full Altinn-respons)
          4. Chatter-melding med:
             - Status (godkjent/avvist) som visuelt blikkfang
             - Lenker til vedlegg
             - Tidspunkt + partsnummer + instans-GUID for sporing

        Idempotent (P1 #5):
          - Vedleggene sjekkes via res_model + res_id + name (alle
             3 typer: .xml + .json — den gamle 'name like %-{aar}.xml'
             tok ikke JSON-vedlegget med).
          - Chatter-post er gated av self.kvittering_archived_at slik
             at gjentatte cron-tikk ikke skaper duplikate poster.

        Brukes for revisjon og audit-trail. Bransjestandard fra Tripletex,
        Visma, Maestro o.l. — alle disse lagrer kvittering som dokument.
        """
        self.ensure_one()
        # Idempotency-vakt: hvis allerede arkivert, gjør ingenting.
        # (Cron poller hver minutt — uten denne vakt ville hver tikk
        # etter mottatt-state ha skapt nye chatter-poster.)
        if self.kvittering_archived_at:
            return
        Attachment = self.env['ir.attachment']
        aar = self.inntektsaar
        # Hent ALLE eksisterende vedlegg på denne recorden — ikke filtrer
        # på filnavn-mønster (den gamle `%-{aar}.xml`-filter savnet JSON-
        # vedlegget og kunne lage duplikat-altinn-instans-json).
        existing = Attachment.search([
            ('res_model', '=', self._name),
            ('res_id', '=', self.id),
        ])
        existing_names = set(existing.mapped('name'))

        created_attachments = []

        # 1. Skattemelding-XML (det vi sendte inn)
        skattemelding_name = f'skattemelding-{aar}.xml'
        if self.skattemelding_xml and skattemelding_name not in existing_names:
            att = Attachment.create({
                'name': skattemelding_name,
                'datas': base64.b64encode(self.skattemelding_xml.encode('utf-8')),
                'res_model': self._name,
                'res_id': self.id,
                'mimetype': 'application/xml',
                'description': _(
                    "Skattemelding-XML innsendt til Skatteetaten via "
                    "Altinn instans %(g)s den %(d)s",
                    g=self.altinn_instance_guid or '–',
                    # P2 #6: format_datetime konverterer til brukerens
                    # tidssone (Europe/Oslo). Tidligere brukte vi naive
                    # strftime som ga UTC-tid (Eristo viste 15:16 i chatter
                    # men 17:16 i form).
                    d=(self.submitted_at
                       and format_datetime(
                           self.env, self.submitted_at,
                           dt_format='dd.MM.yyyy HH:mm',
                       )) or '–',
                ),
            })
            created_attachments.append(att)

        # 2. Tilbakemelding-XML (Skatteetatens vedtak)
        # Leser fra dedikert felt tilbakemelding_xml (P1 #2 — ikke
        # lenger substring-parsing av last_response). Legacy-fallback
        # for gamle records uten dedikerte felter.
        tilbakemelding_name = f'tilbakemelding-{aar}.xml'
        tilbakemelding_body = self.tilbakemelding_xml or ''
        if not tilbakemelding_body and self.last_response \
                and '=== Skatteetatens tilbakemelding' in self.last_response:
            tilbake_idx = self.last_response.find(
                '=== Skatteetatens tilbakemelding (tilbakemelding.xml) ==='
            )
            if tilbake_idx >= 0:
                tilbake_content = self.last_response[tilbake_idx:].split('\n', 1)
                if len(tilbake_content) > 1:
                    tilbakemelding_body = tilbake_content[1].strip()
        if (tilbakemelding_body
                and tilbakemelding_name not in existing_names):
            att = Attachment.create({
                'name': tilbakemelding_name,
                'datas': base64.b64encode(tilbakemelding_body.encode('utf-8')),
                'res_model': self._name,
                'res_id': self.id,
                'mimetype': 'application/xml',
                'description': _(
                    "Skatteetatens tilbakemelding på innsending — "
                    "vedtak %(status)s",
                    status=(
                        self.skatteetaten_endelig_status
                        or 'ukjent'),
                ),
            })
            created_attachments.append(att)

        # 3. Altinn-instans JSON (full kvittering med metadata)
        # Leser fra dedikert felt altinn_instance_json (P1 #2).
        # Legacy-fallback for gamle records.
        altinn_name = f'altinn-instans-{self.altinn_instance_guid}.json'[:200]
        altinn_json_body = self.altinn_instance_json or ''
        if not altinn_json_body and self.last_response \
                and self.last_response.lstrip().startswith('{'):
            altinn_json_body = self.last_response.split('===', 1)[0].strip() \
                or self.last_response
        if (altinn_json_body
                and altinn_name not in existing_names):
            att = Attachment.create({
                'name': altinn_name,
                'datas': base64.b64encode(altinn_json_body.encode('utf-8')),
                'res_model': self._name,
                'res_id': self.id,
                'mimetype': 'application/json',
                'description': _("Full Altinn-instans-respons (audit-trail)"),
            })
            created_attachments.append(att)

        # 4. Post i chatter med status + lenker.
        # NB: alle body-strenger MÅ wraps i Markup() — uten den escaper
        # message_post HTML-en og chatter viser <p><strong>... som
        # plain-tekst. Verifisert mot Eristo prod-record 2026-05-18.
        endelig = self.skatteetaten_endelig_status
        if endelig == 'godkjent':
            subject = _("✓ Skattemelding godkjent av Skatteetaten")
            body_intro = Markup(_(
                "<p><strong style='color:#28a745;'>Skattemeldingen er "
                "godkjent</strong> (resultatAvValidering=validertOK).</p>"
            ))
            tracking_value = 'mottatt_godkjent'
        elif endelig == 'avvist':
            aarsak = self.altinn_substatus_description or '–'
            subject = _("✗ Skattemelding avvist av Skatteetaten")
            body_intro = Markup(_(
                "<p><strong style='color:#dc3545;'>Skattemeldingen er "
                "avvist</strong> (resultatAvValidering=validertMedFeil).</p>"
                "<p>Årsak: <code>%(a)s</code></p>"
                "<p>Lag korreksjons-innsending etter at feilen er rettet.</p>",
                a=aarsak,
            ))
            tracking_value = 'mottatt_avvist'
        else:
            subject = _("Skattemelding mottatt — ukjent status")
            body_intro = Markup(_(
                "<p>Altinn har arkivert instansen, men vi klarte ikke å "
                "tolke Skatteetatens vedtak. Sjekk Validering-fanen.</p>"
            ))
            tracking_value = 'mottatt_ukjent'

        # B9 fix: trunkér Altinn-UUID til 'xxxxxxxx…xxxxxxxx' for å redusere
        # visuell støy. Full UUID forblir i altinn_instance_guid-feltet for
        # support/debug. Wrap i <span title=...> så hover viser full UUID.
        guid = self.altinn_instance_guid or ''
        if len(guid) > 20:
            guid_display = Markup(
                '<span title="%s">%s…%s</span>'
            ) % (guid, guid[:8], guid[-8:])
        else:
            guid_display = guid or '–'

        body_meta = Markup(_(
            "<p style='color:#666;font-size:0.9em;'>"
            "Partsnummer: <code>%(p)s</code> &middot; "
            "Altinn-instans: <code>%(g)s</code> &middot; "
            "Mottatt: %(d)s</p>",
            p=self.partsnummer or '–',
            g=guid_display,
            # P2 #6: format_datetime konverterer til brukerens tidssone
            d=(self.mottatt_at
               and format_datetime(
                   self.env, self.mottatt_at,
                   dt_format='dd.MM.yyyy HH:mm',
               )) or '–',
        ))

        body = body_intro + body_meta
        if created_attachments:
            body += Markup(_(
                "<p style='margin-top:8px;'><b>Vedlegg arkivert:</b></p>"
                "<ul>"
            ))
            for att in created_attachments:
                body += Markup('<li>%s</li>') % att.name
            body += Markup("</ul>")

        # Idempotency: vi har allerede sjekket self.kvittering_archived_at
        # ved entry, men beholder også subject-baserte sjekken som ekstra
        # belte+seler (i tilfelle migration fra eldre records hvor
        # kvittering_archived_at ikke ble satt).
        existing_messages = self.env['mail.message'].search([
            ('model', '=', self._name),
            ('res_id', '=', self.id),
            ('subject', 'in', [
                subject,
                _("✓ Skattemelding godkjent av Skatteetaten"),
                _("✗ Skattemelding avvist av Skatteetaten"),
                _("Skattemelding mottatt — ukjent status"),
            ]),
        ], limit=1)
        if not existing_messages:
            self.message_post(
                subject=subject,
                body=body,
                attachment_ids=[a.id for a in created_attachments],
                subtype_xmlid='mail.mt_comment',
            )
            _logger.info(
                "Skattemelding %s: arkiverte %d vedlegg + posted kvittering "
                "i chatter (status=%s)",
                self.id, len(created_attachments), tracking_value,
            )

        # P1 #5: marker som arkivert slik at neste cron-tikk ikke
        # forsøker å arkivere på nytt.
        self.write({'kvittering_archived_at': fields.Datetime.now()})

    # ---------- Tilbakemelding-fetch fra Altinn ----------

    def action_l10n_no_skattemelding_fetch_tilbakemelding(self):
        """Hent tilbakemelding.xml fra Altinn instans-data.

        Skatteetaten genererer tilbakemelding.xml som dataElement på
        instansen når innsendingen er prosessert (Task_3 / EndEvent).
        Den inneholder enten:
          - Godkjent-bekreftelse, eller
          - Avvist + årsaksforklaring
        """
        self.ensure_one()
        if not self.altinn_instance_guid:
            raise UserError(_("Ingen Altinn-kvittering tilgjengelig ennå."))

        # P1 #2: Les Altinn-instans-JSON fra dedikert felt
        # altinn_instance_json. Legacy-fallback: gamle records kan ha
        # JSON liggende kun i last_response.
        instance_json_raw = self.altinn_instance_json or self.last_response
        if not instance_json_raw:
            raise UserError(_("Ingen Altinn-kvittering tilgjengelig ennå."))
        try:
            instance_data = json.loads(instance_json_raw)
        except (json.JSONDecodeError, TypeError):
            raise UserError(_(
                "Kunne ikke parse Altinn-instans-JSON. "
                "Klikk 'Sjekk status hos Altinn' først for å hente "
                "fersk respons fra Altinn."
            ))

        tilbakemelding_data_guid = None
        for data_el in instance_data.get('data', []):
            if data_el.get('dataType') == 'tilbakemelding':
                tilbakemelding_data_guid = data_el.get('id')
                break
        if not tilbakemelding_data_guid:
            raise UserError(_(
                "Ingen tilbakemelding-vedlegg funnet i Altinn-instansen. "
                "Skatteetaten har ikke ennå generert tilbakemelding."
            ))

        # Hent fra Altinn
        company = self.company_id
        eristo = self.env['l10n.no.eristo.service']
        maskinporten_token = eristo.get_access_token(
            company, scope=_SKATTEMELDING_SCOPE,
        )
        altinn_token = self._exchange_to_altinn_token(maskinporten_token)
        platform = _resolve_altinn_platform(company)
        url = (
            f"{platform}/storage/api/v1/instances/"
            f"{self.altinn_instance_owner_party_id}/"
            f"{self.altinn_instance_guid}/data/{tilbakemelding_data_guid}"
        )
        try:
            resp = requests.get(
                url,
                headers={
                    'Authorization': f'Bearer {altinn_token}',
                    'Accept': 'application/xml',
                },
                timeout=_HTTP_TIMEOUT,
            )
        except requests.RequestException as e:
            raise UserError(_(
                "Altinn ikke tilgjengelig: %(err)s", err=str(e)[:300],
            ))
        # Force UTF-8 decoding. requests defaulter til Latin-1 for text/xml
        # uten Content-Type charset, men Skatteetaten sender UTF-8 (deklarert
        # i XML-headeren). Uten dette får brukeren mojibake i veiledningene:
        # "bÃ¸rsnotert" istedenfor "børsnotert". Verifisert mot Eristo
        # 2026-05-18.
        resp.encoding = 'utf-8'
        if resp.status_code >= 400:
            raise UserError(_(
                "Kunne ikke hente tilbakemelding (HTTP %(c)s):\n%(b)s",
                c=resp.status_code, b=resp.text[:1000],
            ))
        tilbakemelding_xml = resp.text

        # P1 #2: skriv til dedikert felt for tilbakemelding-XML (i tillegg
        # til last_response for diagnostisk visning). _archive_kvittering_
        # _and_post + _compute_altinn_substatus leser nå fra det dedikerte
        # feltet i stedet for substring-parsing.
        #
        # 2026-05-18: bygg merged fra ferske kilder (altinn_instance_json
        # + nytt tilbakemelding_xml), IKKE fra self.last_response som
        # potensielt har gammelt innhold med mojibake fra før UTF-8-fix.
        # Tidligere brukte vi self.last_response → re-fetch akkumulerte
        # duplikate markører og _compute_validation_summary plukket
        # første (eldste/korrupte) treff.
        merged = (
            f"=== Altinn instans-respons ===\n{self.altinn_instance_json or ''}\n\n"
            f"=== Skatteetatens tilbakemelding (tilbakemelding.xml) ===\n"
            f"{tilbakemelding_xml}"
        )
        self.write({
            'last_response': merged,
            'tilbakemelding_xml': tilbakemelding_xml,
        })
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'success',
                'title': _("Tilbakemelding hentet"),
                'message': _(
                    "Skatteetatens tilbakemelding er nå lagret. "
                    "Se Validering-fanen for full innhold."
                ),
                'sticky': True,
                'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
            },
        }

    # ---------- Cron-polling ----------

    @api.model
    def _cron_poll_pending_receipts(self):
        """Poll Altinn for skattemelding-kvitteringer + bruker-signering.

        To grupper records:
          1. state='uploaded' — venter på at bruker signerer i Altinn-portalen.
             Vi poller for å oppdage når brukeren har klikket Send inn
             (state → 'submitted').
          2. state='submitted' — bruker har signert, Skatteetaten behandler
             (3-10 min). Vi poller for kvittering (state → 'mottatt').

        Robusthet:
          - Per-record commit: feil i én record ruller IKKE tilbake andre
            som lyktes (savepoint-pattern)
          - Suppress UserError + nettverksfeil: bare logg, retry neste tick
          - For 'uploaded': vi poller hvis create_date > 1 min (tid for at
            bruker faktisk har rukket å gå til Altinn)
          - For 'submitted': vi poller hvis submitted_at > 2 min
        """
        now = fields.Datetime.now()
        # Records venter på bruker-signering (state=uploaded)
        uploaded_pending = self.search([
            ('state', '=', 'uploaded'),
            ('altinn_instance_guid', '!=', False),
            ('create_date', '<', now - timedelta(minutes=1)),
        ])
        # Records venter på Skatteetaten-kvittering (state=submitted)
        submitted_pending = self.search([
            ('state', '=', 'submitted'),
            ('mottatt_at', '=', False),
            ('submitted_at', '<', now - timedelta(minutes=2)),
        ])
        pending = uploaded_pending + submitted_pending
        if not pending:
            return
        _logger.info(
            "Skattemelding-kvittering-cron: %d pending records å polle",
            len(pending),
        )
        for sm in pending:
            try:
                # Bruk savepoint så feil i én ikke ruller tilbake forrige
                with self.env.cr.savepoint():
                    sm.action_l10n_no_skattemelding_fetch_receipt()
                _logger.debug(
                    "Skattemelding %s: kvittering-polling OK, state=%s",
                    sm.id, sm.state,
                )
            except UserError as e:
                # Forventede feil (instans-ikke-klar, nettverksglitches)
                _logger.info(
                    "Skattemelding %s: kvittering ikke klar enda (%s) — "
                    "retry neste cron-tick",
                    sm.id, str(e)[:200],
                )
            except Exception as e:
                # Uventede feil — logg som warning med stack trace, men
                # ikke crash cron (P2 #4). Andre records skal fortsatt
                # prosesseres i denne cron-runden.
                _logger.warning(
                    "Skattemelding %s: cron-polling uventet feil: %s",
                    sm.id, str(e)[:300],
                    exc_info=True,
                )
