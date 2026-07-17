"""Kvittering + betalingsinformasjon-fetch for mva-melding.

Etter at innsendingen er fullført (state='submitted') behandler Skatteetatens
backend meldingen og legger kvittering + betalingsinformasjon som data-
elementer på Altinn-instansen. Vi poller, henter dem, og arkiverer som
ir.attachment + chatter (audit-trail).

VIKTIG (rettet 2026-07-13): at Skatteetaten HAR gitt tilbakemelding betyr
ikke at meldingen ble GODKJENT. Instansen får også et `valideringsresultat`-
dataelement med verdiktet — 'ingen avvik' = fastsatt, 'ugyldig skattemelding'
= AVVIST. Vi MÅ lese verdiktet før vi rapporterer suksess: godkjent → state=
'mottatt', avvist → state='avvist'. Uten dette overrapporterte modulen en
avvist melding som mottatt (bevist i TT02: kvittering M-2026-1800 «Ugyldig
mva-melding …» ble likevel markert mottatt).

Data-elementer (dataType i instans.json, verifisert 2026-06-05):
  kvittering            → kvittering.pdf (mottakskvittering)
  betalingsinformasjon  → betalingsinformasjon.xml (beløp, frist, konto, KID)
  valideringsresultat   → valideringsresultat.xml

Nedlasting via Altinn storage-API:
  GET {platform}/storage/api/v1/instances/{party}/{guid}/data/{dataGuid}
"""
import base64
import json
import logging
from datetime import timedelta

import requests
from lxml import etree
from markupsafe import Markup, escape

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .l10n_no_mvamelding_submit import (
    _ALTINN_PLATFORM_HOSTS,
    _HTTP_TIMEOUT,
    _resolve_env,
)

_logger = logging.getLogger(__name__)

_NS_BETALING = (
    'no:skatteetaten:fastsetting:avgift:mva:'
    'skattemeldingformerverdiavgift:betalingsinformasjon:v1.0'
)


