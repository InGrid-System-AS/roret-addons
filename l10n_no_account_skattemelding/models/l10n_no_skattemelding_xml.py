"""XML-builder for skattemelding + næringsspesifikasjon.

XSD-versjoner per inntektsår (verifisert mot Skatteetatens GitHub-repo
2026-05-08, regenerert 2026-03-11):

  | Inntektsår | skattemelding upersonlig | naeringsspesifikasjon |
  |------------|--------------------------|-----------------------|
  | 2024       | v4                       | v5                    |
  | 2025       | v5                       | v6                    | ← NÅVÆRENDE
  | 2026       | v6                       | v7                    |

Modulen velger versjon basert på skattemelding.inntektsaar.

Konvolutt-skjemaet er stabilt på v2 på tvers av år.

XSD-required-felt (v5 + v6):

  skattemelding (v5):
    - partsnummer (xsd:long)
    - inntektsaar (xsd:gYear)
    - alle andre er optional

  naeringsspesifikasjon (v6):
    - partsreferanse (xsd:long)
    - inntektsaar
    - virksomhet (m/ regnskapspliktstype, regnskapsperiode, virksomhetstype
      påkrevd; regeltypeForAarsregnskap optional)
    - skalBekreftesAvRevisor (boolean)

Spec: github.com/Skatteetaten/skattemeldingen/blob/master/docs/api-v2/README.md

Implementasjon: bruker lxml.etree for å bygge XML — automatisk korrekt
escaping av Norske bokstaver, &, <, > i selskapsnavn og andre felt-verdier.
F-string-konkatenering var sårbart for XML-injection ved spesielle
tegn i selskaps-config.
"""
import base64
import logging
from collections import defaultdict
from datetime import date

from lxml import etree

from odoo import _, api, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


_KILDESYSTEM = 'Eristo Skattemelding for Odoo 19'

# Mapping: underkodeliste → (XML-sti, element-navn for line item)
# Sti er relativ til <resultatregnskap>. Element-navn er hva hver
# Resultatregnskapsforekomst skal hete (varierer mellom inntekt/kostnad).
# Driftsinntekt/driftskostnad er to-nivå-wrappere; finansinntekt etc.
# legger seg direkte under resultatregnskap.
_RESULTATREGNSKAP_GRENER = {
    'salgsinntekt': (['driftsinntekt', 'salgsinntekt'], 'inntekt'),
    'annenDriftsinntekt': (['driftsinntekt', 'annenDriftsinntekt'], 'inntekt'),
    'varekostnad': (['driftskostnad', 'varekostnad'], 'kostnad'),
    'loennskostnad': (['driftskostnad', 'loennskostnad'], 'kostnad'),
    'annenDriftskostnad': (['driftskostnad', 'annenDriftskostnad'], 'kostnad'),
    'finansinntekt': (['finansinntekt'], 'inntekt'),
    'finanskostnad': (['finanskostnad'], 'kostnad'),
    'skattekostnad': (['skattekostnad'], 'kostnad'),
    'resultatkomponentForIFRSForetak': (
        ['resultatkomponentForIFRSForetak'], 'resultatkomponent',
    ),
}

# Underkodelister hvor positivt beløp = credit - debit (inntekter)
# vs hvor positivt = debit - credit (kostnader)
_INNTEKT_UNDERKODELISTER = {
    'salgsinntekt', 'annenDriftsinntekt', 'finansinntekt',
}

# Mapping: underkodeliste → (XML-sti, element-navn for line item) for
# balanseregnskap. Sti er relativ til <balanseregnskap>.
_BALANSEREGNSKAP_GRENER = {
    'balanseverdiForAnleggsmiddel': (
        ['anleggsmiddel', 'balanseverdiForAnleggsmiddel'], 'balanseverdi',
    ),
    'balanseverdiForOmloepsmiddel': (
        ['omloepsmiddel', 'balanseverdiForOmloepsmiddel'], 'balanseverdi',
    ),
    'langsiktigGjeld': (
        ['gjeldOgEgenkapital', 'langsiktigGjeld'], 'gjeld',
    ),
    'kortsiktigGjeld': (
        ['gjeldOgEgenkapital', 'kortsiktigGjeld'], 'gjeld',
    ),
    'egenkapital': (
        ['gjeldOgEgenkapital', 'egenkapital'], 'kapital',
    ),
}

# Balanse-underkodelister hvor positivt beløp = credit - debit (egenkapital
# og gjeld). For eiendeler er det motsatt (debit - credit) — default.
_BALANSE_KREDIT_UNDERKODELISTER = {
    'egenkapital', 'langsiktigGjeld', 'kortsiktigGjeld',
}

# Versjons-mapping: inntektsår → schema-versjon
# Tabellen utvides etter hvert som nye år publiseres av Skatteetaten.
_SCHEMA_VERSIONS = {
    2024: {'skattemelding': 'v4', 'naeringsspesifikasjon': 'v5'},
    2025: {'skattemelding': 'v5', 'naeringsspesifikasjon': 'v6'},
    2026: {'skattemelding': 'v6', 'naeringsspesifikasjon': 'v7'},
}

_NS_SKATTEMELDING_BASE = (
    'urn:no:skatteetaten:fastsetting:formueinntekt:skattemelding:upersonlig:ekstern'
)
_NS_NAERINGSSPESIFIKASJON_BASE = (
    'urn:no:skatteetaten:fastsetting:formueinntekt:naeringsspesifikasjon:ekstern'
)
_NS_KONVOLUTT = (
    'no:skatteetaten:fastsetting:formueinntekt:'
    'skattemeldingognaeringsspesifikasjon:request:v2'
)


def _ns_element(tag, ns):
    """Bygg en lxml.etree-element under given namespace."""
    return etree.Element(f'{{{ns}}}{tag}', nsmap={None: ns})


def _ns_subelement(parent, tag, ns, text=None):
    """Bygg sub-element under given namespace, sett tekst-innhold."""
    el = etree.SubElement(parent, f'{{{ns}}}{tag}')
    if text is not None:
        el.text = str(text)
    return el


def _serialize(root):
    """Serialiser lxml-tre til XML-streng med XML-deklarasjon."""
    return etree.tostring(
        root,
        xml_declaration=True,
        encoding='UTF-8',
        standalone=False,
        pretty_print=False,
    ).decode('utf-8')


