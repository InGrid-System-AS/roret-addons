"""Forskjeller mellom regnskapsmessig og skattemessig verdi.

Skatteetatens næringsspesifikasjon har seksjonen
`forskjellMellomRegnskapsmessigOgSkattemessigVerdi` med to typer:
  - permanentForskjell: poster som aldri reverseres (eks. ikke-fradragsberettiget
    representasjon, bøter, etc.)
  - midlertidigForskjell: poster der regnskapsmessig og skattemessig
    behandling avviker midlertidig (eks. ulik avskrivnings-takt for
    goodwill: 10% lineær regnskapsmessig vs 20% saldogruppe B skattemessig)

For et AS som InGrid med 5 MNOK goodwill:
  - Regnskapsmessig: 5 år lineær = 1 MNOK/år
  - Skattemessig: 20% saldogruppe B
  - Midlertidig forskjell oppstår når avskrivningene divergerer

Disse forskjellene danner grunnlag for utsatt skatt-beregning (22% av
samlet midlertidig forskjell).

XML-mapping (forenklet):
  midlertidigForskjell:
    - id
    - objektidentifikator (kobling til hvilken post forskjellen gjelder)
    - inngaaendeVerdi (akkumulert forskjell ved årets start)
    - aaretsEndring (positiv eller negativ)
    - utgaaendeVerdi (= inngående + endring)
"""
from odoo import _, api, fields, models


_FORSKJELL_TYPE = [
    ('midlertidig', 'Midlertidig forskjell'),
    ('permanent', 'Permanent forskjell'),
]


class L10nNoSkattemeldingForskjell(models.Model):
    _name = 'l10n.no.skattemelding.forskjell'
    _description = 'Skattemelding: Forskjell regnskap vs skatt'
    _order = 'skattemelding_id, type, name'

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
    type = fields.Selection(
        _FORSKJELL_TYPE,
        string="Type",
        required=True,
        default='midlertidig',
        help="Midlertidig: reverseres over tid (eks. goodwill-avskrivning). "
             "Permanent: aldri-reverserbar (eks. ikke-fradragsberettiget kost).",
    )
    name = fields.Char(
        string="Beskrivelse",
        required=True,
        help="Hva forskjellen gjelder (eks. 'Goodwill: regnskaps 10% vs "
             "skatt 20% saldo'). Mappes til objektidentifikator i XML.",
    )
    inngaaende_verdi = fields.Monetary(
        string="Inngående verdi",
        help="Akkumulert forskjell pr. inntektsårets start. For nye "
             "forskjeller (eks. første år etter oppkjøp): 0.",
    )
    aarets_endring = fields.Monetary(
        string="Årets endring",
        help="Positiv hvis forskjellen vokser i år, negativ hvis den "
             "reverseres. For goodwill år 1: (skattemessig avskr - "
             "regnskaps avskr).",
    )
    utgaaende_verdi = fields.Monetary(
        string="Utgående verdi",
        compute='_compute_utgaaende',
        store=True,
        readonly=False,
        help="Beregnes som: inngående + årets endring. Kan overstyres "
             "ved særlige forhold.",
    )
    anleggsmiddel_id = fields.Many2one(
        'l10n.no.skattemelding.anleggsmiddel',
        string="Tilknyttet anleggsmiddel",
        help="Valgfri kobling til anleggsmiddel-record (typisk for goodwill-"
             "forskjeller). For sporbarhet.",
    )

    @api.depends('inngaaende_verdi', 'aarets_endring')
    def _compute_utgaaende(self):
        for r in self:
            r.utgaaende_verdi = (
                (r.inngaaende_verdi or 0.0) + (r.aarets_endring or 0.0)
            )
