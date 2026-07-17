"""Skatteetatens resultatregnskap- og balansekodetype.

Hver konto i Odoo-kontoplanen mappes til en av disse kodetypene for
rapportering i næringsspesifikasjon. Kodelisten er offisielt vedlikeholdt
av Skatteetaten:

  https://github.com/Skatteetaten/skattemeldingen/tree/master/src/resources/kodeliste

For 2025: 2025_resultatregnskapOgBalanse.xml — 224 koder relevant for
oevrigSelskap (vanlig AS) fordelt på 13 underkodelister.

  Resultatregnskap-underkodelister:
    salgsinntekt (12), annenDriftsinntekt (17),
    varekostnad (10), loennskostnad (7), annenDriftskostnad (66),
    finansinntekt (12), finanskostnad (12), skattekostnad (13)

  Balanse-underkodelister:
    balanseverdiForAnleggsmiddel (39), balanseverdiForOmloepsmiddel (20),
    egenkapital (34), langsiktigGjeld (13), kortsiktigGjeld (23)

  IFRS-spesifikt (kun IFRS-foretak):
    resultatkomponentForIFRSForetak (11)

`underkodeliste`-feltet er authoritativt — det bestemmer hvilken XML-grein
en kodeverdi havner i. Importeres direkte fra Skatteetatens tekniskNavn.

Phase 2 Steg 1: utvider modellen m. nye felter additivt; eksisterende
'kategori' og 'underkategori' beholdes for bakoverkompatibilitet med
stub-data, men fades ut i Step 2 når full import erstatter dem.
"""
from odoo import api, fields, models


# Skatteetatens autoritative underkodeliste-tekniskNavn. Disse er navnene
# vi importerer fra deres XML-kodeliste; matcher 1:1 mot
# <underkodeliste><tekniskNavn>...</tekniskNavn></underkodeliste>.
_UNDERKODELISTE_SELECTION = [
    # Resultatregnskap
    ('salgsinntekt', 'Salgsinntekt'),
    ('annenDriftsinntekt', 'Annen driftsinntekt'),
    ('varekostnad', 'Varekostnad'),
    ('loennskostnad', 'Lønnskostnad'),
    ('annenDriftskostnad', 'Annen driftskostnad'),
    ('finansinntekt', 'Finansinntekt'),
    ('finanskostnad', 'Finanskostnad'),
    ('skattekostnad', 'Skattekostnad'),
    # Balanse
    ('balanseverdiForAnleggsmiddel', 'Anleggsmiddel'),
    ('balanseverdiForOmloepsmiddel', 'Omløpsmiddel'),
    ('egenkapital', 'Egenkapital'),
    ('langsiktigGjeld', 'Langsiktig gjeld'),
    ('kortsiktigGjeld', 'Kortsiktig gjeld'),
    # IFRS / spesial (sjelden brukt; vi importerer for komplettenhet)
    ('resultatkomponentForIFRSForetak', 'Resultatkomponent (IFRS)'),
]


# Deprecert, ble brukt i Phase 1-stub. Beholdes for å ikke knekke
# eksisterende data ved oppgradering — fades ut i Step 2.
_LEGACY_KATEGORI_SELECTION = [
    ('driftsinntekt', 'Driftsinntekt (deprecated)'),
    ('driftskostnad', 'Driftskostnad (deprecated)'),
    ('finansinntekt', 'Finansinntekt (deprecated)'),
    ('finanskostnad', 'Finanskostnad (deprecated)'),
    ('skattekostnad', 'Skattekostnad (deprecated)'),
    ('anleggsmiddel', 'Anleggsmiddel (deprecated)'),
    ('omloepsmiddel', 'Omløpsmiddel (deprecated)'),
    ('egenkapital', 'Egenkapital (deprecated)'),
    ('gjeld_langsiktig', 'Langsiktig gjeld (deprecated)'),
    ('gjeld_kortsiktig', 'Kortsiktig gjeld (deprecated)'),
]


# Skatteetatens fortegns-klassifisering. Bestemmer hvordan beløp signeres
# i XML — positive verdier kan f.eks. trekkes fra ved aggregering hvis
# kategori=negativ.
_FORTEGN_SELECTION = [
    ('positiv', 'Positiv (kostnader/eiendeler bokført med +)'),
    ('negativ', 'Negativ (avgang/reduksjon med +)'),
]