class L10nNoMvamelding(models.Model):
    _inherit = 'l10n.no.mvamelding'

    def action_fetch_receipt(self):
        """Hent kvittering + betalingsinformasjon fra Altinn (manuelt/cron)."""
        self.ensure_one()
        if self.state not in ('submitted', 'mottatt'):
            raise UserError(_(
                "Kan kun hente kvittering for innsendte meldinger. "
                "Status: %(s)s", s=self.state,
            ))
        if not self.altinn_instance_guid:
            raise UserError(_("Mangler Altinn instance-GUID."))

        altinn_token = self._get_altinn_token()
        if not self._do_fetch_receipt(altinn_token):
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'type': 'warning',
                    'title': _("Kvittering ikke klar ennå"),
                    'message': _(
                        "Skatteetaten har ikke ferdigbehandlet meldingen. "
                        "Prøv igjen om noen minutter."),
                },
            }
        # Verdiktet (godkjent vs avvist) er avgjort i _do_fetch_receipt.
        if self.state == 'avvist':
            return self._notify_avvist()
        return self._notify_received()

    def _altinn_feedback_status(self, altinn_token):
        """GET …/feedback/status → True/False/None.

        Skatteetatens mva-app legger innsendingen i en `feedback`-task
        (Tilbakemelding) etter at utfylling + bekreftelse er fullført. Vi MÅ
        sjekke dette endepunktet for å vite om Skatteetaten har ferdigbehandlet
        meldingen — det er IKKE nok at `betalingsinformasjon` har dukket opp
        (den genereres MENS feedback-tasken kjører, før meldingen er fastsatt).

        Returnerer True hvis isFeedbackProvided=true, False hvis ikke, None ved
        feil/uventet svar (caller faller da tilbake til en vanlig instans-GET).

        Ref: skatteetaten.github.io/mva-meldingen — GET {instans}/feedback/status.
        """
        self.ensure_one()
        url = f'{self._altinn_instance_base()}/feedback/status'
        try:
            resp = requests.get(
                url,
                headers={'Authorization': f'Bearer {altinn_token}',
                         'Accept': 'application/json'},
                timeout=_HTTP_TIMEOUT,
            )
        except requests.RequestException as e:
            _logger.warning("Mvamelding %s: feedback/status feilet: %s",
                            self.id, str(e)[:200])
            return None
        if resp.status_code >= 400:
            _logger.info("Mvamelding %s: feedback/status HTTP %s: %s",
                         self.id, resp.status_code, (resp.text or '')[:200])
            return None
        try:
            return bool(resp.json().get('isFeedbackProvided'))
        except ValueError:
            _logger.warning("Mvamelding %s: feedback/status ikke-JSON: %s",
                            self.id, (resp.text or '')[:200])
            return None

    def _altinn_get_feedback(self, altinn_token):
        """GET …/feedback → instans-dict med alle tilbakemeldings-dataelementer.

        Synkront/blokkerende endepunkt: returnerer instansen når Skatteetaten
        har gitt tilbakemelding, da inkludert `kvittering`-dataelementet. Kalles
        kun når feedback/status sier provided=True (så det ikke blokkerer).
        """
        self.ensure_one()
        url = f'{self._altinn_instance_base()}/feedback'
        try:
            resp = requests.get(
                url,
                headers={'Authorization': f'Bearer {altinn_token}',
                         'Accept': 'application/json'},
                timeout=_HTTP_TIMEOUT,
            )
        except requests.RequestException as e:
            _logger.warning("Mvamelding %s: GET feedback feilet: %s",
                            self.id, str(e)[:200])
            return None
        if resp.status_code >= 400:
            _logger.info("Mvamelding %s: GET feedback HTTP %s: %s",
                         self.id, resp.status_code, (resp.text or '')[:200])
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    def _do_fetch_receipt(self, altinn_token):
        """Hent + lagre betalingsinfo/kvittering fra Altinn-instansen.

        Returnerer True når Skatteetaten har FASTSATT meldingen (feedback gitt
        + kvittering/process.ended), False hvis den ennå ikke er ferdigbehandlet.
        Gjenbrukes av den manuelle knappen, cron-en, og auto-forsøket rett etter
        innsending.

        VIKTIG (rettet 2026-06-15): meldingen regnes IKKE som mottatt bare fordi
        `betalingsinformasjon` finnes — den genereres mens feedback-tasken kjører,
        FØR meldingen er fastsatt. Vi sjekker feedback-endepunktet (steg 6–7 i
        Skatteetatens implementasjonsguide) og krever kvittering/process.ended.
        """
        self.ensure_one()
        # Steg 6: har Skatteetaten gitt tilbakemelding? feedback/status er et
        # billig poll-endepunkt som returnerer {"isFeedbackProvided": bool}.
        # Bekreftet mot TT02: feedback-endepunktet svarer HTTP 409 "has not yet
        # received feedback. Try again later." helt til feedback er klar — så vi
        # kaller det KUN når status=true (ellers risikerer vi at prod-varianten,
        # som er synkron/blokkerende, henger en worker mens den venter).
        provided = self._altinn_feedback_status(altinn_token)
        if provided:
            # Steg 7: hent instansen via feedback-endepunktet — nå med kvittering.
            instance = (self._altinn_get_feedback(altinn_token)
                        or self._altinn_get_instance(altinn_token))
        else:
            # Ikke klar ennå. Vanlig instans-GET fanger likevel betalings-
            # informasjon (informativt) og process.ended som backup-signal.
            instance = self._altinn_get_instance(altinn_token)
        data_elements = instance.get('data') or []
        process = instance.get('process') or {}
        by_type = {d.get('dataType'): d for d in data_elements}
        self.last_response = json.dumps(instance)[:8000]

        has_betaling = bool(by_type.get('betalingsinformasjon'))
        has_kvittering = bool(by_type.get('kvittering'))
        process_ended = bool(process.get('ended'))

        # Fang betalingsinformasjon så snart den finnes (informativt: KID/beløp/
        # frist kan vises mens vi venter), men det alene gjør IKKE meldingen
        # mottatt. Vi laster den ned uavhengig av fastsettings-status.
        if has_betaling and not self.betalingsinformasjon_xml:
            self._fetch_betalingsinformasjon(altinn_token, instance, by_type)

        # Ferdigbehandlet = Skatteetaten har gitt tilbakemelding OG produsert
        # kvittering (eller prosessen er formelt endt). Først DA foreligger et
        # verdikt. MERK: «ferdigbehandlet» ≠ «godkjent» — se verdikt-steget.
        finalized = process_ended or (bool(provided) and has_kvittering)
        if not finalized:
            _logger.info(
                "Mvamelding %s: ennå ikke ferdigbehandlet (provided=%s, "
                "kvittering=%s, ended=%s, betaling=%s) — venter.",
                self.id, provided, has_kvittering, process_ended, has_betaling)
            return False

        # Les verdiktet fra valideringsresultat-dataelementet: godkjent (fastsatt)
        # vs avvist (ugyldig). Uten dette ville en avvist melding blitt markert
        # 'mottatt' (overrapportering).
        verdict = self._classify_valideringsresultat(altinn_token, by_type)
        if verdict is None:
            # Verdiktet kunne ikke avgjøres ennå: valideringsresultat-elementet
            # mangler, kunne ikke lastes ned, eller er uleselig (uparsebar/feil
            # namespace). IKKE marker noe — tryggere å vente enn å gjette.
            # Cron/knapp prøver igjen. (_classify_valideringsresultat har logget
            # den spesifikke årsaken.)
            return False

        if verdict['avvist']:
            return self._mark_avvist(altinn_token, instance, by_type, verdict)
        return self._mark_mottatt(altinn_token, instance, by_type, verdict)

    def _classify_valideringsresultat(self, altinn_token, by_type):
        """Hent + tolk valideringsresultat → verdikt-dict, eller None.

        Returnerer:
          {'avvist': bool, 'xml': str, 'summary': str|None,
           'avvik_count': int, 'human': str|None}
          None  hvis verdiktet ikke kan avgjøres ennå (valideringsresultat-
                elementet mangler, kan ikke lastes ned, eller ikke tolkes) —
                caller skal da vente, ikke markere.

        VIKTIG: mangler valideringsresultat-elementet, utleder vi IKKE godkjent.
        En avvist melding har også process.ended/EndEvent, så process.ended
        alene beviser ikke godkjenning — vi må lese selve verdiktet. Er det ennå
        ikke lagt på instansen (timing), utsetter vi til det kommer.
        """
        self.ensure_one()
        vr_el = by_type.get('valideringsresultat')
        if not (vr_el and vr_el.get('id')):
            _logger.warning(
                "Mvamelding %s: ferdigbehandlet uten valideringsresultat-element "
                "ennå — kan ikke avgjøre godkjent vs avvist, utsetter.", self.id)
            return None
        xml = self._download_data(
            altinn_token, self._altinn_storage_base(), vr_el['id'],
            as_text=True)
        if not xml:
            # Elementet finnes, men nedlasting feilet (transient) — ukjent verdikt.
            _logger.warning(
                "Mvamelding %s: valideringsresultat-element finnes, men "
                "nedlasting feilet — verdikt utsatt.", self.id)
            return None
        avvist, summary, avvik_count, human = \
            self._vurder_valideringsresultat(xml)
        if avvist is None:
            # Uparsebar XML — verdikt kan ikke avgjøres.
            return None
        return {'avvist': avvist, 'xml': xml, 'summary': summary,
                'avvik_count': avvik_count, 'human': human}

    def _mark_mottatt(self, altinn_token, instance, by_type, verdict):
        """Marker meldingen som mottatt/fastsatt hos Skatteetaten + arkiver.

        ``last_response`` er allerede skrevet i _do_fetch_receipt (dekker også
        utsett-stiene), så vi skriver den ikke på nytt her.
        """
        self.ensure_one()
        vals = {}
        if self.state != 'mottatt':
            vals['state'] = 'mottatt'
            vals['mottatt_at'] = self.mottatt_at or fields.Datetime.now()
        # Behold et valideringsresultat MED merknad ('avvikende ...'/'mangelfull
        # ...', avvik_count>0) i audit-sporet; et rent 'ingen avvik' lagres ikke
        # (holder fanen «Avvik / valideringsresultat» skjult for ren godkjenning).
        if verdict.get('xml') and (verdict.get('avvik_count') or 0) > 0:
            vals['valideringsresultat_xml'] = verdict['xml']
            vals['avvik_count'] = verdict['avvik_count']
        if vals:
            self.write(vals)

        # Kvittering-PDF + arkivering (idempotent via kvittering_archived_at).
        if not self.kvittering_archived_at:
            storage_base = self._altinn_storage_base()
            created, kvittering_ok = self._bygg_altinn_vedlegg(
                storage_base, altinn_token, by_type,
                valideringsresultat_xml=vals.get('valideringsresultat_xml'))
            if not kvittering_ok:
                # Kvittering-elementet finnes, men PDF-nedlasting feilet
                # (transient). IKKE sett kvittering_archived_at — da ville cron
                # sluttet å polle og kvitteringen gått tapt. La cron prøve igjen.
                _logger.error(
                    "Mvamelding %s: kvittering-element finnes men PDF-"
                    "nedlasting feilet — arkiverer IKKE, cron prøver igjen.",
                    self.id)
                return True
            self._post_mottatt_chatter(created, verdict)
            self.write({'kvittering_archived_at': fields.Datetime.now()})
        return True

    def _mark_avvist(self, altinn_token, instance, by_type, verdict):
        """Marker meldingen som AVVIST + arkiver kvittering/valideringsresultat.

        I motsetning til mottak-flyten finaliserer vi selv om kvittering-PDF-en
        ikke er nedlastbar ennå: valideringsresultatet (selve verdikt-beviset)
        er allerede hentet og lagres, og verdiktet MÅ registreres straks så vi
        ikke fortsetter å (feil)rapportere meldingen som under behandling.
        ``last_response`` er allerede skrevet i _do_fetch_receipt.
        """
        self.ensure_one()
        vals = {
            'valideringsresultat_xml': verdict['xml'],
            'avvik_count': verdict['avvik_count'],
        }
        if self.state != 'avvist':
            vals['state'] = 'avvist'
        self.write(vals)

        if not self.kvittering_archived_at:
            storage_base = self._altinn_storage_base()
            created, kvittering_ok = self._bygg_altinn_vedlegg(
                storage_base, altinn_token, by_type,
                valideringsresultat_xml=verdict['xml'])
            if not kvittering_ok:
                _logger.error(
                    "Mvamelding %s: avvist, men kvittering-PDF ikke nedlastbar "
                    "ennå — arkiverer valideringsresultat uten PDF.", self.id)
            self._post_avvist_chatter(created, verdict)
            self.write({'kvittering_archived_at': fields.Datetime.now()})
        return True

    def _altinn_storage_base(self):
        """Altinn storage-API base for nedlasting av data-elementer."""
        self.ensure_one()
        platform = _ALTINN_PLATFORM_HOSTS[_resolve_env(self.company_id)]
        return (
            f"{platform}/storage/api/v1/instances/"
            f"{self.altinn_instance_owner_party_id}/{self.altinn_instance_guid}"
        )

    def _fetch_betalingsinformasjon(self, altinn_token, instance, by_type):
        """Last ned + parse betalingsinformasjon-XML (beløp/frist/konto/KID).

        Informativt steg: kan kjøres så snart betalingsinformasjon-elementet
        finnes, også før meldingen er endelig fastsatt.
        """
        self.ensure_one()
        betaling_el = by_type.get('betalingsinformasjon')
        if not (betaling_el and betaling_el.get('id')):
            return
        xml = self._download_data(
            altinn_token, self._altinn_storage_base(), betaling_el['id'],
            as_text=True)
        if xml:
            self.betalingsinformasjon_xml = xml
            self._parse_betalingsinformasjon(xml)

    def _notify_received(self):
        """Notifikasjon når meldingen er registrert mottatt hos Skatteetaten."""
        self.ensure_one()
        if self.kvittering_archived_at:
            msg = _("Skatteetaten har mottatt mva-meldingen. Kvittering og "
                    "betalingsinformasjon er arkivert.")
        else:
            msg = _("Skatteetaten har mottatt mva-meldingen og generert "
                    "betalingsinformasjon (å betale: %(b)s kr, frist %(f)s). "
                    "Kvittering-PDF arkiveres automatisk når den er klar.",
                    b=self.betalingsbeloep or '–', f=self.betalingsfrist or '–')
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'success',
                'title': _("MVA-melding mottatt"),
                'message': msg,
                'sticky': True,
                'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
            },
        }

    def _download_data(self, altinn_token, storage_base, data_guid,
                       as_text=False):
        """GET et data-element fra Altinn storage-API."""
        self.ensure_one()
        url = f'{storage_base}/data/{data_guid}'
        try:
            resp = requests.get(
                url,
                headers={'Authorization': f'Bearer {altinn_token}'},
                timeout=_HTTP_TIMEOUT,
            )
        except requests.RequestException as e:
            _logger.warning("Mvamelding %s: download %s feilet: %s",
                            self.id, data_guid, str(e)[:200])
            return None
        if resp.status_code >= 400:
            _logger.warning("Mvamelding %s: download %s HTTP %s",
                            self.id, data_guid, resp.status_code)
            return None
        if as_text:
            resp.encoding = 'utf-8'
            return resp.text
        return resp.content

    def _parse_betalingsinformasjon(self, xml_text):
        """Trekk ut beløp, frist, konto, KID fra betalingsinformasjon.xml."""
        self.ensure_one()
        try:
            root = etree.fromstring(xml_text.encode('utf-8'))
        except etree.XMLSyntaxError as e:
            _logger.error(
                "Mvamelding %s: betalingsinformasjon ikke parsebar XML: %s",
                self.id, str(e)[:200])
            return

        def find(path):
            el = root.find(path)
            return (el.text or '').strip() if el is not None else None

        ns = _NS_BETALING
        beloep = find(f'{{{ns}}}beloep')
        frist = find(f'{{{ns}}}betalingsfrist')
        kid = find(f'{{{ns}}}kundeidentifikasjonsnummer')
        konto = find(
            f'{{{ns}}}betalesTilKonto/{{{ns}}}norskKontonummer')
        vals = {}
        if beloep is not None:
            try:
                vals['betalingsbeloep'] = float(beloep)
            except ValueError:
                pass
        if frist:
            vals['betalingsfrist'] = frist
        if konto:
            vals['betalingskonto'] = konto
        if kid:
            vals['betalings_kid'] = kid
        if vals:
            self.write(vals)
        else:
            # Parsing lyktes, men ingen kjente felt funnet — sannsynlig
            # namespace-/skjema-drift. Ikke la dette se ut som suksess.
            actual_ns = etree.QName(root).namespace
            _logger.error(
                "Mvamelding %s: betalingsinformasjon parset men ingen felt "
                "funnet (forventet ns=%s, fikk ns=%s). Mulig skjema-endring.",
                self.id, _NS_BETALING, actual_ns)
            self.message_post(body=_(
                "⚠ Betalingsinformasjon mottatt, men beløp/frist/KID kunne "
                "ikke leses automatisk (mulig endret format hos Skatteetaten). "
                "Sjekk vedlagt betalingsinformasjon-XML manuelt."))

    def _bygg_altinn_vedlegg(self, storage_base, altinn_token, by_type,
                             valideringsresultat_xml=None):
        """Bygg ir.attachment for mva-melding, konvolutt, betalingsinfo,
        valideringsresultat og kvittering-PDF. → (created, kvittering_ok).

        ``kvittering_ok`` = False betyr at kvittering-elementet finnes, men
        PDF-nedlasting feilet (transient). Mottak-flyten bruker det til å utsette
        arkiveringen så cron prøver igjen; avvist-flyten finaliserer likevel
        (valideringsresultatet er beviset). Idempotent via navnesjekk mot
        eksisterende vedlegg slik at gjentatte tikk ikke dupliserer.
        """
        self.ensure_one()
        Attachment = self.env['ir.attachment']
        existing = set(Attachment.search([
            ('res_model', '=', self._name), ('res_id', '=', self.id),
        ]).mapped('name'))
        suffix = f'{self.aar}-{self.periode}'
        created = []

        def _attach(name, content_bytes, mimetype):
            if name in existing or not content_bytes:
                return
            created.append(Attachment.create({
                'name': name,
                'datas': base64.b64encode(content_bytes),
                'res_model': self._name, 'res_id': self.id,
                'mimetype': mimetype,
            }))

        if self.mvamelding_xml:
            _attach(f'mvamelding-{suffix}.xml',
                    self.mvamelding_xml.encode('utf-8'), 'application/xml')
        if self.konvolutt_xml:
            _attach(f'konvolutt-{suffix}.xml',
                    self.konvolutt_xml.encode('utf-8'), 'application/xml')
        if self.betalingsinformasjon_xml:
            _attach(f'betalingsinformasjon-{suffix}.xml',
                    self.betalingsinformasjon_xml.encode('utf-8'),
                    'application/xml')
        if valideringsresultat_xml:
            _attach(f'valideringsresultat-{suffix}.xml',
                    valideringsresultat_xml.encode('utf-8'), 'application/xml')

        kvittering_ok = True
        kvittering_el = by_type.get('kvittering')
        if kvittering_el and kvittering_el.get('id'):
            pdf = self._download_data(
                altinn_token, storage_base, kvittering_el['id'])
            if pdf:
                _attach(f'kvittering-{suffix}.pdf', pdf, 'application/pdf')
            else:
                kvittering_ok = False
        return created, kvittering_ok

    def _post_mottatt_chatter(self, created, verdict=None):
        """Grønn chatter-post: meldingen er mottatt/fastsatt hos Skatteetaten."""
        self.ensure_one()
        body = Markup(_(
            "<p><strong style='color:#28a745;'>MVA-melding mottatt av "
            "Skatteetaten.</strong></p>"
            "<p style='color:#666;font-size:0.9em;'>Fastsatt MVA: "
            "<code>%(sum)s kr</code> &middot; Altinn-instans: "
            "<code>%(g)s</code></p>",
            sum=self.fastsatt_mva, g=self.altinn_instance_guid or '–',
        ))
        if self.betalingsbeloep:
            body += Markup(_(
                "<p>Å betale: <b>%(b)s kr</b> &middot; Frist: %(f)s &middot; "
                "Konto: %(k)s &middot; KID: %(kid)s</p>",
                b=self.betalingsbeloep, f=self.betalingsfrist or '–',
                k=self.betalingskonto or '–', kid=self.betalings_kid or '–',
            ))
        # Fastsatt MED merknad (avvikende/mangelfull): vis merknaden så den ikke
        # går tapt fra audit-sporet. escape() på fritekst fra Skatteetaten.
        if verdict and (verdict.get('avvik_count') or 0) > 0 and verdict.get('human'):
            body += Markup(_("<p><b>Merknad fra Skatteetaten:</b></p>"))
            body += Markup("<pre style='white-space:pre-wrap;'>%s</pre>") % \
                verdict['human']
        body += self._vedlegg_liste(created)
        self.message_post(
            subject=_("MVA-melding mottatt av Skatteetaten"),
            body=body,
            attachment_ids=[a.id for a in created],
            subtype_xmlid='mail.mt_comment',
        )
        _logger.info("Mvamelding %s: arkiverte %d vedlegg + kvittering (mottatt)",
                     self.id, len(created))

    def _post_avvist_chatter(self, created, verdict):
        """Rød chatter-post: Skatteetaten AVVISTE meldingen, med begrunnelse."""
        self.ensure_one()
        # Vis antall kun når det er kjent og positivt (samme konvensjon som
        # _notify_avvist); 0/ukjent → '?'.
        antall = (verdict['avvik_count']
                  if (verdict.get('avvik_count') or 0) > 0 else '?')
        # summary er fritekst fra Skatteetaten (avvikVedMeldingslevering) →
        # escape før den interpoleres inn i Markup, slik human allerede er.
        summary = escape(verdict.get('summary') or 'ugyldig mva-melding')
        body = Markup(_(
            "<p><strong style='color:#dc3545;'>MVA-melding AVVIST av "
            "Skatteetaten.</strong></p>"
            "<p style='color:#666;font-size:0.9em;'>Verdikt: <code>%(s)s</code> "
            "&middot; %(n)s avvik &middot; Altinn-instans: <code>%(g)s</code></p>",
            s=summary, n=antall, g=self.altinn_instance_guid or '–',
        ))
        if verdict.get('human'):
            body += Markup(
                "<p><b>Begrunnelse:</b></p>"
                "<pre style='white-space:pre-wrap;'>%s</pre>"
            ) % verdict['human']
        body += Markup(_(
            "<p>Meldingen ble <b>ikke fastsatt</b>. Rett bilagene/registreringen "
            "og send inn på nytt.</p>"))
        body += self._vedlegg_liste(created)
        self.message_post(
            subject=_("MVA-melding avvist av Skatteetaten"),
            body=body,
            attachment_ids=[a.id for a in created],
            subtype_xmlid='mail.mt_comment',
        )
        _logger.info("Mvamelding %s: registrert AVVIST (%s) + %d vedlegg",
                     self.id, verdict.get('summary') or '?', len(created))

    def _vedlegg_liste(self, created):
        """Markup-fragment som lister arkiverte vedlegg (tom hvis ingen)."""
        if not created:
            return Markup("")
        frag = Markup("<p><b>Vedlegg arkivert:</b></p><ul>")
        for att in created:
            frag += Markup('<li>%s</li>') % att.name
        return frag + Markup("</ul>")

    @api.model
    def _cron_poll_pending_receipts(self):
        """Poll Altinn for kvittering på innsendte mva-meldinger."""
        now = fields.Datetime.now()
        # Poll til kvittering-PDFen er arkivert: 'submitted' (venter på
        # betalingsinformasjon/mottak) OG 'mottatt' uten arkivert kvittering
        # (betalingsinfo fanget, venter på kvittering-PDF).
        pending = self.search([
            ('state', 'in', ('submitted', 'mottatt')),
            ('kvittering_archived_at', '=', False),
            ('submitted_at', '<', now - timedelta(minutes=1)),
        ])
        if not pending:
            return
        _logger.info("MVA-kvittering-cron: %d pending records", len(pending))
        for rec in pending:
            try:
                with self.env.cr.savepoint():
                    rec.action_fetch_receipt()
            except UserError as e:
                _logger.info("Mvamelding %s: kvittering ikke klar (%s)",
                             rec.id, str(e)[:200])
            except Exception as e:  # noqa: BLE001
                _logger.warning("Mvamelding %s: cron uventet feil: %s",
                                rec.id, str(e)[:300], exc_info=True)
