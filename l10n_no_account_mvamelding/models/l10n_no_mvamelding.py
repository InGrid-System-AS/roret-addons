"""MVA-melding hovedmodell — én record per selskap + termin.

Datamodellen speiler l10n.no.skattemelding: en state-maskin som tar en
periode fra rådata (Odoos Tax Report) → XML → validering → Altinn-innsending
→ kvittering. Feltene for senere faser (Altinn-GUID-er, kvittering) er
definert her med en gang slik at validate/submit/kvittering-modulene kun
legger til metoder via _inherit — ingen schema-migrering mellom faser.

Periode-modellen: norsk MVA leveres normalt på bimånedlige terminer (6 i
året). `periode`-feltet holder termin-nummer (1–6) eller 'aarlig'. Hjelperen
_periode_spec() oversetter til (XSD-element, XSD-verdi, dato-fra, dato-til)
som både datauttrekk (rapport-intervall) og XML-bygging trenger.
"""
import calendar
import logging
import uuid

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Termin → (periode-element i XSD, enum-verdi, (mnd_fra, mnd_til)).
# Element/verdi verifisert mot Skatteetaten/mva-meldingen XSD:
#   periode er en choice; bimånedlig = <skattleggingsperiodeToMaaneder> med
#   verdi 'januar-februar' osv. Årlig = <skattleggingsperiodeAar>aarlig.
_PERIODE_SPEC = {
    '1': ('skattleggingsperiodeToMaaneder', 'januar-februar', (1, 2)),
    '2': ('skattleggingsperiodeToMaaneder', 'mars-april', (3, 4)),
    '3': ('skattleggingsperiodeToMaaneder', 'mai-juni', (5, 6)),
    '4': ('skattleggingsperiodeToMaaneder', 'juli-august', (7, 8)),
    '5': ('skattleggingsperiodeToMaaneder', 'september-oktober', (9, 10)),
    '6': ('skattleggingsperiodeToMaaneder', 'november-desember', (11, 12)),
    'aarlig': ('skattleggingsperiodeAar', 'aarlig', (1, 12)),
}

# TOKEN-scope ved innsending: Altinn-plattform-scopet altinn:instances.write
# (Altinn-app-instans-API-et krever det) — IKKE skatteetaten:mvameldinginnsending
# (det er ID-porten-scopet for borger-flyten). Systembrukeren delegeres
# tilgangspakken `merverdiavgift` (app-ressursen er delegable:false).
SCOPE_INNSENDING = 'altinn:instances.write'

# ONBOARDING-nøkkel (det vi sender i scopes_text). Distinkt fra token-scopet
# fordi altinn:instances.write deles med årsregnskap — token-tjenestens
# scope_mapping gir denne nøkkelen merverdiavgift-pakken + tokenScope-override
# til altinn:instances.write. Egen nøkkel hindrer kollisjon med årsregnskap
# i delegering og i aktivert-flagget.
SCOPE_ONBOARDING = 'skatteetaten:mvamelding'

# Vi sender kun alminnelig mva-melding i v1 (jf. plan). Selection holdes
# åpen for senere primaernaering/omvendtAvgiftsplikt/kompensasjon.
MELDINGSKATEGORI_SELECTION = [
    ('alminnelig', 'Alminnelig'),
]

# Lesbar tekst per mvaKode. Brukes både i spesifikasjons-HTML-en og som
# etiketter i merknad-linjene, så de aldri kan drifte fra hverandre.
MVA_KODE_LABELS = {
        '1': 'Inngående MVA, fradrag (25%)',
        '11': 'Inngående MVA, fradrag (15%)',
        '12': 'Inngående MVA, fisk (11,11%)',
        '13': 'Inngående MVA, fradrag (12%)',
        '14': 'Innførsel varer, fradrag (25%)',
        '15': 'Innførsel varer, fradrag (15%)',
        '3': 'Utgående MVA, salg (25%)',
        '31': 'Utgående MVA, salg (15%)',
        '32': 'Utgående MVA, fisk (11,11%)',
        '33': 'Utgående MVA, salg (12%)',
        '5': 'Salg fritatt (0%)',
        '6': 'Salg unntatt (0%)',
        '51': 'Innenlands omsetning omvendt avgiftsplikt (0%)',
        '52': 'Eksport (0%)',
        '81': 'Innførsel varer m/fradrag (25%)',
        '82': 'Innførsel varer u/fradrag (25%)',
        '83': 'Innførsel varer m/fradrag (15%)',
        '84': 'Innførsel varer u/fradrag (15%)',
        '85': 'Innførsel varer (0%)',
        '86': 'Tjenester fra utlandet m/fradrag (25%)',
        '87': 'Tjenester fra utlandet u/fradrag (25%)',
        '88': 'Tjenester fra utlandet m/fradrag (12%)',
        '89': 'Tjenester fra utlandet u/fradrag (12%)',
        '91': 'Klimakvoter/gull m/fradrag (25%)',
        '92': 'Klimakvoter/gull u/fradrag (25%)',
    }

