"""Anleggsmiddel-spesifikasjon for skattemelding.

Skatteetatens næringsspesifikasjon har en seksjon `spesifikasjonAvAnleggsmiddel`
som inneholder per-eiendel-detaljer for SKATTEMESSIG avskrivning. Mest
vanlige type for AS er `saldoavskrevetAnleggsmiddel` — eiendeler avskrevet
etter saldoprinsippet i saldogruppe A-J per skatteloven §14-40 ff.

Saldogrupper og satser (skatteloven §14-43):
  - A: Kontormaskiner mv. (30%)
  - B: Ervervet forretningsverdi/goodwill (20%)
  - C: Vogntog, lastebiler, busser, varebiler mv. (24%)
  - C2: Varebiler som bare bruker elektrisk kraft (30%)
  - D: Personbiler, traktorer, maskiner, redskap mv. (20%)
  - E: Skip, fartøyer, rigger mv. (14%)
  - F: Fly, helikopter (12%)
  - G: Anlegg for overføring og distribusjon av elektrisk kraft (5%)
  - H: Bygg og anlegg, hoteller mv. (4%, hoteller 6%)
  - I: Forretningsbygg (2%)
  - J: Tekniske installasjoner i forretningsbygg (10%)

For goodwill (saldogruppe B) er sats fast 20%. Avskrivnings-beløpet er
20% av grunnlag (inngående saldo + nyanskaffelse - avgang).

XML-mapping til Skatteetaten (saldoavskrevetAnleggsmiddel under
spesifikasjonAvAnleggsmiddel):
  - id (Tekst, lokal identifikator)
  - objektidentifikator (TekstMedInnkapsling) — beskrivelse
  - ervervsdato (DatoMedInnkapsling)
  - inngaaendeVerdi (BeloepMedInnkapsling)
  - nyanskaffelse (BeloepMedSkattemessigeEgenskaper)
  - aaretsAvskrivning (BeloepMedSkattemessigeEgenskaper)
  - utgaaendeVerdi (BeloepMedInnkapsling, erAvledet=true men vi sender)
  - saldogruppe (SaldogruppeMedInnkapsling)
  - avskrivningssats (ProsentMedInnkapsling)
  - erDetFysiskAnleggsmiddelIUtgaaendeVerdi (BoolskMedInnkapsling)

Manuell roll-over fra år til år: brukeren oppretter ny record for
neste år med inngaaende_verdi = forrige_aars.utgaaende_verdi. Vi
genererer foreløpig ikke automatisk rull-frem (deferred til iterasjon 2+).
"""
from datetime import date as _date

from odoo import _, api, fields, models
from odoo.exceptions import UserError


_SALDOGRUPPER = [
    ('a',  'A — Kontormaskiner mv. (30%)'),
    ('b',  'B — Ervervet forretningsverdi/goodwill (20%)'),
    ('c',  'C — Vogntog, lastebiler, busser, varebiler mv. (24%)'),
    ('c2', 'C2 — Varebiler kun elektrisk kraft (30%)'),
    ('d',  'D — Personbiler, traktorer, maskiner, redskap (20%)'),
    ('e',  'E — Skip, fartøyer, rigger (14%)'),
    ('f',  'F — Fly, helikopter (12%)'),
    ('g',  'G — Anlegg for overføring av elektrisk kraft (5%)'),
    ('h',  'H — Bygg og anlegg, hoteller (4-6%)'),
    ('i',  'I — Forretningsbygg (2%)'),
    ('j',  'J — Tekniske installasjoner i forretningsbygg (10%)'),
]

# Lovbestemte satser per saldogruppe (skatteloven §14-43)
_SATS_PER_GRUPPE = {
    'a':  30.0,
    'b':  20.0,
    'c':  24.0,
    'c2': 30.0,
    'd':  20.0,
    'e':  14.0,
    'f':  12.0,
    'g':   5.0,
    'h':   4.0,  # default, hoteller 6% — kan overstyres
    'i':   2.0,
    'j':  10.0,
}