class SkattemeldingKodetype(models.Model):
    _name = 'l10n.no.skattemelding.kodetype'
    _description = 'Skatteetaten resultat- og balansekodetype'
    _order = 'inntektsaar desc, code'
    _rec_name = 'display_name'

    code = fields.Char(
        string="Kode",
        required=True,
        index=True,
        help="Skatteetatens kode-id. Følger NS 4102-konvensjon men er ofte "
             "grovere granularitet (1000 dekker hele 1000-1019-spennvidden).",
    )
    name = fields.Char(string="Navn", required=True)
    description = fields.Text(
        string="Begrepsreferanse",
        help="Skatteetatens definisjon av koden — hentes fra "
             "<begrepsreferanse> i kodelisten. Brukes til regnskaps-veiledning "
             "i UI når bruker velger kode.",
    )
    inntektsaar = fields.Integer(
        string="Inntektsår",
        required=True,
        index=True,
        default=2025,
        help="Kodelisten endres mellom år. Hver kode må eksistere som egen "
             "record per år den er gyldig.",
    )

    # ---- Skatteetatens autoritative klassifisering ----------------------

    underkodeliste = fields.Selection(
        _UNDERKODELISTE_SELECTION,
        string="Underkodeliste",
        index=True,
        help="Skatteetatens underkodeliste — bestemmer hvilken XML-grein "
             "kodens beløp havner i ved generering av næringsspesifikasjon. "
             "Importeres direkte fra <underkodeliste><tekniskNavn>.",
    )
    fortegn = fields.Selection(
        _FORTEGN_SELECTION,
        string="Fortegn",
        help="Skatteetatens kategorisering av om koden brukes for positive "
             "eller negative beløp. Påvirker fortegns-håndtering ved "
             "aggregering fra account.move.balance.",
    )
    korttype_naeringsspesifikasjon = fields.Char(
        string="XML korttype",
        help="Sier hvilket XML-element koden serialiseres til i "
             "næringsspesifikasjonen (eks. 'verdianleggsmiddel', "
             "'verdiomloepsmiddel', 'salgsinntekt'). Hentes fra "
             "<korttypeNaeringsspesifikasjonISme>-tagget i Skatteetatens "
             "kodeliste.",
    )

    # ---- Filtreringsmetadata --------------------------------------------

    gjelder_oevrig_selskap = fields.Boolean(
        string="Gjelder for vanlig AS",
        default=True,
        help="Skatteetaten flagger hver kode med hvilke virksomhetstyper "
             "den gjelder for. True = koden kan brukes for vanlige AS "
             "(virksomhetstype='oevrigSelskap'). Filtrér på dette i UI for "
             "å skjule koder kun relevante for spesial-selskaper.",
    )
    gjelder_full_regnskapsplikt = fields.Boolean(
        string="Gjelder ved full regnskapsplikt",
        default=True,
        help="True = koden kan brukes ved regnskapspliktstype="
             "'fullRegnskapsplikt'. Filtreres på dette for å skjule koder "
             "kun for små foretak.",
    )
    gir_verdsettingsrabatt = fields.Boolean(
        string="Gir verdsettingsrabatt",
        default=False,
        help="Skatteetatens BalansekontoGirVerdsettingsrabatt-flagg. "
             "Brukes ved formuesskatts-beregning for balanse-koder.",
    )

    # ---- Deprecated felter (Phase 1 stub-data, fades ut i Step 2) -------

    kategori = fields.Selection(
        _LEGACY_KATEGORI_SELECTION,
        string="Kategori (legacy)",
        help="DEPRECATED — bruk underkodeliste i stedet. Beholdes inntil "
             "Step 2 har erstattet stub-data med full Skatteetaten-import.",
    )
    underkategori = fields.Char(
        string="Underkategori (legacy)",
        help="DEPRECATED — bruk korttype_naeringsspesifikasjon i stedet.",
    )

    # ---- Computed -------------------------------------------------------

    display_name = fields.Char(
        compute='_compute_display_name',
        store=True,
    )

    # ---- Constraints ----------------------------------------------------

    _code_year_uniq = models.Constraint(
        'unique(code, inntektsaar)',
        'Hver kodetype må være unik per inntektsår.',
    )

    @api.depends('code', 'name', 'inntektsaar')
    def _compute_display_name(self):
        for r in self:
            r.display_name = f"{r.code} — {r.name} ({r.inntektsaar})"
