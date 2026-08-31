"""Merknad på en mva-spesifikasjonslinje.

Skatteetatens XSD tillater ÉN merknad per mvaSpesifikasjonslinje, og den er
en `xsd:choice` mellom fritekst og en kodelisteverdi:

    <xsd:complexType name="Merknad">
      <xsd:sequence>
        <xsd:choice>
          <xsd:sequence><xsd:element name="beskrivelse" type="Tekst"/></xsd:sequence>
          <xsd:sequence><xsd:element name="utvalgtMerknad" type="UtvalgtMerknad"/></xsd:sequence>
        </xsd:choice>
      </xsd:sequence>
    </xsd:complexType>

Vi støtter fritekst (`beskrivelse`) i denne omgang. Kodelisten `utvalgtMerknad`
har 39 verdier, men alle er snevert avgrenset (uttak, tap på krav, kapitalvarer,
tolldeklarasjoner …) og ingen dekker det vanligste tilfellet vårt: tilbakeføring
av inngående MVA som aldri var fradragsberettiget. Fritekst er både gyldig etter
regelen og mer opplysende for Skatteetaten. `utvalgtMerknad` kan legges til
senere uten schema-endring — feltet er en ren tilføyelse.

Unik per (melding, mvaKode) fordi XSD-en har maxOccurs=1 på merknad: to
merknader for samme kode ville ikke latt seg serialisere.
"""
from odoo import fields, models

from .l10n_no_mvamelding import MVA_KODE_LABELS

# Stigende numerisk, som resten av modulen sorterer mvaKode.
_MERKNAD_KODE_SELECTION = [
    (kode, "%s — %s" % (kode, MVA_KODE_LABELS[kode]))
    for kode in sorted(MVA_KODE_LABELS, key=lambda c: int(c))
]


class L10nNoMvameldingMerknad(models.Model):
    _name = 'l10n.no.mvamelding.merknad'
    _description = 'Merknad på mva-melding-spesifikasjonslinje'
    _order = 'mva_kode'

    mvamelding_id = fields.Many2one(
        'l10n.no.mvamelding', string="MVA-melding", required=True,
        ondelete='cascade', index=True,
    )
    company_id = fields.Many2one(
        related='mvamelding_id.company_id', store=True, index=True,
    )
    mva_kode = fields.Selection(
        _MERKNAD_KODE_SELECTION, string="MVA-kode", required=True,
        help="Spesifikasjonslinjen merknaden hører til.",
    )
    beskrivelse = fields.Text(
        string="Beskrivelse", required=True,
        help="Fritekstforklaring som sendes til Skatteetaten sammen med "
             "linjen. Dette er en forklaring i selskapets navn — skriv den "
             "som du ville forklart forholdet til en saksbehandler.",
    )

    _unique_kode = models.Constraint(
        'UNIQUE(mvamelding_id, mva_kode)',
        "Det finnes allerede en merknad for denne mva-koden. Skatteetatens "
        "skjema tillater kun én merknad per spesifikasjonslinje.",
    )

    _constraint_beskrivelse = models.Constraint(
        "CHECK (btrim(beskrivelse) <> '')",
        "Merknaden kan ikke være tom — Skatteetaten krever en faktisk "
        "forklaring.",
    )