class L10nNoSkattemeldingXmlService(models.AbstractModel):
    _name = 'l10n.no.skattemelding.xml.service'
    _description = 'XML-generator for skattemelding-innsending'

    @api.model
    def _schema_version(self, inntektsaar, doctype):
        """Returner versjons-string ('v5', 'v6' osv.) for år + dokumenttype."""
        if inntektsaar not in _SCHEMA_VERSIONS:
            raise UserError(_(
                "Inntektsår %(year)d har ingen kjent XSD-versjons-mapping. "
                "Kontakt Eristo for oppdatering når Skatteetaten publiserer "
                "nye versjoner.",
                year=inntektsaar,
            ))
        return _SCHEMA_VERSIONS[inntektsaar][doctype]

    @api.model
    def _resolve_partsnummer(self, skattemelding):
        """Hent partsnummer eller raise med klar instruksjon.

        partsnummer er Skatteetatens interne id (xsd:long, typisk 10 sifre)
        — ikke orgnr. Den må enten settes manuelt på skattemelding-record
        eller hentes via hentGjeldende-action FØR XML kan bygges.
        """
        if not skattemelding.partsnummer:
            raise UserError(_(
                "Partsnummer mangler. Klikk 'Hent partsnummer fra Skatteetaten' "
                "for automatisk oppslag, eller fyll inn manuelt hvis du har "
                "verdien fra Skatteetaten direkte.\n\n"
                "(Partsnummer er Skatteetatens interne ID for selskapet — "
                "typisk et 10-sifret tall som skiller seg fra organisasjonsnummeret.)"
            ))
        try:
            int(skattemelding.partsnummer)
        except (TypeError, ValueError):
            raise UserError(_(
                "Partsnummer '%(val)s' er ikke et gyldig heltall. "
                "Skatteetaten forventer xsd:long.",
                val=skattemelding.partsnummer,
            ))
        return skattemelding.partsnummer

    @api.model
    def build_skattemelding_xml(self, skattemelding):
        """Bygg skattemeldingUpersonlig (innerste) XML-streng.

        Inkluderer:
          - partsnummer + inntektsaar (XSD-påkrevd)
          - inntektOgUnderskudd m. beregnet næringsinntekt eller underskudd
            (Phase 2.5: matcher resultatregnskap fra næringsspesifikasjon
            for å unngå konsistens-veiledning fra Skatteetaten).
        """
        skattemelding.ensure_one()
        company = skattemelding.company_id
        version = self._schema_version(skattemelding.inntektsaar, 'skattemelding')
        ns = f'{_NS_SKATTEMELDING_BASE}:{version}'
        partsnummer = self._resolve_partsnummer(skattemelding)

        root = _ns_element('skattemelding', ns)
        _ns_subelement(root, 'partsnummer', ns, text=partsnummer)
        _ns_subelement(root, 'inntektsaar', ns, text=skattemelding.inntektsaar)

        # inntektOgUnderskudd — minimum-struktur som validerte OK i første
        # E2E-test (2026-05-15, 3 ikke-blokkerende veiledninger). Eksperimenter
        # med å legge til <samletUnderskudd> og <opplysningerOmSkattesubjekt>
        # forårsaket xmlValideringsfeilPaaSkattemelding i v5-XSD som vi
        # ikke har tilgang til for å verifisere struktur-forskjeller fra v2.
        #
        # 2024-eksemplet (v4-XSD) viser bare overskudd-case med
        # <inntekt><naeringsinntekt> + <inntektFoerFradragForEventuelt...>
        # + <samletInntekt>. Ingen v4/v5-eksempel for UNDERSKUDD finnes
        # i Skatteetatens GitHub.
        #
        # Pragmatisk valg: send minimum som vi har bevis for at v5 godtar.
        # Eksperimenter med tilleggsfelter når v5-XSD blir tilgjengelig.
        naeringsinntekt, underskudd = self._calculate_naeringsresultat(skattemelding)
        if naeringsinntekt > 0 or underskudd > 0:
            inntekt_og_underskudd = _ns_subelement(root, 'inntektOgUnderskudd', ns)
            if naeringsinntekt > 0:
                inntekt_wrap = _ns_subelement(inntekt_og_underskudd, 'inntekt', ns)
                nær_wrap = _ns_subelement(inntekt_wrap, 'naeringsinntekt', ns)
                _ns_subelement(
                    nær_wrap, 'beloepSomHeltall', ns,
                    text=str(int(round(naeringsinntekt))),
                )
            else:
                fradrag_wrap = _ns_subelement(inntekt_og_underskudd, 'inntektsfradrag', ns)
                und_wrap = _ns_subelement(fradrag_wrap, 'underskudd', ns)
                _ns_subelement(
                    und_wrap, 'beloepSomHeltall', ns,
                    text=str(int(round(underskudd))),
                )

        # opplysningerOmSkattesubjekt — REVERTET ANDRE GANG 2026-05-18.
        # v5-XSD (skattemelding 2025) avviser dette med
        # 'xmlValideringsfeilPaaSkattemelding' — samme blokker som
        # 2026-05-15-revertering. Iterasjon 1-researchen så på v7
        # (næringsspes 2026), men 2025 bruker v5-skattemelding-XSD som
        # IKKE har samme struktur. Trenger v5-XSD eller v5-sample XML
        # for å vite hvor disse feltene egentlig hører.
        #
        # Feltene (l10n_no_skattemelding_boersnotert,
        # l10n_no_skattemelding_har_ytelser_aksjonaer på res.company) er
        # beholdt — vi bruker dem når v5-strukturen er kjent.
        #
        # Konsekvens: 3 merknadStandard fra Skatteetaten om manglende
        # selskaps-opplysninger (ikke-blokkerende). Innsending lykkes
        # likevel.

        return _serialize(root)

    @api.model
    def _build_opplysninger_om_skattesubjekt(self, root, ns, skattemelding):
        """Legg til <opplysningerOmSkattesubjekt> under skattemelding-XML.

        Inneholder selskaps-flagg som Skatteetaten trenger:
          - erBoersnotert: er selskapet børsnotert?
          - harYtelseMellomAksjonaerEllerNaerstaaendeOg...: ytelser m/
            aksjonær eller nærstående parter?

        Begge er Boolean-felter på res.company.

        Hvis BEGGE er False (vanlig for små AS) bygger vi seksjonen
        likevel — det fjerner merknadStandard om "manglende opplysninger".
        """
        company = skattemelding.company_id
        boersnotert = bool(getattr(company, 'l10n_no_skattemelding_boersnotert', False))
        har_ytelse = bool(getattr(company, 'l10n_no_skattemelding_har_ytelser_aksjonaer', False))
        wrap = _ns_subelement(root, 'opplysningerOmSkattesubjekt', ns)
        # erBoersnotert (BoolskMedInnkapsling: ytre er feltnavn, indre er <boolsk>)
        b1 = _ns_subelement(wrap, 'erBoersnotert', ns)
        _ns_subelement(b1, 'boolsk', ns, text='true' if boersnotert else 'false')
        # harYtelseMellomAksjonaerEllerNaerstaaendeOgSelskapEllerSelskapetsDatterselskap
        b2 = _ns_subelement(
            wrap,
            'harYtelseMellomAksjonaerEllerNaerstaaendeOgSelskapEllerSelskapetsDatterselskap',
            ns,
        )
        _ns_subelement(b2, 'boolsk', ns, text='true' if har_ytelse else 'false')

    @api.model
    def _calculate_naeringsresultat(self, skattemelding):
        """Beregn årsresultat fra resultatregnskap-aggregering.

        Returnerer (naeringsinntekt, underskudd) der nøyaktig én er > 0:
          - Overskudd (inntekter > kostnader) → (positivt tall, 0)
          - Underskudd → (0, positivt tall)
          - Null → (0, 0)

        Bruker samme aggregering som _build_resultatregnskap, så summene
        i skattemelding matcher resultatregnskap-summene i nær.spes.

        Vi inkluderer ALLE resultat-underkodelister (drifts- og finans-
        inntekt/-kostnad + skattekostnad). Skatteetaten differensierer
        skattemessig næringsinntekt fra regnskapsmessig årsresultat med
        diverse tillegg/fradrag — Phase 3 vil utvide med ekte beregning;
        for nå brukes regnskapsmessig årsresultat.
        """
        by_kodetype = self._aggregate_amounts_by_kodetype(
            skattemelding, cumulative=False,
        )
        income_total = 0.0
        expense_total = 0.0
        for kt, sums in by_kodetype.items():
            ukl = kt.underkodeliste
            if ukl not in _RESULTATREGNSKAP_GRENER:
                continue
            net = self._net_amount_for_kodetype(kt, sums['debit'], sums['credit'])
            if ukl in _INNTEKT_UNDERKODELISTER:
                income_total += net
            else:
                expense_total += net
        resultat = income_total - expense_total
        if resultat > 0.5:
            return (resultat, 0.0)
        if resultat < -0.5:
            return (0.0, -resultat)
        return (0.0, 0.0)

    @api.model
    def build_naeringsspesifikasjon_xml(self, skattemelding):
        """Bygg næringsspesifikasjon XML-streng (resultat + balanse).

        Phase 2: XSD-påkrevde felt + resultatregnskap + balanseregnskap
        aggregert fra account.move-data.

        Sjekker coverage før bygging: hvis kontoer har posted move-lines
        for året men mangler kodetype-mapping, varsles brukeren med klar
        instruks om hva som mangler. Phase 1-stub (uten resultat/balanse)
        nåes ikke lenger via denne metoden.
        """
        skattemelding.ensure_one()

        # Coverage-sjekk: to-trinns
        # 1. Mangler mapping: konto har transaksjoner men ingen kodetype
        # 2. Mangler verifisering: kodetype er foreslått men ikke godkjent
        # Begge blokkerer XML-bygging.
        coverage = self._check_mapping_coverage(skattemelding)
        company = skattemelding.company_id

        if coverage['unmapped']:
            unmapped = coverage['unmapped']
            sample = unmapped[:10]
            sample_lines = ', '.join(
                f"{a.code} ({a.name or 'Uten navn'})" for a in sample
            )
            more = (
                f" + {len(unmapped) - len(sample)} til"
                if len(unmapped) > len(sample) else ''
            )
            raise UserError(_(
                "%(n)d kontoer har bokførte transaksjoner men MANGLER "
                "Skatteetaten-kodetype.\n\n"
                "Klikk knappen 'Sett opp kontomapping' i toppmenyen — "
                "den auto-foreslår kodetyper for alle konti og åpner "
                "en redigerbar liste der du kan verifisere + bulk-godkjenne "
                "i én skjerm.\n\n"
                "Eksempler: %(sample)s%(more)s",
                n=len(unmapped), sample=sample_lines, more=more,
            ))

        if coverage['unverified']:
            unverified = coverage['unverified']
            sample = unverified[:10]
            sample_lines = '\n'.join(
                f"  • {a.code} {a.name or ''} → "
                f"{a.l10n_no_skattemelding_kodetype_id.code} "
                f"({a.l10n_no_skattemelding_kodetype_id.name})"
                for a in sample
            )
            more = (
                f"\n  ... + {len(unverified) - len(sample)} til"
                if len(unverified) > len(sample) else ''
            )
            raise UserError(_(
                "%(n)d kontoer har AUTO-FORESLÅTT kodetype men er IKKE "
                "verifisert.\n\n"
                "KRITISK: Auto-suggest er en heuristikk og kan gjøre "
                "semantisk feil mapping (eks: konto 3020 'Sales services' "
                "kan ende opp under petroleum-kode 3008). Du må manuelt "
                "gjennomgå hver mapping mot Skatteetatens beskrivelse "
                "før XML kan bygges.\n\n"
                "Klikk knappen 'Sett opp kontomapping' i toppmenyen — "
                "den åpner en redigerbar liste der du kan se forslag + "
                "Skatteetaten-beskrivelse for hver konto. Multi-edit "
                "støttes så du kan markere flere rader og toggle 'Verifisert' "
                "i ett kjøp.\n\n"
                "Følgende kontoer mangler verifisering:\n%(sample)s%(more)s",
                n=len(unverified), sample=sample_lines, more=more,
            ))

        if coverage['incompatible_regnskapsplikt']:
            incompatible = coverage['incompatible_regnskapsplikt']
            sample = incompatible[:10]
            sample_lines = '\n'.join(
                f"  • {a.code} {a.name or ''} → "
                f"{a.l10n_no_skattemelding_kodetype_id.code} "
                f"({a.l10n_no_skattemelding_kodetype_id.name})"
                for a in sample
            )
            more = (
                f"\n  ... + {len(incompatible) - len(sample)} til"
                if len(incompatible) > len(sample) else ''
            )
            rpt = (
                company.l10n_no_skattemelding_regnskapspliktstype
                or 'fullRegnskapsplikt'
            )
            raise UserError(_(
                "%(n)d kontoer er mappet til Skatteetaten-kodetyper som "
                "IKKE er forenlige med selskapets regnskapspliktstype "
                "'%(rpt)s'.\n\n"
                "Skatteetaten avviser slike skattemeldinger med "
                "N_FEIL_ANLEGGSMIDDELTYPE (eller tilsvarende avvik for "
                "andre underkodelister). Eksempel: kode 1295 'Driftsmidler "
                "som avskrives lineært' gjelder kun for begrenset "
                "regnskapsplikt — full regnskapsplikt skal bruke "
                "saldogruppe-spesifikke koder (1280, 1281, 1282 osv.).\n\n"
                "Fix: åpne hver konto under og bytt til en kompatibel "
                "kodetype.\n\n"
                "Inkompatible kontoer:\n%(sample)s%(more)s",
                n=len(incompatible), rpt=rpt,
                sample=sample_lines, more=more,
            ))

        company = skattemelding.company_id
        year = skattemelding.inntektsaar
        version = self._schema_version(year, 'naeringsspesifikasjon')
        ns = f'{_NS_NAERINGSSPESIFIKASJON_BASE}:{version}'
        partsreferanse = self._resolve_partsnummer(skattemelding)

        # XSD-sequence under <naeringsspesifikasjon>:
        #   1. partsreferanse (required)
        #   2. inntektsaar (required)
        #   3. resultatregnskap (optional) — MÅ komme før virksomhet
        #   4. balanseregnskap (optional) — MÅ komme før virksomhet
        #   ... (flere optional)
        #   5. virksomhet (required)
        #   ... (flere optional)
        #   6. skalBekreftesAvRevisor (required)
        #
        # Bug funnet 2026-05-11 i E2E: vi hadde resultatregnskap+balanseregnskap
        # som BARN av virksomhet → Skatteetaten avviste m. avvik
        # 'xmlValideringsfeilPaaNaeringsopplysningene'.
        root = _ns_element('naeringsspesifikasjon', ns)
        _ns_subelement(root, 'partsreferanse', ns, text=partsreferanse)
        _ns_subelement(root, 'inntektsaar', ns, text=year)

        # Resultatregnskap (Phase 2) — søsken av virksomhet, FØR i sequence
        self._build_resultatregnskap(root, ns, skattemelding)

        # Balanseregnskap (Phase 2) — søsken av virksomhet, FØR i sequence
        self._build_balanseregnskap(root, ns, skattemelding)

        # Sikring #4: verifiser at XML-totalsummer matcher Odoos
        # rapportering. Hindrer at algoritmisk feil (eks: utelater en
        # konto, dobbeltteller, feil sign) går uoppdaget til Skatteetaten.
        self._validate_aggregated_totals(skattemelding)

        # Virksomhet — påkrevd, plassert etter resultat/balanse per XSD
        virksomhet = _ns_subelement(root, 'virksomhet', ns)

        # regnskapspliktstype (innkapslet i en wrapper med samme navn)
        rpt_wrap = _ns_subelement(virksomhet, 'regnskapspliktstype', ns)
        _ns_subelement(
            rpt_wrap, 'regnskapspliktstype', ns,
            text=company.l10n_no_skattemelding_regnskapspliktstype or 'fullRegnskapsplikt',
        )

        # regnskapsperiode
        periode = _ns_subelement(virksomhet, 'regnskapsperiode', ns)
        start_wrap = _ns_subelement(periode, 'start', ns)
        _ns_subelement(start_wrap, 'dato', ns, text=date(year, 1, 1).isoformat())
        slutt_wrap = _ns_subelement(periode, 'slutt', ns)
        _ns_subelement(slutt_wrap, 'dato', ns, text=date(year, 12, 31).isoformat())

        # virksomhetstype
        vt_wrap = _ns_subelement(virksomhet, 'virksomhetstype', ns)
        _ns_subelement(
            vt_wrap, 'virksomhetstype', ns,
            text=company.l10n_no_skattemelding_virksomhetstype or 'oevrigSelskap',
        )

        # regeltypeForAarsregnskap (optional men vi setter alltid)
        rt_wrap = _ns_subelement(virksomhet, 'regeltypeForAarsregnskap', ns)
        _ns_subelement(
            rt_wrap, 'regeltypeForAarsregnskap', ns,
            text=(company.l10n_no_skattemelding_regeltype_aarsregnskap
                  or 'regnskapslovensAlminneligeRegler'),
        )

        # Iterasjon 1 (2026-05-18) RESEARCHED FOR v7 BUT 2025 USES v6:
        # vi forsøkte å legge til 3 nye seksjoner basert på v7-research:
        #   - spesifikasjonAvAnleggsmiddel
        #   - forskjellMellomRegnskapsmessigOgSkattemessigVerdi
        #   - egenkapitalavstemming
        # Skatteetaten avviste m. xmlValideringsfeilPaaNaeringsopplysningene
        # 2026-05-18 mot v6-XSD (2025). v6 vs v7 har sannsynligvis ulik
        # element-navn/struktur. Reverterer for at InGrid 2025 skal
        # komme gjennom — får 3 merknadStandard (samme som Roret fikk)
        # men innsending lykkes.
        #
        # Modellene (l10n.no.skattemelding.anleggsmiddel + .forskjell) er
        # beholdt for fremtidig korrekt implementasjon i v6 og v7. Også
        # roll-forward-funksjonen er beholdt.
        #
        # Helper-metodene _build_spesifikasjon_av_anleggsmiddel,
        # _build_forskjell_regnskap_skatt, _build_egenkapitalavstemming
        # og _sum_egenkapital_balance er beholdt for raskt re-aktivering
        # når v6-XSD er verifisert.
        #
        # self._build_spesifikasjon_av_anleggsmiddel(root, ns, skattemelding)
        # self._build_forskjell_regnskap_skatt(root, ns, skattemelding)
        # self._build_egenkapitalavstemming(root, ns, skattemelding)

        # skalBekreftesAvRevisor — påkrevd, siste i sequence
        _ns_subelement(root, 'skalBekreftesAvRevisor', ns, text='false')

        return _serialize(root)


    # NB: Iterasjon 1 (2026-05-18) hadde helper-metoder her for
    # _build_spesifikasjon_av_anleggsmiddel, _build_forskjell_regnskap_skatt,
    # _build_egenkapitalavstemming og _sum_egenkapital_balance. De ble
    # slettet etter at v7-research-basert XML ble avvist av v6-XSD
    # (Skatteetaten 2025). Modellene .anleggsmiddel + .forskjell finnes
    # fortsatt og kan brukes når v6/v7-XSD er korrekt verifisert. Se
    # git-historikk commit bfb0f91 for original implementasjon.


    @api.model
    def _validate_aggregated_totals(self, skattemelding):
        """Sikring #4: sjekk at aggregerte XML-summer matcher Odoo direkte.

        For inntekts- og kostnadssidene:
        - Beregn sum av alle account.move.line.balance for kontoer med
          gitt account_type (income/expense) i inntektsåret
        - Sammenlign med sum av aggregerte beløp i XML-en
        - Hvis differanse > 0.1% av total → STOPP og varsle

        Dette fanger:
        - Kontoer som tilfeldigvis mangler mapping (men har move-lines)
        - Algoritmiske feil i aggregering (dobbeltelling, sign-feil)
        - Edge cases der enkelte kontoer faller mellom underkodelister

        Reiser UserError m. konkret avvik hvis funnet.
        """
        skattemelding.ensure_one()
        company = skattemelding.company_id
        year = skattemelding.inntektsaar

        # XML-summer: sum av aggregert resultatregnskap per fortegn
        by_kodetype = self._aggregate_amounts_by_kodetype(
            skattemelding, cumulative=False,
        )
        xml_income = 0.0
        xml_expense = 0.0
        for kt, sums in by_kodetype.items():
            ukl = kt.underkodeliste
            if ukl not in _RESULTATREGNSKAP_GRENER:
                continue  # balanse — ignorer i resultat-validering
            net = self._net_amount_for_kodetype(
                kt, sums['debit'], sums['credit'],
            )
            if ukl in _INNTEKT_UNDERKODELISTER:
                xml_income += net
            else:
                xml_expense += net

        # Odoo-direkte: sum balance per account_type for inntektsåret.
        # NB: må ekskludere closing-bilag for å matche aggregeringen i
        # _aggregate_amounts_by_kodetype (som filtrerer dem bort fra
        # resultatregnskap). Bugfix 2026-05-17: tidligere SQL inkluderte
        # closing → "Odoo direkte sum" ble 0 etter at avslutningsbilag
        # var postet (closing reverserte år-aktiviteten), mens XML-en
        # korrekt viste år-aktiviteten → falsk TOTALSUM-AVVIK.
        self.env.cr.execute("""
            SELECT
                CASE WHEN aa.account_type IN ('income', 'income_other')
                     THEN 'income' ELSE 'expense' END AS bucket,
                SUM(aml.credit - aml.debit) AS net_income_sign,
                SUM(aml.debit - aml.credit) AS net_expense_sign
            FROM account_move_line aml
            JOIN account_move am ON aml.move_id = am.id
            JOIN account_account aa ON aml.account_id = aa.id
            WHERE am.company_id = %s
              AND am.state = 'posted'
              AND COALESCE(am.l10n_no_skattemelding_closing, FALSE) = FALSE
              AND aml.date BETWEEN MAKE_DATE(%s, 1, 1) AND MAKE_DATE(%s, 12, 31)
              AND aa.account_type IN (
                'income', 'income_other',
                'expense', 'expense_depreciation',
                'expense_direct_cost', 'expense_other'
              )
            GROUP BY bucket
        """, [company.id, year, year])
        odoo_income = 0.0
        odoo_expense = 0.0
        for row in self.env.cr.fetchall():
            bucket, income_sign, expense_sign = row
            if bucket == 'income':
                odoo_income = income_sign or 0.0
            else:
                odoo_expense = expense_sign or 0.0

        # Sammenlign — tolerér 0.1% avvik (avrundinger)
        def assert_match(label, xml_amount, odoo_amount):
            if odoo_amount == 0 and xml_amount == 0:
                return  # OK, ingenting å sammenligne
            diff = abs(xml_amount - odoo_amount)
            total_for_pct = max(abs(odoo_amount), abs(xml_amount), 1.0)
            pct = diff / total_for_pct
            if pct > 0.001:
                raise UserError(_(
                    "TOTALSUM-AVVIK for %(label)s i inntektsår %(year)d:\n\n"
                    "  XML aggregert sum: %(xml)s\n"
                    "  Odoo direkte sum:  %(odoo)s\n"
                    "  Differanse:        %(diff)s (%(pct).2f%%)\n\n"
                    "Dette indikerer en algoritmisk feil — eks: en konto "
                    "har feil mapping (havner i annenDriftskostnad istedenfor "
                    "salgsinntekt), eller signs er motsatt forventet. "
                    "Inspiser mappingen før innsending — Skatteetaten må "
                    "IKKE få feilrapportert tall.",
                    label=label, year=year,
                    xml=f"{xml_amount:>14,.2f} kr",
                    odoo=f"{odoo_amount:>14,.2f} kr",
                    diff=f"{diff:>14,.2f} kr",
                    pct=pct * 100,
                ))

        assert_match("inntekter", xml_income, odoo_income)
        assert_match("kostnader", xml_expense, odoo_expense)

    @api.model
    def _check_mapping_coverage(self, skattemelding):
        """Identifiser kontoer som blokkerer XML-bygging.

        Returnerer dict med tre lister:
          {
            'unmapped': recordset av kontoer som mangler kodetype-mapping,
            'unverified': recordset av kontoer som har mapping men ikke
                          verifisert (auto-suggest har satt forslag, men
                          ingen har bekreftet det semantisk),
            'incompatible_regnskapsplikt': kontoer mappet til kodetype
                          som er flagget inkompatibel med selskapets
                          regnskapspliktstype (eks: kode 1295 brukt mens
                          selskapet har fullRegnskapsplikt — Skatteetaten
                          avviser med N_FEIL_ANLEGGSMIDDELTYPE).
          }

        Blokkerer XML-bygging hvis NOEN av listene har innhold. Dette er
        en behavioral guardrail mot at maskinell heuristikk produserer
        feil skattemelding uten human review (bug-eksempel: konto 3020
        'Sales services' kunne tidligere ende opp under petroleum-koden
        3008 fordi algoritmen mekanisk valgte 'nærmeste ≤ i serien').

        Kontoer uten code ekskluderes — de er system-kontoer som ikke
        skal rapporteres uansett.

        Bugfix 2026-05-16: bruk samme `_get_accounts_requiring_mapping`-
        helper som setup-mapping-actionen, slik at coverage-sjekken og
        mapping-listen bruker IDENTISK kriterium for hvilke kontoer som
        er "in scope". Tidligere SQL-spørringen her hentet ALLE konti m.
        bevegelse — inklusive 8800 Årsresultat som kun har closing-bilag-
        aktivitet og nullstilles, men ble feilaktig flagget som påkrevd-
        mapping fordi denne sjekken ikke filtrerte ut closing.
        """
        skattemelding.ensure_one()
        company = skattemelding.company_id

        active = skattemelding._get_accounts_requiring_mapping()
        if not active:
            empty = self.env['account.account']
            return {
                'unmapped': empty,
                'unverified': empty,
                'incompatible_regnskapsplikt': empty,
            }

        unmapped = active.filtered(
            lambda a: not a.l10n_no_skattemelding_kodetype_id
        )
        unverified = active.filtered(
            lambda a: a.l10n_no_skattemelding_kodetype_id
                      and not a.l10n_no_skattemelding_kodetype_verified
        )
        incompatible = active.filtered(
            lambda a: self._is_kodetype_incompatible_with_company(
                a.l10n_no_skattemelding_kodetype_id, company,
            )
        )
        return {
            'unmapped': unmapped,
            'unverified': unverified,
            'incompatible_regnskapsplikt': incompatible,
        }

    @api.model
    def _is_kodetype_incompatible_with_company(self, kodetype, company):
        """Sjekk om en kodetype er flagget inkompatibel med selskapet.

        Skatteetatens kodeliste flagger hver kode med hvilke
        `regnskapspliktstype`-verdier den er gyldig for. Vi importerer
        flagget som `gjelder_full_regnskapsplikt` på kodetype-record.

        Returnerer True når kombinasjonen vil utløse avvik fra Skatteetaten
        (eks. N_FEIL_ANLEGGSMIDDELTYPE) — caller skal blokkere XML-bygging
        og be brukeren bytte kodetype.
        """
        if not kodetype:
            return False
        rpt = (
            company.l10n_no_skattemelding_regnskapspliktstype
            or 'fullRegnskapsplikt'
        )
        if rpt == 'fullRegnskapsplikt' and not kodetype.gjelder_full_regnskapsplikt:
            return True
        return False

    @api.model
    def _aggregate_amounts_by_kodetype(self, skattemelding, cumulative=False):
        """Aggreger account.move.line-summer per Skatteetaten-kodetype.

        Returnerer dict {kodetype_record: {'debit': float, 'credit': float}}
        for kontoer som har l10n_no_skattemelding_kodetype_id satt og som
        har posted move-lines.

        Args:
          cumulative: hvis False (default), summer kun innen inntektsåret
            (jan 1 - dec 31). Brukes for resultatregnskap.
            Hvis True, summer fra all-tid frem til 31.12 inntektsår.
            Brukes for balanseregnskap (closing balance).

        Bruker `read_group` for ytelse — vi vil ikke fetche enkelt-lines
        for store regnskaper.
        """
        skattemelding.ensure_one()
        company = skattemelding.company_id
        year = skattemelding.inntektsaar
        date_to = date(year, 12, 31)

        domain = [
            ('company_id', '=', company.id),
            ('move_id.state', '=', 'posted'),
            ('date', '<=', date_to),
            ('account_id.l10n_no_skattemelding_kodetype_id', '!=', False),
        ]
        if not cumulative:
            domain.append(('date', '>=', date(year, 1, 1)))
            # Resultatregnskap: ekskluder årsavslutningsbilag slik at
            # P&L-saldoer vises som ÅRETS BEVEGELSER, ikke 'netto 0
            # etter lukke-bilag'. Balanseregnskap (cumulative=True) skal
            # IKKE filtrere ut closing — vi vil ha 2080/2050-saldo fra
            # avslutningen synlig i balansen.
            domain.append(('move_id.l10n_no_skattemelding_closing', '=', False))

        # _read_group (read_group er deprecated fra 19.0): returnerer
        # tupler (account, debit_sum, credit_sum).
        groups = self.env['account.move.line'].sudo()._read_group(
            domain,
            groupby=['account_id'],
            aggregates=['debit:sum', 'credit:sum'],
        )

        by_kodetype = defaultdict(lambda: {'debit': 0.0, 'credit': 0.0})
        for account, debit, credit in groups:
            kt = account.l10n_no_skattemelding_kodetype_id
            # Filtrér: kodetype må gjelde dette inntektsåret. Hvis konto
            # er mappet til 2024-kodetype og vi rapporterer 2025, skip
            # (kunden må re-mappe for året først).
            if not kt or kt.inntektsaar != year:
                continue
            by_kodetype[kt]['debit'] += debit
            by_kodetype[kt]['credit'] += credit
        return by_kodetype

    @api.model
    def _net_amount_for_kodetype(self, kodetype, debit, credit):
        """Beregn netto-beløp i Skatteetaten-konvensjon (positivt tall).

        Skatteetaten forventer positive verdier — sign-konvensjonen er
        implisitt i hvilken XML-grein verdien havner i.

        Konti hvor positivt = credit - debit:
          - Inntekter: salgsinntekt, annenDriftsinntekt, finansinntekt
          - Egenkapital, langsiktig/kortsiktig gjeld

        Alle andre (kostnader, eiendeler): positivt = debit - credit.

        For kodetyper med fortegn='negativ' (eks. 2080 'Negativ egenkapital'
        / udekket tap) returnerer vi absoluttverdi — fortegnet er
        implisitt i koden selv, og Skatteetaten avviser med
        N_NEGATIV_KONTO_<kode> dersom verdien sendes med minus-tegn.
        """
        ukl = kodetype.underkodeliste
        if ukl in _INNTEKT_UNDERKODELISTER or ukl in _BALANSE_KREDIT_UNDERKODELISTER:
            net = credit - debit
        else:
            net = debit - credit
        if kodetype.fortegn == 'negativ':
            return abs(net)
        return net

    @api.model
    def _build_resultatregnskap(self, parent_el, ns, skattemelding):
        """Bygg <resultatregnskap>-strukturen under parent-elementet.

        Per XSD er <resultatregnskap> et BARN av <naeringsspesifikasjon>,
        plassert FØR <virksomhet> i sequence. Parent skal være
        naeringsspesifikasjon-root, ikke virksomhet.

        Gruperer aggregerte beløp per underkodeliste, oppretter wrapper-
        elementer (driftsinntekt/driftskostnad/etc.) bare når det finnes
        beløp under dem.

        XSD-struktur:
          <resultatregnskap>
            <driftsinntekt>
              <salgsinntekt>
                <inntekt>
                  <beloep><beloep><beloep>123.45</beloep></beloep></beloep>
                  <id>kt-3000-2025</id>
                  <type>
                    <resultatOgBalanseregnskapstype>3000</resultatOgBalanseregnskapstype>
                  </type>
                </inntekt>
              </salgsinntekt>
              <annenDriftsinntekt>...</annenDriftsinntekt>
            </driftsinntekt>
            <driftskostnad>...</driftskostnad>
            <finansinntekt>...</finansinntekt>
            ...
          </resultatregnskap>
        """
        by_kodetype = self._aggregate_amounts_by_kodetype(
            skattemelding, cumulative=False,
        )
        if not by_kodetype:
            return  # ingen mapped data — skip element helt (minOccurs=0)

        # Grupper per underkodeliste
        by_grein = defaultdict(list)
        for kt, sums in by_kodetype.items():
            ukl = kt.underkodeliste
            if ukl not in _RESULTATREGNSKAP_GRENER:
                # Balanse-koder havner her — ignoreres i resultatregnskap;
                # de håndteres av _build_balanseregnskap (Steg 5).
                continue
            net = self._net_amount_for_kodetype(kt, sums['debit'], sums['credit'])
            # Skip nuller — Skatteetaten trenger ikke se tomme beløp
            if abs(net) < 0.005:
                continue
            by_grein[ukl].append((kt, net))

        if not by_grein:
            return

        # Bygg <resultatregnskap>
        resultatregnskap = _ns_subelement(parent_el, 'resultatregnskap', ns)

        # Bygg wrapper-trær. Driftsinntekt/driftskostnad har to-nivå-wrapper.
        # Vi gjenbruker samme wrapper hvis flere underkodelister deler det.
        wrapper_cache = {}  # path-tuple → element

        def ensure_wrapper(path_segments):
            """Opprett eller gjenbruk nestede wrapper-elementer."""
            key = tuple(path_segments)
            if key in wrapper_cache:
                return wrapper_cache[key]
            # Bygg parent først (rekursivt), så denne under
            if len(path_segments) == 1:
                parent = resultatregnskap
            else:
                parent = ensure_wrapper(path_segments[:-1])
            el = _ns_subelement(parent, path_segments[-1], ns)
            wrapper_cache[key] = el
            return el

        # Emit forekomster i underkodeliste-rekkefølge (deterministisk)
        for ukl_name in _RESULTATREGNSKAP_GRENER:
            if ukl_name not in by_grein:
                continue
            path, item_tag = _RESULTATREGNSKAP_GRENER[ukl_name]
            wrapper = ensure_wrapper(path)
            # Sortér per kode for stabilt XML-output
            for kt, net in sorted(by_grein[ukl_name], key=lambda x: x[0].code):
                self._emit_regnskaps_forekomst(wrapper, ns, item_tag, kt, net)

    @api.model
    def _emit_regnskaps_forekomst(self, parent, ns, tag, kodetype, amount):
        """Emit en Resultatregnskapsforekomst-element under parent.

        Struktur (Resultatregnskapsforekomst — beloep, id, type i orden):
          <inntekt|kostnad|resultatkomponent>
            <beloep>
              <beloep>
                <beloep>123.45</beloep>
              </beloep>
            </beloep>
            <id>{kodetype.code}-{år}</id>
            <type>
              <resultatOgBalanseregnskapstype>{kode}</resultatOgBalanseregnskapstype>
            </type>
          </inntekt|kostnad|resultatkomponent>
        """
        forekomst = _ns_subelement(parent, tag, ns)
        # Tre-nivå beloep-innkapsling (per XSD)
        beloep_outer = _ns_subelement(forekomst, 'beloep', ns)
        beloep_middle = _ns_subelement(beloep_outer, 'beloep', ns)
        _ns_subelement(beloep_middle, 'beloep', ns, text=f'{amount:.2f}')
        # id — unik per forekomst innen dokumentet. kodetype.code er unik
        # innenfor en underkodeliste, så det er trygt som id.
        # id MÅ være identisk med resultatOgBalanseregnskapstype.code per
        # Skatteetaten — annet gir avvik 'idAvvikerFraKrav'.
        _ns_subelement(forekomst, 'id', ns, text=kodetype.code)
        # type-wrapper
        type_wrap = _ns_subelement(forekomst, 'type', ns)
        _ns_subelement(type_wrap, 'resultatOgBalanseregnskapstype', ns, text=kodetype.code)

    @api.model
    def _build_balanseregnskap(self, parent_el, ns, skattemelding):
        """Bygg <balanseregnskap>-strukturen under parent-elementet.

        Per XSD er <balanseregnskap> et BARN av <naeringsspesifikasjon>,
        plassert FØR <virksomhet> i sequence.

        Bruker CUMULATIVE-aggregering (sum fra all-tid til 31.12 inntektsår)
        for å beregne closing balance per Skatteetaten-kode.

        XSD-struktur:
          <balanseregnskap>
            <anleggsmiddel>
              <balanseverdiForAnleggsmiddel>
                <balanseverdi>...</balanseverdi>
              </balanseverdiForAnleggsmiddel>
            </anleggsmiddel>
            <omloepsmiddel>
              <balanseverdiForOmloepsmiddel>...</balanseverdiForOmloepsmiddel>
            </omloepsmiddel>
            <gjeldOgEgenkapital>
              <langsiktigGjeld>
                <gjeld>...</gjeld>
              </langsiktigGjeld>
              <kortsiktigGjeld>...</kortsiktigGjeld>
              <egenkapital>
                <kapital>...</kapital>
              </egenkapital>
            </gjeldOgEgenkapital>
          </balanseregnskap>
        """
        by_kodetype = self._aggregate_amounts_by_kodetype(
            skattemelding, cumulative=True,
        )
        if not by_kodetype:
            return

        # Grupper per underkodeliste
        by_grein = defaultdict(list)
        for kt, sums in by_kodetype.items():
            ukl = kt.underkodeliste
            if ukl not in _BALANSEREGNSKAP_GRENER:
                continue  # resultat-koder behandles av _build_resultatregnskap
            net = self._net_amount_for_kodetype(kt, sums['debit'], sums['credit'])
            if abs(net) < 0.005:
                continue
            by_grein[ukl].append((kt, net))

        # Phase 4 (2026-05-15): Inkluder årets resultat som syntetisk linje i
        # egenkapital-grenen. Uten dette har balansen 'driftsresultat'-hull
        # tilsvarende årets P&L (Odoo har ingen automatisk Current Year
        # Earnings-postering i balansekontoene — det er en computed line i
        # rapport-rendering, ikke ekte transaksjoner). Skatteetatens
        # balanseregnskap-validator forventer at eiendeler = gjeld + EK,
        # så vi må legge resultatet eksplisitt inn på 2050/2080-kodetypen.
        #
        # Skipping (2026-05-15): hvis brukeren har bokført et POSTERT
        # årsavslutningsbilag (closing_entry_id + state='posted'), så har
        # 2080/2050 allerede ekte saldo i regnskapet — kommer naturlig med
        # via balanse-aggregering. Ikke legg til syntetisk linje (ville
        # duplisere beløpet).
        #
        # NB sjekker explicit state='posted'. Hvis bilaget er i draft
        # (brukeren har laget det men ikke postet), vil balanse-
        # aggregering ikke se det (parent_state='posted' filter), men
        # heller ikke Phase 4 ville lagt til linje → balansen ville
        # manglet årets resultat. Vi bruker derfor:
        #   - posted closing entry → skip Phase 4 (balansen tar seg av det)
        #   - draft eller manglende closing → kjør Phase 4 (syntetisk linje)
        closing_posted = (
            skattemelding.closing_entry_id
            and skattemelding.closing_entry_id.state == 'posted'
        )
        if closing_posted:
            naeringsinntekt, underskudd = 0, 0
        else:
            naeringsinntekt, underskudd = self._calculate_naeringsresultat(
                skattemelding,
            )
        #
        # Konvensjon (matcher Fiken/Tripletex/Visma):
        #   Overskudd → +X på 2050 (Positiv egenkapital, fortegn=positiv)
        #   Underskudd → +|X| på 2080 (Negativ egenkapital, fortegn=negativ)
        #
        # Hvis 2050/2080 allerede har balansesaldo (akkumulerte tidligere
        # år's resultater bokført som ekte bilag), akkumuleres årets bidrag
        # ovenpå den eksisterende verdien.
        if naeringsinntekt > 0.5:
            self._add_aarsresultat_to_egenkapital(
                skattemelding, by_grein, target_code='2050',
                amount=naeringsinntekt,
            )
        elif underskudd > 0.5:
            self._add_aarsresultat_to_egenkapital(
                skattemelding, by_grein, target_code='2080',
                amount=underskudd,
            )

        if not by_grein:
            return

        balanseregnskap = _ns_subelement(parent_el, 'balanseregnskap', ns)
        wrapper_cache = {}

        def ensure_wrapper(path_segments):
            key = tuple(path_segments)
            if key in wrapper_cache:
                return wrapper_cache[key]
            parent = (balanseregnskap if len(path_segments) == 1
                      else ensure_wrapper(path_segments[:-1]))
            el = _ns_subelement(parent, path_segments[-1], ns)
            wrapper_cache[key] = el
            return el

        for ukl_name in _BALANSEREGNSKAP_GRENER:
            if ukl_name not in by_grein:
                continue
            path, item_tag = _BALANSEREGNSKAP_GRENER[ukl_name]
            wrapper = ensure_wrapper(path)
            for kt, net in sorted(by_grein[ukl_name], key=lambda x: x[0].code):
                self._emit_balanse_forekomst(wrapper, ns, item_tag, kt, net)

    @api.model
    def _add_aarsresultat_to_egenkapital(self, skattemelding, by_grein,
                                          target_code, amount):
        """Legg årets resultat inn som linje i egenkapital-grenen.

        Args:
            skattemelding: l10n.no.skattemelding-record (gir inntektsår)
            by_grein: defaultdict(list) {ukl_name → [(kodetype, net)]}
            target_code: '2050' (overskudd) eller '2080' (underskudd)
            amount: positivt beløp (signaturen er allerede normalisert via
                _calculate_naeringsresultat → naeringsinntekt/underskudd)

        Hvis target_code allerede finnes i by_grein['egenkapital'] (kontoen
        har posted bevegelser), akkumuleres årets bidrag oppå eksisterende
        verdi. Ellers legges en ny linje til.

        Hvis target_code-kodetype ikke finnes for inntektsåret (kodelisten
        ikke importert), logger vi advarsel og hopper over — balansen vil
        da ha samme hull som før, men submission blokkeres ikke.
        """
        Kodetype = self.env['l10n.no.skattemelding.kodetype']
        target_kt = Kodetype.search([
            ('code', '=', target_code),
            ('inntektsaar', '=', skattemelding.inntektsaar),
        ], limit=1)
        if not target_kt:
            _logger.warning(
                "Kodetype %s for inntektsår %s mangler — kan ikke legge "
                "årets resultat inn i balanseregnskap. Importer kodelisten.",
                target_code, skattemelding.inntektsaar,
            )
            return

        existing_list = by_grein.get('egenkapital', [])
        for idx, (kt, existing_amount) in enumerate(existing_list):
            if kt.code == target_code:
                # Akkumuler oppå eksisterende saldo (typisk hvis kontoen
                # har faktiske bilag fra tidligere år overført til den)
                existing_list[idx] = (kt, existing_amount + amount)
                return

        # Ingen eksisterende linje for target_code — legg til ny
        existing_list.append((target_kt, amount))
        by_grein['egenkapital'] = existing_list

    @api.model
    def _emit_balanse_forekomst(self, parent, ns, tag, kodetype, amount):
        """Emit en Balanseregnskapsforekomst-element under parent.

        Struktur (Balanseregnskapsforekomst — id, beloep, type i denne
        rekkefølgen per XSD. MERK: ulikt fra Resultatregnskapsforekomst
        som har beloep først).
          <balanseverdi|gjeld|kapital>
            <id>{kode}-{år}</id>
            <beloep>
              <beloep>
                <beloep>123.45</beloep>
              </beloep>
            </beloep>
            <type>
              <resultatOgBalanseregnskapstype>{kode}</resultatOgBalanseregnskapstype>
            </type>
          </balanseverdi|gjeld|kapital>
        """
        forekomst = _ns_subelement(parent, tag, ns)
        # id FØRST per XSD-sequence
        # id MÅ være identisk med resultatOgBalanseregnskapstype.code per
        # Skatteetaten — annet gir avvik 'idAvvikerFraKrav'.
        _ns_subelement(forekomst, 'id', ns, text=kodetype.code)
        # beloep — tre-nivå innkapsling
        beloep_outer = _ns_subelement(forekomst, 'beloep', ns)
        beloep_middle = _ns_subelement(beloep_outer, 'beloep', ns)
        _ns_subelement(beloep_middle, 'beloep', ns, text=f'{amount:.2f}')
        # type
        type_wrap = _ns_subelement(forekomst, 'type', ns)
        _ns_subelement(type_wrap, 'resultatOgBalanseregnskapstype', ns, text=kodetype.code)

    # NB 2026-05-18: tidligere duplikat _build_egenkapitalavstemming +
    # _sum_egenkapital_balance fra Phase C-reversion (minimum-struktur-
    # forsøk basert på v2-XSD) er slettet. De overskrev Iterasjon 1-
    # implementasjonen lenger opp (Python tar siste def i klassen).
    # Den nye implementasjonen er på linje ~624 (_build_egenkapital-
    # avstemming) og ~670 (_sum_egenkapital_balance).

    @api.model
    def build_konvolutt_xml(self, skattemelding):
        """Bygg ytre konvolutt med skattemelding + næringsspesifikasjon
        base64-enkodet inni.

        Det er denne XML-en som lastes opp til Altinn-instansen.
        """
        skattemelding.ensure_one()
        if not skattemelding.skattemelding_xml or not skattemelding.naeringsspesifikasjon_xml:
            raise UserError(_(
                "Indre XML-er er ikke generert enda. Klikk 'Generer XML' først."
            ))

        sme_b64 = base64.b64encode(
            skattemelding.skattemelding_xml.encode('utf-8'),
        ).decode('ascii')
        nsp_b64 = base64.b64encode(
            skattemelding.naeringsspesifikasjon_xml.encode('utf-8'),
        ).decode('ascii')

        ns = _NS_KONVOLUTT

        root = _ns_element('skattemeldingOgNaeringsspesifikasjonRequest', ns)
        dokumenter = _ns_subelement(root, 'dokumenter', ns)

        for doctype, content in (
            ('skattemeldingUpersonlig', sme_b64),
            ('naeringsspesifikasjon', nsp_b64),
        ):
            dok = _ns_subelement(dokumenter, 'dokument', ns)
            _ns_subelement(dok, 'type', ns, text=doctype)
            _ns_subelement(dok, 'encoding', ns, text='utf-8')
            _ns_subelement(dok, 'content', ns, text=content)

        # dokumentreferanseTilGjeldendeDokument er PÅKREVD for /valider-endpoint
        # men IKKE for /validertest. Vi emitter den kun hvis vi har en prior id
        # (fra korreksjon eller tidligere hentGjeldende). Tom = vi bruker
        # validertest under utvikling.
        prior_id = (
            skattemelding.dokumentidentifikator
            or (skattemelding.erstatter_skattemelding_id.dokumentidentifikator
                if skattemelding.erstatter_skattemelding_id else None)
        )
        if prior_id:
            ref = _ns_subelement(root, 'dokumentreferanseTilGjeldendeDokument', ns)
            _ns_subelement(ref, 'dokumenttype', ns, text='skattemeldingUpersonlig')
            _ns_subelement(ref, 'dokumentidentifikator', ns, text=prior_id)

        _ns_subelement(root, 'inntektsaar', ns, text=skattemelding.inntektsaar)

        innsending = _ns_subelement(root, 'innsendingsinformasjon', ns)
        # innsendingstype=komplett er kritisk for at Skatteetatens
        # visnings-tjeneste skal akseptere innsending for signering.
        # Tidligere kommentar antydet at "ikkeKomplett er trygt for
        # validertest; final submit oppgraderer til komplett i Phase 5",
        # men Phase 5 ble aldri implementert. Resultat: visnings-
        # tjenesten crashet med SMEVB-005 fordi innsendingstype ikke
        # var "komplett" (vår mistanke verifisert mot 2024-eksempel +
        # asynk-API-dokumentasjon, 2026-05-15).
        #
        # innsendingsformaal er påkrevd fra inntektsår 2025 (Skatteetaten
        # avvik 'innkommendeForespoerselManglerInnsendingsformaal' uten
        # den). Verdier: [egenfastsetting | klage | endringsanmodning].
        _ns_subelement(innsending, 'innsendingstype', ns, text='komplett')
        _ns_subelement(innsending, 'opprettetAv', ns, text=_KILDESYSTEM)
        _ns_subelement(innsending, 'innsendingsformaal', ns, text='egenfastsetting')

        return _serialize(root)