# R021: fradragskoder som KREVER merknad når linjen har motsatt fortegn
# (tomt grunnlag og positiv merverdiavgift). Kodesettet er hentet ordrett
# fra Skatteetatens regeldefinisjon — se docstringen i _koder_uten_merknad.
MVA_KODER_MERKNADSPLIKT_VED_MOTSATT_FORTEGN = frozenset(
    {'1', '11', '12', '13', '14', '15', '81', '83', '86', '88', '91'}
)


class L10nNoMvamelding(models.Model):
    _name = 'l10n.no.mvamelding'
    _description = 'MVA-melding-innsending til Skatteetaten'
    _order = 'aar desc, periode desc, id desc'
    _rec_name = 'display_name'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    company_id = fields.Many2one(
        'res.company', string="Selskap", required=True, index=True,
        default=lambda self: self.env.company,
    )
    aar = fields.Integer(
        string="År", required=True,
        default=lambda self: fields.Date.context_today(self).year,
    )
    periode = fields.Selection(
        [
            ('1', '1. termin (jan–feb)'),
            ('2', '2. termin (mar–apr)'),
            ('3', '3. termin (mai–jun)'),
            ('4', '4. termin (jul–aug)'),
            ('5', '5. termin (sep–okt)'),
            ('6', '6. termin (nov–des)'),
            ('aarlig', 'Årstermin'),
        ],
        string="Termin", required=True, default='1',
    )
    meldingskategori = fields.Selection(
        MELDINGSKATEGORI_SELECTION, string="Meldingskategori",
        default='alminnelig', required=True,
    )
    state = fields.Selection(
        [
            ('draft', 'Utkast'),
            ('generated', 'XML generert'),
            ('validated', 'Validert'),
            ('submitting', 'Sender inn'),
            ('submitted', 'Sendt inn'),
            ('mottatt', 'Mottatt'),
            ('avvist', 'Avvist'),
            ('error', 'Feil'),
        ],
        string="Status", default='draft', required=True, copy=False,
        tracking=True,
    )

    # ---- innsendings-referanse (kreves i mvaMeldingDto/innsending) ----
    regnskapssystemsreferanse = fields.Char(
        string="Regnskapssystemsreferanse", copy=False, readonly=True,
        help="Unik referanse (UUID) for denne innsendingen, satt ved "
             "opprettelse. Sendes i mva-meldingens <innsending>-element så "
             "Skatteetaten kan idempotent-deduplisere.",
    )

    # ---- XML ----
    mvamelding_xml = fields.Text(string="MVA-melding XML", copy=False)
    konvolutt_xml = fields.Text(string="Konvolutt XML", copy=False)
    xml_generated_at = fields.Datetime(string="XML generert", copy=False)
    fastsatt_mva = fields.Monetary(
        string="Fastsatt MVA", currency_field='currency_id', copy=False,
        help="Netto merverdiavgift å betale (positiv) / til gode (negativ) "
             "for terminen, beregnet fra Tax Report.",
    )
    # Lesbar oversikt over spesifikasjonslinjene (UX: la bruker verifisere
    # tallene mot Tax Report før en bindende innsending — XML alene er ikke
    # menneskelesbart). Settes ved action_generate_xml.
    linjer_oversikt_html = fields.Html(
        string="Spesifikasjon", copy=False, readonly=True, sanitize=False,
    )
    # Speiler selskapets aktiveringsstatus inn på skjemaet, slik at brukeren
    # ser om innsending er mulig FØR de prøver (og får en CTA hvis ikke).
    company_mvamelding_aktivert = fields.Boolean(
        related='company_id.l10n_no_mvamelding_aktivert',
        string="Selskap aktivert for MVA-melding",
    )

    merknad_ids = fields.One2many(
        'l10n.no.mvamelding.merknad', 'mvamelding_id', string="Merknader",
        copy=False,
        help="Forklaringer knyttet til enkelt-spesifikasjonslinjer. "
             "Skatteetaten KREVER merknad når en fradragskode har motsatt "
             "fortegn (regel R021) — uten den avvises hele meldingen.",
    )

    # ---- validering ----
    valideringsresultat_xml = fields.Text(string="Valideringsresultat", copy=False)
    avvik_count = fields.Integer(string="Antall avvik", copy=False)
    validated_at = fields.Datetime(string="Validert", copy=False)

    # ---- Altinn-innsending (fylles i submit-fase) ----
    altinn_instance_owner_party_id = fields.Char(
        string="Altinn partyId", copy=False)
    altinn_instance_guid = fields.Char(string="Altinn instans-GUID", copy=False)
    altinn_konvolutt_element_guid = fields.Char(
        string="Altinn konvolutt-element-GUID", copy=False,
        help="ID-en til det forhånds-opprettede (tomme) konvolutt-data-"
             "elementet, fanget fra opprett-instans-svaret. Lar oss PUT-e "
             "konvolutten uten et separat GET (som race-er 404 i ~60s på TT02).",
    )
    altinn_konvolutt_data_guid = fields.Char(
        string="Altinn konvolutt-data-GUID", copy=False)
    altinn_mvamelding_data_guid = fields.Char(
        string="Altinn mva-melding-data-GUID", copy=False)
    altinn_process_current_task = fields.Char(
        string="Altinn-task", copy=False)
    submitted_at = fields.Datetime(string="Sendt inn", copy=False)
    mottatt_at = fields.Datetime(string="Mottatt", copy=False)
    kvittering_archived_at = fields.Datetime(
        string="Kvittering arkivert", copy=False,
        help="Idempotens-vakt: hindrer at gjentatte cron-tikk skaper "
             "duplikate kvittering-vedlegg/chatter-poster.",
    )

    # ---- kvittering / betaling (fylles i kvittering-fase) ----
    betalingsinformasjon_xml = fields.Text(string="Betalingsinformasjon", copy=False)
    betalingsbeloep = fields.Monetary(
        string="Beløp å betale", currency_field='currency_id', copy=False)
    betalingsfrist = fields.Date(string="Betalingsfrist", copy=False)
    betalingskonto = fields.Char(string="Betal til konto", copy=False)
    betalings_kid = fields.Char(string="KID", copy=False)

    last_response = fields.Text(string="Siste API-respons", copy=False)

    display_name = fields.Char(
        string="Navn", compute='_compute_display_name', store=True)
    currency_id = fields.Many2one(
        'res.currency', related='company_id.currency_id', readonly=True)

    _unique_periode = models.Constraint(
        'UNIQUE(company_id, aar, periode)',
        "Det finnes allerede en MVA-melding for dette selskapet, året og "
        "terminen.",
    )

    @api.depends('company_id', 'aar', 'periode')
    def _compute_display_name(self):
        labels = dict(self._fields['periode'].selection)
        for rec in self:
            rec.display_name = "%s — MVA %s %s" % (
                rec.company_id.name or '?', rec.aar or '?',
                labels.get(rec.periode, rec.periode or ''),
            )

    @api.model
    def default_get(self, fields_list):
        """Default til SISTE AVSLUTTEDE bimånedlige termin (den som forfaller
        nå), ikke 1. termin. En fersk bruker som oppretter en MVA-melding i
        dag vil nesten alltid rapportere terminen som nettopp er ferdig.
        """
        res = super().default_get(fields_list)
        today = fields.Date.context_today(self)
        # Termin under arbeid nå = ((mnd-1)//2)+1. Forfalt = den forrige.
        current_termin = (today.month - 1) // 2 + 1
        due_termin = current_termin - 1
        year = today.year
        if due_termin < 1:
            due_termin = 6
            year -= 1
        if 'periode' in fields_list:
            res['periode'] = str(due_termin)
        if 'aar' in fields_list:
            res['aar'] = year
        return res

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            vals.setdefault('regnskapssystemsreferanse', str(uuid.uuid4()))
        return super().create(vals_list)

    def action_open_activation(self):
        """Snarvei fra MVA-melding-skjemaet til selskaps-aktiveringen
        (Altinn-onboarding). UX: brukeren slipper å lete i Innstillinger."""
        self.ensure_one()
        return self.company_id.action_l10n_no_mvamelding_activate()

    # ---------- periode-helper ----------

    def _periode_spec(self):
        """Returner (xsd_element, xsd_verdi, date_from, date_to) for terminen.

        date_from/date_to er ``datetime.date`` og brukes som intervall mot
        Tax Report. XSD-element/verdi går rett inn i <periode> i XML-en.
        """
        self.ensure_one()
        spec = _PERIODE_SPEC.get(self.periode)
        if not spec:
            raise UserError(_(
                "Ukjent termin '%(p)s' på MVA-meldingen.", p=self.periode,
            ))
        element, value, (m_from, m_to) = spec
        from datetime import date
        date_from = date(self.aar, m_from, 1)
        last_day = calendar.monthrange(self.aar, m_to)[1]
        date_to = date(self.aar, m_to, last_day)
        return element, value, date_from, date_to

    # ---------- orchestrering: generer XML ----------

    def action_generate_xml(self):
        """Hent Tax Report-tall for terminen og bygg begge XML-meldingene."""
        self.ensure_one()
        lines, fastsatt = self._collect_mva_lines()
        if not lines:
            raise UserError(_(
                "Tax Report ga ingen MVA-linjer for %(p)s %(aar)s. Sjekk at "
                "det finnes bokførte bilag med MVA i terminen.",
                p=dict(self._fields['periode'].selection).get(self.periode),
                aar=self.aar,
            ))
        mvamelding_xml = self._build_mvamelding_xml(lines, fastsatt)
        konvolutt_xml = self._build_konvolutt_xml()
        self.write({
            'mvamelding_xml': mvamelding_xml,
            'konvolutt_xml': konvolutt_xml,
            'linjer_oversikt_html': self._build_linjer_oversikt_html(
                lines, fastsatt),
            'fastsatt_mva': fastsatt,
            'xml_generated_at': fields.Datetime.now(),
            'state': 'generated',
            # Nullstill ev. Altinn-spor fra et tidligere (typisk avvist)
            # forsøk. Ellers ville action_submit gjenbrukt den gamle
            # instansen + data-GUID-ene og sendt den UENDREDE (avviste) XML-en
            # videre i stedet for de korrigerte tallene. Friskt forsøk =
            # ny instans med ny XML.
            'altinn_instance_guid': False,
            'altinn_instance_owner_party_id': False,
            'altinn_konvolutt_data_guid': False,
            'altinn_mvamelding_data_guid': False,
            'altinn_process_current_task': False,
            'valideringsresultat_xml': False,
            'avvik_count': 0,
            # Nullstill også mottaks-sporet. Et tidligere forsøk kan ha vært
            # avvist via feedback-flyten, som setter kvittering_archived_at —
            # uten denne nullstillingen ville cron-en (filter
            # kvittering_archived_at=False) aldri pollet den korrigerte
            # innsendingen, og _mark_mottatt/_mark_avvist ville hoppet over
            # arkivering (guard på kvittering_archived_at).
            'kvittering_archived_at': False,
            'mottatt_at': False,
        })
        self.message_post(body=_(
            "MVA-melding-XML generert fra Tax Report. Fastsatt MVA: "
            "%(sum)s kr (%(n)d spesifikasjonslinjer).",
            sum=fastsatt, n=len(lines),
        ))
        # Fang R021 med en gang, mens brukeren står i skjemaet — ikke først
        # når 'Send inn' blokkerer.
        mangler = self._koder_uten_merknad(lines)
        if mangler:
            koder = ', '.join(sorted(set(mangler), key=int))
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'type': 'warning',
                    'title': _("Merknad kreves før innsending"),
                    'message': _(
                        "Fastsatt MVA %(sum)s kr. Mva-kode %(koder)s har "
                        "motsatt fortegn og MÅ ha merknad (regel R021), "
                        "ellers avvises meldingen. Legg den inn under "
                        "«Merknader».", sum=fastsatt, koder=koder),
                    'sticky': True,
                    'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
                },
            }
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'success',
                'title': _("XML generert"),
                'message': _(
                    "Fastsatt MVA %(sum)s kr fra %(n)d linjer. Kontroller "
                    "spesifikasjonen, og klikk 'Send inn' når den stemmer.",
                    sum=fastsatt, n=len(lines),
                ),
                'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
            },
        }

    def _koder_uten_merknad(self, lines):
        """mvaKoder i ``lines`` som krever merknad, men mangler den.

        Skatteetatens regel R021 (alvorlighetsgrad UGYLDIG_SKATTEMELDING —
        den AVVISER meldingen, den advarer ikke):

            kodene(1, 11, 12, 13, 14, 15, 81, 83, 86, 88, 91)
                .hvor { grunnlag er tomt og merverdiavgift > 0 }
                .skal { ha merknad.beskrivelse eller merknad.utvalgtMerknad }

        Motsatt fortegn oppstår når terminens tilbakeføringer av inngående
        MVA overstiger dens egne fradrag — typisk ved retting av uberettiget
        fradragsført MVA fra en tidligere termin. Da blir fradragslinjen
        netto positiv, og Skatteetaten krever en forklaring på hvorfor.

        Regelen har også unntak for `spesifikasjon` = TILBAKEFØRING/
        TAPPÅKRAV/JUSTERING, men de kodene gjelder kapitalvarer og tap på
        krav. Vi emitterer dem ikke, så merknad er eneste vei her.
        """
        self.ensure_one()
        satt = {m.mva_kode for m in self.merknad_ids if m.beskrivelse}
        return [
            line['mva_kode'] for line in lines
            if line['mva_kode'] in MVA_KODER_MERKNADSPLIKT_VED_MOTSATT_FORTEGN
            # 'is None', ikke falsy: XML-byggeren emitterer <grunnlag>
            # også når verdien er 0, og da hopper R021-sjekken på payloaden
            # over linjen. Med 'not' ville denne advart om koder gaten ikke
            # blokkerer — to kopier av samme regel som ikke var enige.
            and line.get('grunnlag') is None
            and line['merverdiavgift'] > 0
            and line['mva_kode'] not in satt
        ]

    def _build_linjer_oversikt_html(self, lines, fastsatt):
        """Bygg en lesbar HTML-tabell over mva-spesifikasjonslinjene, slik at
        brukeren kan verifisere tallene mot Tax Report før innsending.
        """
        self.ensure_one()
        kode_labels = MVA_KODE_LABELS

        def nok(v):
            if v is None:
                return ''
            return '{:,.0f}'.format(v).replace(',', ' ')

        # NB: bygges med f-strenger/konkatenering, IKKE %-operatoren — CSS-en
        # inneholder literal `%` (width:100%) som %-formatering ville krasjet på.
        td = "padding:2px 10px;"
        tdr = "padding:2px 10px;text-align:right;"
        rows = []
        for ln in lines:
            kode = ln['mva_kode']
            label = kode_labels.get(kode, '')
            grunnlag = nok(ln.get('grunnlag'))
            sats = (ln.get('sats') + ' %') if ln.get('sats') else ''
            mva = nok(ln.get('merverdiavgift'))
            rows.append(
                f"<tr><td style='{td}'>{kode}</td>"
                f"<td style='{td}'>{label}</td>"
                f"<td style='{tdr}'>{grunnlag}</td>"
                f"<td style='{tdr}'>{sats}</td>"
                f"<td style='{tdr}'>{mva}</td></tr>"
            )
        body = ''.join(rows)
        total = nok(fastsatt)
        return (
            "<table style='border-collapse:collapse;width:100%;font-size:13px;'>"
            "<thead><tr style='border-bottom:2px solid #ccc;text-align:left;'>"
            "<th style='padding:4px 10px;'>Kode</th>"
            "<th style='padding:4px 10px;'>Beskrivelse</th>"
            "<th style='padding:4px 10px;text-align:right;'>Grunnlag</th>"
            "<th style='padding:4px 10px;text-align:right;'>Sats</th>"
            "<th style='padding:4px 10px;text-align:right;'>MVA</th>"
            f"</tr></thead><tbody>{body}</tbody>"
            "<tfoot><tr style='border-top:2px solid #ccc;font-weight:bold;'>"
            "<td colspan='4' style='padding:4px 10px;'>"
            "Fastsatt MVA (å betale / til gode)</td>"
            f"<td style='padding:4px 10px;text-align:right;'>{total}</td>"
            "</tr></tfoot></table>"
        )