class L10nNoSkattemeldingAnleggsmiddel(models.Model):
    _name = 'l10n.no.skattemelding.anleggsmiddel'
    _description = 'Skattemelding: Saldoavskrevet anleggsmiddel'
    _order = 'skattemelding_id, saldogruppe, name'

    skattemelding_id = fields.Many2one(
        'l10n.no.skattemelding',
        string="Skattemelding",
        required=True,
        ondelete='cascade',
    )
    company_id = fields.Many2one(
        related='skattemelding_id.company_id',
        store=True,
    )
    currency_id = fields.Many2one(
        related='company_id.currency_id',
        store=True,
    )
    inntektsaar = fields.Integer(
        related='skattemelding_id.inntektsaar',
        store=True,
    )
    name = fields.Char(
        string="Beskrivelse",
        required=True,
        help="Objektidentifikator i Skatteetaten-rapportering — "
             "kort tekst som identifiserer eiendelen (eks. 'Goodwill "
             "virksomhetsoverdragelse Fhsd Connect 2025').",
    )
    saldogruppe = fields.Selection(
        _SALDOGRUPPER,
        string="Saldogruppe",
        required=True,
        help="Saldogruppe per skatteloven §14-41. For ervervet goodwill: B.",
    )
    avskrivningssats = fields.Float(
        string="Avskrivningssats (%)",
        digits=(5, 2),
        required=True,
        help="Lovbestemt sats per saldogruppe. Auto-utfylles fra "
             "saldogruppe-valget men kan overstyres for saldogruppe H "
             "(hoteller: 6%) eller spesielle tilfeller.",
    )
    ervervsdato = fields.Date(
        string="Ervervsdato",
        help="Dato eiendelen ble anskaffet/aktivert. Kreves for nye "
             "tilganger i inntektsåret.",
    )
    inngaaende_verdi = fields.Monetary(
        string="Inngående verdi",
        help="Saldo ved inntektsårets start. For nyanskaffelser i året: "
             "0 (verdien føres på 'Nyanskaffelse' istedenfor).",
    )
    nyanskaffelse = fields.Monetary(
        string="Nyanskaffelse i året",
        help="Tilganger i inntektsåret som økte saldoen.",
    )
    aarets_avskrivning = fields.Monetary(
        string="Årets avskrivning",
        compute='_compute_aarets_avskrivning',
        store=True,
        readonly=False,
        help="Beregnes som: (inngående + nyanskaffelse) × sats. "
             "Kan overstyres ved spesielle forhold.",
    )
    utgaaende_verdi = fields.Monetary(
        string="Utgående verdi",
        compute='_compute_utgaaende_verdi',
        store=True,
        readonly=False,
        help="Beregnes som: inngående + nyanskaffelse - avskrivning. "
             "Sendes med i XML (erAvledet=true men Skatteetaten "
             "krever konsistens).",
    )
    er_fysisk = fields.Boolean(
        string="Fysisk anleggsmiddel",
        default=False,
        help="True for fysiske eiendeler (biler, maskiner, bygg). "
             "False for immaterielle eiendeler (goodwill, lisenser).",
    )
    account_id = fields.Many2one(
        'account.account',
        string="Tilknyttet balansekonto",
        help="Valgfri kobling til balansekontoen som eier denne eiendelen "
             "(eks. 1080 Goodwill). Kun for sporbarhet — påvirker ikke "
             "XML-rapportering.",
        domain="[('company_ids', 'in', company_id)]",
    )

    @api.depends('inngaaende_verdi', 'nyanskaffelse', 'avskrivningssats')
    def _compute_aarets_avskrivning(self):
        for r in self:
            grunnlag = (r.inngaaende_verdi or 0.0) + (r.nyanskaffelse or 0.0)
            r.aarets_avskrivning = round(
                grunnlag * (r.avskrivningssats or 0.0) / 100.0, 2,
            )

    @api.depends('inngaaende_verdi', 'nyanskaffelse', 'aarets_avskrivning')
    def _compute_utgaaende_verdi(self):
        for r in self:
            r.utgaaende_verdi = (
                (r.inngaaende_verdi or 0.0)
                + (r.nyanskaffelse or 0.0)
                - (r.aarets_avskrivning or 0.0)
            )

    @api.onchange('saldogruppe')
    def _onchange_saldogruppe(self):
        """Auto-utfyll avskrivningssats fra lovbestemt sats — KUN hvis
        sats ikke allerede er satt manuelt.

        P1-fix fra code review 2026-05-18: tidligere overskrev denne en
        bruker-overstyrt sats (eks. hotel saldogruppe H = 6%) hvis
        brukeren bare endret saldogruppe-feltet i form-view. Nå
        respekterer vi eksisterende sats hvis den allerede er satt.
        """
        if self.saldogruppe and self.saldogruppe in _SATS_PER_GRUPPE:
            # Sett sats KUN hvis ikke allerede satt — bevarer overstyringer
            if not self.avskrivningssats:
                self.avskrivningssats = _SATS_PER_GRUPPE[self.saldogruppe]
            # er_fysisk er en metadata-hint som er trygt å auto-sette på
            # saldogruppe-endring (kan re-overstyres etterpå)
            self.er_fysisk = self.saldogruppe not in ('b',)  # B = goodwill, immateriell

    @api.constrains('avskrivningssats')
    def _check_sats_range(self):
        for r in self:
            if r.avskrivningssats < 0 or r.avskrivningssats > 100:
                raise UserError(_(
                    "Avskrivningssats må være mellom 0 og 100 (%(sats)s%% "
                    "er ugyldig).", sats=r.avskrivningssats,
                ))
