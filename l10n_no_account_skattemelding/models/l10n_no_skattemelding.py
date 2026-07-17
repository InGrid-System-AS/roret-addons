"""Skattemelding-record som tracker state per innsending.

Én rad per (company, inntektsaar). Holder XML-utkastet, koblingen til
Altinn 3-instansen, valideringsjobb-id og resultat. State-felt går
gjennom phases som:

  draft → built (XML generert lokalt) → uploaded (sendt til Altinn) →
  validated (Skatteetaten validerte OK) | feilet (avvik fra validering)
  → submitted (sendt inn endelig) → mottatt (kvittering mottatt)

For korreksjonsinnsending peker dokumentidentifikator på forrige
innsending — Skatteetaten arkiverer den gamle og bruker denne i stedet.
"""
import base64
import json
import re
from datetime import date
from html import escape as html_escape

from odoo import _, api, fields, models
from odoo.exceptions import UserError


_STATE_SELECTION = [
    ('draft', 'Utkast'),
    ('built', 'XML generert'),
    ('validated', 'Validert OK'),
    ('feilet', 'Avvik ved validering'),
    ('uploaded', 'Lastet opp til Altinn'),
    ('submitted', 'Sendt inn'),
    ('mottatt', 'Mottatt — kvittering'),
]


class L10nNoSkattemelding(models.Model):
    _name = 'l10n.no.skattemelding'
    _description = 'Skattemelding-innsending for AS'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'inntektsaar desc, company_id'
    _rec_name = 'display_name'

    company_id = fields.Many2one(
        'res.company',
        string="Selskap",
        required=True,
        default=lambda self: self.env.company,
        ondelete='restrict',
    )
    inntektsaar = fields.Integer(
        string="Inntektsår",
        required=True,
        default=lambda self: date.today().year - 1,
    )
    state = fields.Selection(
        _STATE_SELECTION,
        string="Status",
        default='draft',
        required=True,
        copy=False,
        # P3 #8: tracking=False (var True). Hver state-transition genererte
        # en chatter-melding 'draft → built → validated → uploaded → ...'
        # som duplikerte statusbar-widget'en. For en typisk skattemelding
        # ble det 5+ meldinger som drukner det vesentlige (kvittering fra
        # Skatteetaten). Statusbar viser uansett current state; viktig
        # transition (mottatt godkjent/avvist) postes eksplisitt via
        # message_post i _archive_kvittering_and_post.
        tracking=False,
    )

    # Skatteetaten-spesifikke identifikatorer
    partsnummer = fields.Char(
        string="Partsnummer (Skatteetaten)",
        copy=False,
        help="Skatteetatens interne parts-id for selskapet. Identifiseres "
             "når Altinn-instansen opprettes første gang.",
    )
    dokumentidentifikator = fields.Char(
        string="Dokumentidentifikator",
        copy=False,
        help="Identifikator fra forrige innsending (for korreksjon). "
             "Tomt for første innsending.",
    )
    erstatter_skattemelding_id = fields.Many2one(
        'l10n.no.skattemelding',
        string="Erstatter tidligere innsending",
        copy=False,
        ondelete='set null',
        help="Pek på forrige innsending hvis dette er en korreksjon — "
             "Skatteetaten arkiverer da den gamle og bruker denne i stedet.",
    )

    # Altinn 3 instans-info
    altinn_instance_owner_party_id = fields.Char(
        string="Altinn party ID",
        copy=False,
        help="instanceOwnerPartyId for Altinn-instansen — typisk Skatteetatens "
             "interne ID for selskapet i Altinn 3.",
    )
    altinn_instance_guid = fields.Char(
        string="Altinn instance GUID",
        copy=False,
    )
    altinn_data_guid = fields.Char(
        string="Altinn data GUID",
        copy=False,
        help="GUID for konvolutt-data-elementet i Altinn-instansen.",
    )
    requires_resend = fields.Boolean(
        string="Krever ny innsending til Altinn",
        copy=False,
        default=False,
        help="Settes til True når XML er regenerert etter at skattemeldingen "
             "er lastet opp til Altinn — dvs. data i Odoo har drevet fra "
             "det som ligger i Altinn-utkastet. Brukeren ser banner og "
             "knapp for 'Send oppdatert utkast til Altinn'. Resetter til "
             "False etter vellykket re-upload.",
    )

    # Skatteetaten valideringsjobb
    valideringsjobb_id = fields.Char(
        string="Valideringsjobb ID",
        copy=False,
    )
    valideringsjobb_started_at = fields.Datetime(
        string="Validering startet",
        copy=False,
    )
    valideringsjobb_completed_at = fields.Datetime(
        string="Validering ferdig",
        copy=False,
    )

    # Aksjeinformasjon (Skatteetaten merknad N_MANGLER_VERDI_BAK_AKSJENE).
    # Samlet verdi av aksjene i selskapet — typisk aksjekapital fra
    # selskapets vedtekter. Hvis aksjekapitalen ble endret i løpet av
    # året, oppgis verdien per 31.12. Verdien rapporteres uten desimaler
    # (heltall NOK) i XML, men vi lagrer som monetary for typesikkerhet.
    samlet_verdi_aksjer = fields.Monetary(
        string="Samlet verdi av aksjene bak selskapet (NOK)",
        copy=False,
        currency_field='currency_id',
        help="Samlet pålydende verdi av aksjene i selskapet (aksjekapital). "
             "Settes typisk lik bokført aksjekapital per 31.12 i inntektsåret. "
             "For Eristo AS er det normalt 100 000 - 1 000 000 NOK.",
    )
    currency_id = fields.Many2one(
        related='company_id.currency_id',
        readonly=True,
    )

    # Iterasjon 1 av næringsspesifikasjon-utvidelse (2026-05-18):
    # spesifikasjonAvAnleggsmiddel + forskjellMellomRegnskapsmessigOg-
    # SkattemessigVerdi krever per-skattemelding records for hver
    # anleggsmiddel og forskjell-post.
    anleggsmiddel_ids = fields.One2many(
        'l10n.no.skattemelding.anleggsmiddel',
        'skattemelding_id',
        string="Anleggsmidler (saldoavskrevet)",
        help="Eiendeler som avskrives etter saldoprinsippet (saldogruppe A-J). "
             "Mest aktuelt for AS m. goodwill, biler, maskiner, kontorutstyr.",
    )
    forskjell_ids = fields.One2many(
        'l10n.no.skattemelding.forskjell',
        'skattemelding_id',
        string="Forskjeller regnskap vs skatt",
        help="Permanente og midlertidige forskjeller mellom regnskapsmessig "
             "og skattemessig behandling. Typisk for goodwill m. ulik "
             "avskrivnings-takt.",
    )

    def action_l10n_no_skattemelding_roll_forward(self):
        """Kopier anleggsmidler + forskjell-records fra forrige års
        skattemelding for samme selskap.

        For hver anleggsmiddel-record i fjorårets skattemelding opprettes
        en ny record på denne med:
          - inngaaende_verdi = fjorårets utgaaende_verdi
          - nyanskaffelse = 0 (kunde må manuelt sette ved tilganger)
          - aarets_avskrivning auto-beregnes fra nytt grunnlag × sats
          - utgaaende_verdi auto-beregnes
          - Andre felter (saldogruppe, ervervsdato, navn) kopieres som-er

        For hver forskjell-record:
          - inngaaende_verdi = fjorårets utgaaende_verdi
          - aarets_endring = 0 (kunde må manuelt sette)
          - utgaaende_verdi = inngående (uendret inntil endring settes)

        Hvis det allerede finnes records på denne skattemeldingen,
        BLOKKERER vi (forhindrer dupliserte rader). Kunde må slette
        eksisterende records først hvis de vil regenerere.

        Hvis fjorårets skattemelding ikke finnes → ingenting å rulle frem,
        ikke en feil.
        """
        self.ensure_one()
        if self.anleggsmiddel_ids or self.forskjell_ids:
            raise UserError(_(
                "Denne skattemeldingen har allerede anleggsmidler eller "
                "forskjeller. Slett eksisterende records først hvis du vil "
                "rulle frem fra fjorår på nytt."
            ))
        prev = self.search([
            ('company_id', '=', self.company_id.id),
            ('inntektsaar', '=', self.inntektsaar - 1),
        ], order='id desc', limit=1)
        if not prev:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'type': 'warning',
                    'title': "Ingen fjorårs-skattemelding",
                    'message': (
                        f"Fant ikke skattemelding-record for "
                        f"{self.company_id.name} inntektsår "
                        f"{self.inntektsaar - 1}. Ingenting å rulle frem."
                    ),
                    'sticky': False,
                },
            }
        # P1-4 fra code review 2026-05-18: re-link forskjell ↔ anleggsmiddel
        # ved match på (name, saldogruppe). Brukeren får tilbake koblingen
        # uten manuelt arbeid.
        old_am_to_new = {}  # old_am.id -> new_am
        am_count = 0
        for old in prev.anleggsmiddel_ids:
            new_am = self.env['l10n.no.skattemelding.anleggsmiddel'].create({
                'skattemelding_id': self.id,
                'name': old.name,
                'saldogruppe': old.saldogruppe,
                'avskrivningssats': old.avskrivningssats,
                'ervervsdato': old.ervervsdato,
                'inngaaende_verdi': old.utgaaende_verdi or 0.0,
                'nyanskaffelse': 0.0,
                # aarets_avskrivning + utgaaende_verdi auto-beregnes fra
                # IB + sats. Bruker kan overstyre i UI etterpå.
                'er_fysisk': old.er_fysisk,
                'account_id': old.account_id.id if old.account_id else False,
            })
            old_am_to_new[old.id] = new_am
            am_count += 1
        f_count = 0
        for old in prev.forskjell_ids:
            # Re-link til ny anleggsmiddel hvis det fantes en kobling før
            new_am = old_am_to_new.get(
                old.anleggsmiddel_id.id if old.anleggsmiddel_id else None
            )
            self.env['l10n.no.skattemelding.forskjell'].create({
                'skattemelding_id': self.id,
                'type': old.type,
                'name': old.name,
                'inngaaende_verdi': old.utgaaende_verdi or 0.0,
                'aarets_endring': 0.0,
                # utgaaende_verdi auto-beregnes fra IB + 0 endring
                # (= IB inntil bruker setter endring)
                'anleggsmiddel_id': new_am.id if new_am else False,
            })
            f_count += 1
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'success',
                'title': f"Roll-forward fullført",
                'message': (
                    f"Kopiert {am_count} anleggsmidler og {f_count} "
                    f"forskjeller fra {prev.inntektsaar}. Inngående verdier "
                    f"satt fra fjorårets utgående. Sjekk og oppdater "
                    f"nyanskaffelser/endringer for {self.inntektsaar}."
                ),
                'sticky': True,
            },
        }

    # XML-payloads (lagres for revisjon + retry)
    skattemelding_xml = fields.Text(
        string="skattemeldingUpersonlig XML",
        copy=False,
        help="Indre XML for skattemelding upersonlig (RF-1167-erstatter).",
    )
    xml_generated_at = fields.Datetime(
        string="XML generert tidspunkt",
        copy=False,
        readonly=True,
        help="Timestamp for siste XML-generering. Brukes til å oppdage "
             "om XML er stale fordi avslutningsbilag eller andre regnskaps-"
             "endringer er gjort etter generering.",
    )
    skattemelding_xml_display = fields.Text(
        string="skattemeldingUpersonlig XML (pretty)",
        compute='_compute_xml_displays',
        help="Pretty-printet for visning — rå XML lagres som-er.",
    )
    naeringsspesifikasjon_xml = fields.Text(
        string="Næringsspesifikasjon XML",
        copy=False,
        help="Indre XML for næringsspesifikasjon (resultat + balanse).",
    )
    naeringsspesifikasjon_xml_display = fields.Text(
        string="Næringsspesifikasjon XML (pretty)",
        compute='_compute_xml_displays',
    )
    konvolutt_xml = fields.Text(
        string="Konvolutt XML (full payload)",
        copy=False,
        help="Ytre konvolutt med skattemelding + næringsspesifikasjon "
             "base64-enkodet inni. Det er denne som lastes opp til Altinn.",
    )
    konvolutt_xml_display = fields.Text(
        string="Konvolutt XML (pretty)",
        compute='_compute_xml_displays',
    )

    # Validering / kvittering
    last_response = fields.Text(
        string="Siste respons (rå)",
        copy=False,
        help="Rå sist-mottatte respons (JSON eller fritekst-diagnose). "
             "Beholdes som diagnostisk view for support; parsete delene "
             "ligger i dedikerte felter (tilbakemelding_xml, "
             "altinn_instance_json).",
    )
    tilbakemelding_xml = fields.Text(
        string="Skatteetatens tilbakemelding (XML)",
        copy=False,
        help="Tilbakemelding-XML fra Skatteetaten, hentet via "
             "/storage/api/v1/instances/.../data/{tilbakemelding-guid}. "
             "Inneholder <resultatAvValidering> (validertOK/validertMedFeil) "
             "og evt <aarsakTilValidertMedFeil>. Brukes som autoritativ "
             "kilde for skatteetaten_endelig_status.",
    )
    altinn_instance_json = fields.Text(
        string="Altinn instans-respons (JSON)",
        copy=False,
        help="Siste JSON-respons fra Altinn /instances-endepunkt. "
             "Inneholder status.substatus + dataElements-liste. Lagres "
             "som dedikert felt slik at vi slipper å parse last_response.",
    )
    validation_summary = fields.Html(
        string="Valideringssammendrag",
        compute='_compute_validation_summary',
        help="Lesbart sammendrag av siste respons fra Skatteetaten — viser "
             "valideringsresultat, blokkerende avvik og advisory-veiledninger.",
        sanitize=True,
    )
    onboarding_checklist = fields.Html(
        string="Status-sjekkliste",
        compute='_compute_onboarding_checklist',
        help="Viser hva som gjenstår før skattemeldingen kan sendes inn.",
        sanitize=False,
    )
    altinn_substatus_label = fields.Char(
        string="Altinn substatus",
        compute='_compute_altinn_substatus',
        help="Skatteetatens substatus.label fra Altinn-instansen — "
             "typisk 'Godkjent' eller 'Avvist'.",
    )
    altinn_substatus_description = fields.Char(
        string="Altinn substatus-beskrivelse",
        compute='_compute_altinn_substatus',
    )
    skatteetaten_endelig_status = fields.Selection(
        [
            ('pending', 'Avventer behandling'),
            ('godkjent', 'Godkjent'),
            ('avvist', 'Avvist'),
            ('ukjent', 'Ukjent status'),
        ],
        string="Endelig status fra Skatteetaten",
        compute='_compute_altinn_substatus',
        help="Skatteetatens endelig vedtak — godkjent eller avvist. "
             "Henter fra tilbakemelding-XML eller substatus.label.",
    )
    avvik_count = fields.Integer(
        string="Antall avvik",
        compute='_compute_avvik_count',
    )

    display_name = fields.Char(
        compute='_compute_display_name',
        store=True,
    )

    # Årsavslutningsbilag — referanse til account.move som lukker P&L og
    # overfører resultat til balanse-EK. Brukes for å:
    #  1) Idempotency: gjenbruke samme bilag hvis brukeren klikker
    #     'Opprett avslutningsbilag' flere ganger
    #  2) Smart-button på skattemelding-formen for rask navigering
    #  3) Phase 4-logikken skipper syntetisk 2080-linje når dette finnes
    closing_entry_id = fields.Many2one(
        'account.move',
        string="Avslutningsbilag",
        copy=False,
        readonly=True,
        ondelete='set null',
        help="Bokført årsavslutningsbilag (lukker P&L til balanse-EK). "
             "Opprettes via 'Opprett avslutningsbilag'-knappen. Hvis "
             "bilaget slettes via account.move-formen, settes denne til "
             "tom — neste 'Opprett avslutningsbilag' lager nytt bilag.",
    )
    closing_entry_count = fields.Integer(
        compute='_compute_closing_entry_count',
        compute_sudo=True,
        help="Smart-button-teller for avslutningsbilag (0 eller 1).",
    )
    xml_stale_due_to_closing = fields.Boolean(
        compute='_compute_xml_stale_due_to_closing',
        compute_sudo=True,
        help="True hvis et postert avslutningsbilag er skrevet ETTER at "
             "XML-en ble generert. Brukeren må regenerere XML for at "
             "balansen i innsendingen skal matche det nye bilaget. "
             "Vises som warning-banner i UI.",
    )

    _company_year_uniq = models.Constraint(
        'unique(company_id, inntektsaar)',
        'Det kan kun finnes én skattemelding-innsending per selskap per år. '
        'For korreksjon, åpne den eksisterende og bruk action "Lag korreksjon".',
    )

    @api.depends('closing_entry_id')
    def _compute_closing_entry_count(self):
        for r in self:
            r.closing_entry_count = 1 if r.closing_entry_id else 0

    @api.depends(
        'closing_entry_id', 'closing_entry_id.state',
        'closing_entry_id.write_date', 'xml_generated_at',
    )
    def _compute_xml_stale_due_to_closing(self):
        """True hvis closing-bilag postert ETTER siste XML-generering.

        Brukeren må klikke 'Generer XML' på nytt for at balansen i
        skattemelding-XML skal reflektere ekte 2050/2080-saldoer fra
        bilaget. Vises som warning-banner i UI.
        """
        for r in self:
            entry = r.closing_entry_id
            stale = (
                entry
                and entry.state == 'posted'
                and r.xml_generated_at
                and entry.write_date
                and entry.write_date > r.xml_generated_at
            )
            r.xml_stale_due_to_closing = bool(stale)

    @api.depends('company_id', 'inntektsaar')
    def _compute_display_name(self):
        for r in self:
            r.display_name = f"{r.company_id.name} skattemelding {r.inntektsaar}"

    def _compute_avvik_count(self):
        # Phase 2 vil legge til avvik-modell. For nå: returner 0.
        for r in self:
            r.avvik_count = 0

    # ---------- XML pretty-print for display ----------

    @api.depends('skattemelding_xml', 'naeringsspesifikasjon_xml', 'konvolutt_xml')
    def _compute_xml_displays(self):
        """Pretty-print XML for display-feltene.

        Lagrer rå XML uendret (Skatteetaten kanoniserer ved validering),
        men viser pen versjon i UI for revisjon.
        """
        for r in self:
            r.skattemelding_xml_display = self._pretty_xml(r.skattemelding_xml)
            r.naeringsspesifikasjon_xml_display = self._pretty_xml(
                r.naeringsspesifikasjon_xml)
            r.konvolutt_xml_display = self._pretty_xml(r.konvolutt_xml)

    @api.model
    def _pretty_xml(self, xml_str):
        if not xml_str:
            return False
        try:
            from lxml import etree
            parser = etree.XMLParser(remove_blank_text=True)
            tree = etree.fromstring(xml_str.encode('utf-8'), parser)
            return etree.tostring(
                tree, pretty_print=True, xml_declaration=True,
                encoding='UTF-8',
            ).decode('utf-8')
        except Exception:
            return xml_str

    # ---------- XML-nedlasting ----------

    def action_l10n_no_skattemelding_download_xml(self):
        """Last ned XML-payload som fil.

        Brukes via xml_type i context: 'skattemelding', 'naeringsspesifikasjon',
        eller 'konvolutt'. Returnerer act_url som peker på en custom controller
        som streamer XML direkte — vi bruker IKKE /web/content fordi Odoo.sh
        dev-miljøet returnerer 503 der (verifisert 2026-05-12).
        """
        self.ensure_one()
        xml_type = self.env.context.get('xml_type') or 'konvolutt'
        if xml_type not in ('skattemelding', 'naeringsspesifikasjon', 'konvolutt'):
            xml_type = 'konvolutt'

        # Sjekk at vi faktisk har XML å laste ned
        xml_field = {
            'skattemelding': self.skattemelding_xml,
            'naeringsspesifikasjon': self.naeringsspesifikasjon_xml,
            'konvolutt': self.konvolutt_xml,
        }[xml_type]
        if not xml_field:
            from odoo.exceptions import UserError
            raise UserError(_(
                "Ingen %(t)s-XML lagret enda. Klikk 'Generer XML' først.",
                t=xml_type,
            ))

        return {
            'type': 'ir.actions.act_url',
            'url': f'/l10n_no_skattemelding/download/{self.id}/{xml_type}',
            'target': 'self',
        }

    # ---------- Validation summary (lesbar respons fra Skatteetaten) ----------

    @api.depends('last_response')
    def _compute_validation_summary(self):
        """Bygg et lesbart HTML-sammendrag av Skatteetatens respons.

        Skatteetaten emitter to formater avhengig av endepunkt:
          - XML: <skattemeldingOgNaeringsspesifikasjonResponse> fra
            valider-jobben — inneholder resultatAvValidering + avvik +
            veiledningEtterKontroll/veiledning med betjeningsstrategi
            ('faktiskFeil' blokkerer, 'merknadStandard' er bare råd).
          - JSON: Altinn instans-respons fra kvittering-henting —
            inneholder process.ended/currentTask + status.

        Returnerer HTML formattert som:
          - Header med resultat (validert OK / med feil)
          - Liste over blokkerende avvik (rød)
          - Liste over advisory-veiledninger (gul)
          - Plain status-info for Altinn-respons
        """
        for r in self:
            body = r.last_response
            if not body:
                r.validation_summary = False
                continue

            body_str = body if isinstance(body, str) else str(body)
            stripped = body_str.lstrip()

            # Merged Altinn JSON + tilbakemelding-XML (etter
            # auto-fetch_tilbakemelding). Vi prioriterer tilbakemelding.
            if '=== Skatteetatens tilbakemelding' in body_str:
                tilbake_idx = body_str.find('=== Skatteetatens tilbakemelding')
                tilbake_body = body_str[tilbake_idx:]
                altinn_section = body_str[:tilbake_idx]
                r.validation_summary = (
                    self._format_validation_xml_summary(tilbake_body)
                    + self._format_altinn_json_summary(
                        altinn_section.split('===', 2)[-1]
                        if altinn_section.startswith('=== Altinn')
                        else altinn_section
                    )
                )
                continue

            # Detektér JSON (Altinn kvittering-respons)
            if stripped.startswith('{'):
                r.validation_summary = self._format_altinn_json_summary(body_str)
                continue

            # Detektér XML (Skatteetaten valideringsrespons)
            if stripped.startswith('<') and 'skattemeldingOgNaeringsspesifikasjonResponse' in body_str:
                r.validation_summary = self._format_validation_xml_summary(body_str)
                continue

            # Detektér XML (Skatteetaten forespoersel-respons fra "Hent
            # partsnummer"-actionen — inneholder base64-encoded utkast-XML
            # med partsnummer + inntektsår, ikke en validate-respons).
            # MERK: ulik tag-streng fra validate (forespoersel vs ikke).
            if (stripped.startswith('<')
                    and 'skattemeldingOgNaeringsspesifikasjonforespoerselResponse' in body_str):
                r.validation_summary = self._format_forespoersel_xml_summary(body_str)
                continue

            # Ukjent format — vis som plain pre-tekst, trimmet
            r.validation_summary = (
                '<div class="alert alert-warning" role="alert">'
                '<strong>Ukjent respons-format</strong>'
                '<br/>Se rå-respons under for detaljer.</div>'
            )

    @api.model
    def _format_altinn_json_summary(self, body_str):
        """Format Altinn instans-JSON som lesbar HTML."""
        try:
            data = json.loads(body_str)
        except (json.JSONDecodeError, ValueError):
            return (
                '<div class="alert alert-warning" role="alert">'
                'Kunne ikke parse JSON-respons.</div>'
            )

        process = data.get('process') or {}
        ended = process.get('ended')
        current_task = (process.get('currentTask') or {}).get('elementId')
        end_event = process.get('endEvent')
        status = data.get('status') or {}
        is_archived = status.get('isArchived')

        parts = []
        if ended:
            parts.append(
                '<div class="alert alert-success" role="alert">'
                '<strong>Kvittering mottatt fra Altinn</strong><br/>'
                f'Prosess avsluttet: {html_escape(str(ended))}'
                + (f'<br/>Endepunkt: {html_escape(str(end_event))}' if end_event else '')
                + ('<br/>Arkivert: Ja' if is_archived else '')
                + '</div>'
            )
        else:
            # P1 #1 fix: differensiere melding basert på Task. Task_1 og
            # Task_2 er bruker-aksjoner i Altinn-portalen, Task_3+ er
            # Skatteetatens behandling. Tidligere viste vi "Skatteetaten
            # behandler" for alle task-er — forvirrende på Task_2 der
            # bruker fortsatt har et steg igjen.
            task_str = str(current_task or 'ukjent')
            if task_str == 'Task_1':
                # Utfylling — bruker har ikke klikket "Videre" ennå
                msg = (
                    '<strong>Utkast venter på din signering i Altinn</strong>'
                    '<br/>Nåværende oppgave: <code>Task_1 (Utfylling)</code>'
                    '<br/>Klikk <strong>Åpne i Altinn for signering</strong> '
                    'over for å gjennomføre signeringen i nettleseren.'
                )
            elif task_str == 'Task_2':
                # Bekreftelse — bruker har klikket "Videre" men ikke "Send inn"
                msg = (
                    '<strong>Venter på din endelige bekreftelse i Altinn</strong>'
                    '<br/>Nåværende oppgave: <code>Task_2 (Bekreftelse)</code>'
                    '<br/>Gå tilbake til Altinn-fanen og klikk '
                    '<strong>"Se opplysninger og send inn"</strong> → '
                    '<strong>"Send inn"</strong> i Skatteetatens visning. '
                    'Først da går innsendingen videre til Skatteetatens '
                    'behandling.'
                )
            else:
                # Task_3+ eller ukjent — Skatteetaten behandler
                msg = (
                    f'<strong>Skatteetaten behandler innsendingen</strong>'
                    f'<br/>Nåværende oppgave: <code>{html_escape(task_str)}</code>'
                    '<br/>Kvittering kommer typisk innen 3–10 minutter. '
                    'Vi sjekker status automatisk hvert 10. sekund.'
                )
            parts.append(
                f'<div class="alert alert-info" role="alert">{msg}</div>'
            )
        return ''.join(parts)

    @api.model
    def _format_forespoersel_xml_summary(self, body_str):
        """Format Skatteetatens forespoersel-respons som lesbar HTML.

        Returneres fra 'Hent partsnummer'-actionen — inneholder
        ``<dokumenter><skattemeldingdokument>`` med base64-encoded
        skattemelding-utkast-XML. Vi dekoder content og trekker ut
        partsnummer + inntektsår for visning.

        Bruker regex (ikke lxml) for robusthet mot namespace-variasjoner.
        """
        # Hent ut <content>-elementet (base64 av indre XML)
        content_m = re.search(
            r'<(?:[a-zA-Z0-9]+:)?content>\s*([A-Za-z0-9+/=\s]+?)\s*'
            r'</(?:[a-zA-Z0-9]+:)?content>',
            body_str,
        )
        partsnummer = '–'
        inntektsaar = '–'
        if content_m:
            try:
                inner_xml = base64.b64decode(content_m.group(1)).decode('utf-8')
                p_m = re.search(
                    r'<(?:[a-zA-Z0-9]+:)?partsnummer>(\d+)'
                    r'</(?:[a-zA-Z0-9]+:)?partsnummer>',
                    inner_xml,
                )
                if p_m:
                    partsnummer = p_m.group(1)
                a_m = re.search(
                    r'<(?:[a-zA-Z0-9]+:)?inntektsaar>(\d+)'
                    r'</(?:[a-zA-Z0-9]+:)?inntektsaar>',
                    inner_xml,
                )
                if a_m:
                    inntektsaar = a_m.group(1)
            except (ValueError, UnicodeDecodeError):
                pass

        # Hent ut dokument-id og type (utenfor base64-content)
        id_m = re.search(
            r'<(?:[a-zA-Z0-9]+:)?id>([A-Z0-9:]+)</(?:[a-zA-Z0-9]+:)?id>',
            body_str,
        )
        type_m = re.search(
            r'<(?:[a-zA-Z0-9]+:)?type>(\w+)</(?:[a-zA-Z0-9]+:)?type>',
            body_str,
        )
        dok_id = id_m.group(1) if id_m else '–'
        dok_type = type_m.group(1) if type_m else '–'

        return (
            '<div class="alert alert-success" role="alert">'
            '<strong>Partsnummer hentet fra Skatteetaten</strong></div>'
            '<div class="card mt-2"><div class="card-body">'
            '<table class="table table-sm">'
            f'<tr><th>Partsnummer:</th><td><code>{html_escape(partsnummer)}</code></td></tr>'
            f'<tr><th>Inntektsår:</th><td>{html_escape(inntektsaar)}</td></tr>'
            f'<tr><th>Dokument-ID:</th><td><code>{html_escape(dok_id)}</code></td></tr>'
            f'<tr><th>Dokument-type:</th><td>{html_escape(dok_type)}</td></tr>'
            '</table></div></div>'
        )

    @api.model
    def _format_validation_xml_summary(self, body_str):
        """Format Skatteetatens valideringsrespons-XML som lesbar HTML.

        Bruker regex (ikke lxml) for robusthet mot namespace-variasjoner.
        Henter ut:
          - resultatAvValidering (validertOK / validertMedFeil)
          - <avvik>-elementer (blokkerende)
          - <veiledning>-elementer med betjeningsstrategi + hjelpetekst
        """
        # Resultat
        result_m = re.search(
            r'<(?:[a-zA-Z0-9]+:)?resultatAvValidering>\s*'
            r'(validertOK|validertMedFeil)\s*'
            r'</(?:[a-zA-Z0-9]+:)?resultatAvValidering>',
            body_str,
        )
        result = result_m.group(1) if result_m else None

        # aarsakTilValidertMedFeil — Skatteetatens overordnet avslags-årsak
        aarsak_m = re.search(
            r'<(?:[a-zA-Z0-9]+:)?aarsakTilValidertMedFeil>\s*'
            r'([^<]+?)\s*'
            r'</(?:[a-zA-Z0-9]+:)?aarsakTilValidertMedFeil>',
            body_str,
        )
        aarsak = aarsak_m.group(1).strip() if aarsak_m else ''

        # Header
        if result == 'validertOK':
            header = (
                '<div class="alert alert-success" role="alert">'
                '<strong>Skatteetaten godkjente innsendingen</strong> '
                '(resultatAvValidering = validertOK)<br/>'
                'Skattemeldingen er endelig levert og arkivert.</div>'
            )
        elif result == 'validertMedFeil':
            # Sett enklere/menneskelig melding for kjente avslagsårsaker
            aarsak_friendly = self._friendly_aarsak_message(aarsak)
            aarsak_html = ''
            if aarsak:
                aarsak_html = (
                    f'<br/><strong>Årsak:</strong> '
                    f'<code>{html_escape(aarsak)}</code>'
                )
                if aarsak_friendly:
                    aarsak_html += (
                        f'<br/><strong>Forklaring:</strong> '
                        f'{html_escape(aarsak_friendly)}'
                    )
            header = (
                '<div class="alert alert-danger" role="alert">'
                '<strong>Skatteetaten avviste innsendingen</strong> '
                '(resultatAvValidering = validertMedFeil)'
                + aarsak_html
                + '</div>'
            )
        else:
            header = (
                '<div class="alert alert-warning" role="alert">'
                'Ukjent valideringsresultat — sjekk rå-respons.</div>'
            )

        # <avvik>-elementer. NB: Skatteetaten bruker "avvik" semantisk
        # for både blokkerende feil OG "verdier hvor Skatteetaten har
        # beregnet noe annet enn det innsendte". Når resultatAvValidering=
        # validertOK er disse INFO-poster, ikke feil. Vi tar med strategien
        # for å skille.
        avvik_blocks = []
        for m in re.finditer(
            r'<(?:[a-zA-Z0-9]+:)?avvik(?:\s[^>]*)?>(.*?)</(?:[a-zA-Z0-9]+:)?avvik>',
            body_str, re.DOTALL,
        ):
            inner = m.group(1)
            info = self._extract_feil_info(inner)
            info['strategi'] = ''  # avvik har ingen strategi
            avvik_blocks.append(info)

        # Veiledning (advisory eller faktiske feil)
        faktisk_feil = []
        merknader = []
        for m in re.finditer(
            r'<(?:[a-zA-Z0-9]+:)?veiledning(?:\s[^>]*)?>(.*?)</(?:[a-zA-Z0-9]+:)?veiledning>',
            body_str, re.DOTALL,
        ):
            inner = m.group(1)
            strategi_m = re.search(
                r'<(?:[a-zA-Z0-9]+:)?betjeningsstrategi>\s*([^<]+?)\s*'
                r'</(?:[a-zA-Z0-9]+:)?betjeningsstrategi>',
                inner,
            )
            strategi = (strategi_m.group(1).strip() if strategi_m else '').strip()
            info = self._extract_feil_info(inner)
            info['strategi'] = strategi
            if strategi == 'faktiskFeil':
                faktisk_feil.append(info)
            else:
                merknader.append(info)

        parts = [header]

        if result == 'validertMedFeil' and (avvik_blocks or faktisk_feil):
            # Faktiske blokkerende feil
            parts.append('<h4 style="margin-top:12px;">Blokkerende feil</h4>')
            parts.append('<ul>')
            for info in avvik_blocks + faktisk_feil:
                parts.append(self._render_feil_li(info, severity='danger'))
            parts.append('</ul>')

        if merknader:
            parts.append('<h4 style="margin-top:12px;">'
                         f'Veiledninger ({len(merknader)} stk. — ikke-blokkerende)</h4>')
            parts.append(
                '<p><i>Disse er Skatteetatens råd som du <b>kan</b> rette, men '
                'innsendingen lykkes også uten.</i></p>'
            )
            parts.append('<ul>')
            for info in merknader:
                parts.append(self._render_feil_li(info, severity='warning'))
            parts.append('</ul>')

        # Når validertOK + avvik finnes, er disse beregnings-info fra
        # Skatteetaten — ikke feil. Vis dem kollapsbart for revisjon.
        if result == 'validertOK' and avvik_blocks:
            parts.append(
                f'<details style="margin-top:12px;">'
                f'<summary><b>Skatteetatens beregnede verdier</b> '
                f'({len(avvik_blocks)} poster) — klikk for å vise</summary>'
                f'<p><i>Disse er verdier Skatteetaten har beregnet eller '
                f'normalisert basert på innsendt XML. Ikke feil — '
                f'kun til revisjon.</i></p><ul>'
            )
            for info in avvik_blocks:
                parts.append(self._render_feil_li(info, severity='info'))
            parts.append('</ul></details>')

        if not avvik_blocks and not faktisk_feil and not merknader and result == 'validertOK':
            parts.append('<p><i>Ingen merknader fra Skatteetaten.</i></p>')

        return ''.join(parts)

    @api.model
    def _friendly_aarsak_message(self, aarsak_kode):
        """Oversett aarsakTilValidertMedFeil-koder til norsk forklaring."""
        mapping = {
            'innkommendeForespoerselManglerSporTilUtfoerende': (
                "Innsendingen mangler informasjon om HVEM som har utført "
                "innsendingen (personen bak handlingen). Skatteetaten "
                "krever dette av revisjonshensyn. Sjekk at "
                "Maskinporten-token har 'utførende person' identifisert, "
                "eller om XML-konvolutten må inkludere "
                "utfoerende-element."
            ),
            'tomXml': "Innsendt XML er tom.",
            'ugyldigXml': "Innsendt XML er ikke gyldig mot skjemaet.",
            'partsnummerErIkkePerson': (
                "Partsnummer i innsending matcher ikke skattepliktig "
                "person/selskap."),
        }
        return mapping.get(aarsak_kode, '')

    @api.model
    def _extract_feil_info(self, inner_xml):
        """Hent regelnavn, hjelpetekst, sti, regelId, beregnetVerdi fra et
        <avvik>- eller <veiledning>-element."""
        def grab(tag):
            m = re.search(
                rf'<(?:[a-zA-Z0-9]+:)?{tag}>\s*([^<]+?)\s*</(?:[a-zA-Z0-9]+:)?{tag}>',
                inner_xml, re.DOTALL,
            )
            return m.group(1).strip() if m else ''

        return {
            'regelnavn': grab('regelnavn') or grab('navn') or grab('feilnavn'),
            'regel_id': grab('regelId') or grab('regelid'),
            'hjelpetekst': grab('hjelpetekst') or grab('beskrivelse') or grab('feilmelding'),
            'sti': grab('sti') or grab('feilLokalisering'),
            'beregnet_verdi': grab('beregnetVerdi'),
            'forekomst': grab('forekomstidentifikator'),
            'strategi': '',
        }

    # Lookup-tabell: oversetter Skatteetatens XPath-stier til lesbare
    # norske labels. Brukt i validation_summary-rendering så brukeren
    # ser "Sum egenkapital: -40 232 kr" istedenfor en kryptisk path.
    # Utvid ved behov når nye stier dukker opp i prod-tilbakemeldinger.
    _STI_LABEL_MAP = {
        'inntektOgUnderskudd/underskuddTilFremfoering/fremfoerbartUnderskuddIInntekt/beloep/beloepSomHeltall':
            'Fremførbart underskudd',
        'inntektOgUnderskudd/samletUnderskudd/beloep/beloepSomHeltall':
            'Samlet underskudd',
        'inntektOgUnderskudd/inntektFoerFradragForEventueltAvgittKonsernbidrag/beloepSomHeltall':
            'Inntekt før fradrag for konsernbidrag',
        'balanseregnskap/omloepsmiddel/sumBalanseverdiForOmloepsmiddel/beloep/beloep':
            'Sum omløpsmidler',
        'beregnetNaeringsinntekt/skattemessigResultat/beloep/beloep':
            'Skattemessig resultat',
        'balanseregnskap/gjeldOgEgenkapital/sumEgenkapital/beloep/beloep':
            'Sum egenkapital',
        'balanseregnskap/sumBalanseverdiForEiendel/beloep/beloep':
            'Sum eiendeler',
        'balanseregnskap/anleggsmiddel/sumBalanseverdiForAnleggsmiddel/beloep/beloep':
            'Sum anleggsmidler',
        'balanseregnskap/gjeldOgEgenkapital/sumKortsiktigGjeld/beloep/beloep':
            'Sum kortsiktig gjeld',
        'balanseregnskap/sumGjeldOgEgenkapital/beloep/beloep':
            'Sum gjeld og egenkapital',
        'beregnetNaeringsinntekt/fordeltBeregnetNaeringsinntektForUpersonligSkattepliktig/fordeltSkattemessigResultatEtterKorreksjon/beloep/beloep':
            'Fordelt skattemessig resultat (etter korreksjon)',
        'resultatregnskap/aarsresultat/beloep/beloep':
            'Årsresultat',
        'beregnetNaeringsinntekt/fordeltBeregnetNaeringsinntektForUpersonligSkattepliktig/fordeltSkattemessigResultat/beloep/beloep':
            'Fordelt skattemessig resultat',
        'resultatregnskap/driftskostnad/sumDriftskostnad/beloep/beloep':
            'Sum driftskostnader',
    }

    @api.model
    def _sti_to_label(self, sti):
        """Oversett Skatteetaten-XPath-sti til lesbar norsk label.

        Spesialtilfeller:
          - 'global' eller tom sti → returnerer '' (veiledninger på
            dokument-nivå har ikke en konkret sti — gir ingen mening
            å vise 'Global' som tittel).

        Fallback: ta siste meningsfulle segment av sti og lag en label av
        det (eks: 'sumDriftskostnad' → 'Sum driftskostnad'). Mer
        informativt enn å vise rå XPath, og vi unngår å vedlikeholde
        en uttømmende ordbok.
        """
        if not sti or sti == 'global':
            return ''
        if sti in self._STI_LABEL_MAP:
            return self._STI_LABEL_MAP[sti]
        # Fallback: konverter camelCase siste segment til readable text
        segments = [s for s in sti.split('/') if s not in ('beloep', 'beloepSomHeltall')]
        last = segments[-1] if segments else sti
        # camelCase → "Camel Case" enkelt heuristikk
        readable = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', last)
        # Også PascalCase til "Pascal Case" og "InnInn" → "Inn Inn"
        readable = re.sub(r'(?<=[A-Z])(?=[A-Z][a-z])', ' ', readable)
        return readable[:1].upper() + readable[1:] if readable else sti

    @api.model
    def _format_beregnet_verdi(self, value):
        """Formater beregnetVerdi som tall hvis numerisk, ellers som-er."""
        if not value:
            return ''
        # Strip whitespace
        v = value.strip()
        try:
            num = float(v)
            if num.is_integer():
                # Norsk tusenskille
                return f"{int(num):,}".replace(',', ' ') + ' kr'
            return f"{num:,.2f}".replace(',', ' ').replace('.', ',') + ' kr'
        except (ValueError, TypeError):
            return v

    # ---------- Altinn substatus (Godkjent / Avvist) ----------

    @api.depends(
        'last_response', 'tilbakemelding_xml', 'altinn_instance_json', 'state',
    )
    def _compute_altinn_substatus(self):
        """Hent substatus.label fra Altinn instans-respons + tilbakemelding-XML.

        Tre kilder, i prioritert rekkefølge:
          1. tilbakemelding_xml (dedikert felt, mest autoritativ)
             - <resultatAvValidering>validertOK</> → godkjent
             - <resultatAvValidering>validertMedFeil</> → avvist
          2. altinn_instance_json (dedikert felt med Altinn-respons)
             - status.substatus.label
          3. Legacy fallback: parse last_response (gamle records uten
             dedikerte felter — kan fjernes etter migration).
          4. State (submitted/mottatt) for pending
        """
        for r in self:
            label = ''
            description = ''
            final = 'pending'

            if r.state not in ('submitted', 'mottatt'):
                r.altinn_substatus_label = False
                r.altinn_substatus_description = False
                r.skatteetaten_endelig_status = 'pending' if r.state in ('uploaded', 'submitted') else False
                continue

            # Kilde 1: tilbakemelding_xml (dedikert felt — autoritativ)
            tilbakemelding_body = r.tilbakemelding_xml or ''
            # Legacy fallback: tilbakemelding kunne ligge i last_response
            # før vi splittet til dedikerte felter
            if not tilbakemelding_body and r.last_response:
                idx = r.last_response.find('=== Skatteetatens tilbakemelding')
                if idx >= 0:
                    tilbakemelding_body = r.last_response[idx:]

            if tilbakemelding_body:
                result_m = re.search(
                    r'<(?:[a-zA-Z0-9]+:)?resultatAvValidering>\s*'
                    r'(validertOK|validertMedFeil)\s*'
                    r'</(?:[a-zA-Z0-9]+:)?resultatAvValidering>',
                    tilbakemelding_body,
                )
                if result_m:
                    if result_m.group(1) == 'validertOK':
                        final = 'godkjent'
                        label = 'Godkjent'
                    else:
                        final = 'avvist'
                        label = 'Avvist'
                        aarsak_m = re.search(
                            r'<(?:[a-zA-Z0-9]+:)?aarsakTilValidertMedFeil>\s*'
                            r'([^<]+?)\s*'
                            r'</(?:[a-zA-Z0-9]+:)?aarsakTilValidertMedFeil>',
                            tilbakemelding_body,
                        )
                        if aarsak_m:
                            description = aarsak_m.group(1).strip()

            # Kilde 2: altinn_instance_json (dedikert felt med substatus)
            if not label:
                altinn_json = r.altinn_instance_json or ''
                # Legacy fallback: JSON kunne ligge i last_response
                if not altinn_json and r.last_response \
                        and r.last_response.lstrip().startswith('{'):
                    altinn_json = r.last_response.split('===', 1)[0].strip() \
                        or r.last_response
                if altinn_json:
                    try:
                        data = json.loads(altinn_json)
                        substatus = (data.get('status') or {}).get('substatus') or {}
                        label = substatus.get('label') or ''
                        description = substatus.get('description') or ''
                        if label == 'Godkjent':
                            final = 'godkjent'
                        elif label == 'Avvist':
                            final = 'avvist'
                        elif r.state == 'mottatt':
                            final = 'ukjent'
                    except (json.JSONDecodeError, ValueError):
                        pass

            if r.state == 'mottatt' and final == 'pending':
                final = 'ukjent'

            r.altinn_substatus_label = label or False
            r.altinn_substatus_description = description or False
            r.skatteetaten_endelig_status = final

    # ---------- Onboarding-checklist ----------

    @api.depends(
        'state', 'partsnummer', 'skattemelding_xml', 'konvolutt_xml',
        'company_id.l10n_no_skattemelding_aktivert',
    )
    def _compute_onboarding_checklist(self):
        """Bygg sjekkliste-banner med konkrete neste steg.

        Vises på toppen av form-en så account-manager ser hva som
        mangler uten å lese inn hver enkelt knapp.
        """
        for r in self:
            # Hopp over avsluttede records
            if r.state in ('submitted', 'mottatt'):
                r.onboarding_checklist = False
                continue

            aktivert = bool(r.company_id.l10n_no_skattemelding_aktivert)
            har_parts = bool(r.partsnummer)
            har_xml = bool(r.skattemelding_xml and r.konvolutt_xml)
            er_validert = r.state in ('validated', 'uploaded')

            items = [
                ('Skattemelding aktivert i Altinn', aktivert,
                 'Aktiver på Selskaps-skjemaet (Skattemelding-fane → '
                 '"Aktiver Skattemelding...")'),
                ('Partsnummer hentet fra Skatteetaten', har_parts,
                 'Klikk "Hent partsnummer fra Skatteetaten"'),
                ('XML generert', har_xml,
                 'Klikk "Generer XML"'),
                ('Validert mot Skatteetaten', er_validert,
                 'Klikk "Valider mot Skatteetaten"'),
            ]

            # Alle steg fullført → vis "Klar for innsending"
            if all(done for _, done, _ in items):
                r.onboarding_checklist = (
                    '<div class="alert alert-success" role="alert" '
                    'style="margin-bottom:8px;">'
                    '<strong>Klar for innsending</strong> — '
                    'klikk <i>Send til Altinn for signering</i> i toppmenyen.'
                    '</div>'
                )
                continue

            parts = [
                '<div class="alert alert-info" role="alert" '
                'style="margin-bottom:8px;">'
                '<strong>Status — neste steg:</strong>'
                '<ul style="margin-bottom:0;margin-top:4px;">'
            ]
            for label, done, hint in items:
                marker = ('✓' if done else '☐')
                color = '#28a745' if done else '#6c757d'
                style = (
                    f'color:{color};'
                    + ('text-decoration:line-through;' if done else '')
                )
                if done:
                    parts.append(
                        f'<li style="{style}">{marker} {html_escape(label)}</li>'
                    )
                else:
                    parts.append(
                        f'<li><span style="{style}">{marker} '
                        f'{html_escape(label)}</span>'
                        f' — <i>{html_escape(hint)}</i></li>'
                    )
            parts.append('</ul></div>')
            r.onboarding_checklist = ''.join(parts)

    @api.model
    def _render_feil_li(self, info, severity='warning'):
        """Render en feil-info som <li> HTML.

        Foretrukket innhold (i prioritert rekkefølge):
          1. regelnavn / regel_id (hvis satt — typisk for blokkerende feil)
          2. Lesbar sti-label via _sti_to_label (for info-poster med
             tom regelnavn — typisk Skatteetatens beregnede verdier)

        For 'info'-poster (Skatteetatens beregnede verdier) vises også
        beregnetVerdi som et formattert tall slik at brukeren faktisk
        ser HVA Skatteetaten beregnet, ikke bare hvor verdien havner.
        """
        sti_raw = info.get('sti') or ''
        navn_raw = info.get('regelnavn') or info.get('regel_id') or ''
        if not navn_raw:
            navn_raw = self._sti_to_label(sti_raw)
        # navn kan være tom for veiledninger med sti='global' og tomt
        # regelnavn — da skipper vi <b>navn</b> og viser kun hjelpeteksten
        # (som er det meningsfulle innholdet for slike merknader).
        navn = html_escape(navn_raw) if navn_raw else ''
        hjelp = html_escape(info.get('hjelpetekst') or '')
        sti = html_escape(sti_raw) if sti_raw and sti_raw != 'global' else ''
        beregnet_verdi_raw = info.get('beregnet_verdi') or ''
        beregnet_verdi = html_escape(
            self._format_beregnet_verdi(beregnet_verdi_raw)
        )
        color = {
            'danger': '#dc3545',
            'warning': '#856404',
            'info': '#0c5460',
        }.get(severity, '#856404')
        parts = ['<li>']
        if navn:
            parts.append(f'<b style="color:{color};">{navn}</b>')
            if beregnet_verdi:
                # Vis verdien prominently — det er kjernen i info-posten
                parts.append(
                    f': <b style="color:{color};">{beregnet_verdi}</b>'
                )
            if hjelp:
                parts.append(f'<br/>{hjelp}')
        else:
            # Ingen meningsfull tittel — vis hjelpeteksten direkte
            if hjelp:
                parts.append(hjelp)
            if beregnet_verdi:
                parts.append(
                    f' <b style="color:{color};">({beregnet_verdi})</b>'
                )
        if sti and sti != navn:
            parts.append(
                f'<br/><small style="color:#999;"><code>{sti}</code></small>'
            )
        parts.append('</li>')
        return ''.join(parts)
