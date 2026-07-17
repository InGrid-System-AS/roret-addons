#!/usr/bin/env python3
"""Generer skattemelding_kodetype_data.xml fra Skatteetatens kodeliste.

Henter offisiell kodeliste fra Skatteetaten GitHub for hvert år og
filtrerer ned til koder relevant for vanlige AS (oevrigSelskap +
fullRegnskapsplikt). Skipper bank/forsikring/IFRS/filialregnskap.

Bruk:
    python3 generate_kodetype_data.py [--years 2024,2025,2026]

Output: skriver til ../data/skattemelding_kodetype_data.xml.

Kjør på nytt når Skatteetaten publiserer kodeliste for nytt inntektsår.
"""
import argparse
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

# Skatteetatens publiserte kodelister per inntektsår.
KODELISTE_URL = (
    'https://raw.githubusercontent.com/Skatteetaten/skattemeldingen/master/'
    'src/resources/kodeliste/{year}/{year}_resultatregnskapOgBalanse.xml'
)
NS = {'k': 'urn:no:skatteetaten:informasjonsforvaltning:kodeliste:v2'}

# Underkodelister vi importerer. Bank/forsikring/IFRS-spesialer skippes;
# de er for spesialiserte selskaper og ikke relevant for vanlige AS.
RELEVANT_UNDERKODELISTER = {
    'salgsinntekt', 'annenDriftsinntekt',
    'varekostnad', 'loennskostnad', 'annenDriftskostnad',
    'finansinntekt', 'finanskostnad', 'skattekostnad',
    'balanseverdiForAnleggsmiddel', 'balanseverdiForOmloepsmiddel',
    'egenkapital', 'langsiktigGjeld', 'kortsiktigGjeld',
    'resultatkomponentForIFRSForetak',
}


def fetch_kodeliste(year):
    """Last ned XML for ett år. Returner ElementTree.Element (root)."""
    url = KODELISTE_URL.format(year=year)
    print(f"  Laster {url}...", file=sys.stderr)
    with urllib.request.urlopen(url, timeout=30) as resp:
        return ET.fromstring(resp.read())


def parse_kode(kode_el, underkodeliste_name):
    """Returnerer dict med felter for én kode, eller None hvis ikke relevant."""
    tn = kode_el.find('k:tekniskNavn', NS).text.strip()

    # Skip ikke-numeriske koder. Bank/forsikring bruker dot-separated
    # (1.05.0.10) og filialregnskap bruker camelCase-strings — vi vil
    # bare ha rene NS 4102-koder for vanlige AS.
    if not tn.isdigit():
        return None

    kt = kode_el.find('k:kodetillegg', NS)
    if kt is None:
        return None

    # Sjekk at koden gjelder oevrigSelskap (vanlig AS).
    vts = [vt.text for vt in kt.findall('k:virksomhetstype', NS)]
    if 'oevrigSelskap' not in vts:
        return None

    # Sjekk at den gjelder ved fullRegnskapsplikt (default for AS).
    rps = [r.text for r in kt.findall('k:regnskapspliktstype', NS)]
    gjelder_full = 'fullRegnskapsplikt' in rps

    # Hent navn — foretrekker nb_NO, fallback til nynorsk eller første.
    name = ''
    visningsnavn = kode_el.find('k:visningsnavn', NS)
    if visningsnavn is not None:
        for st in visningsnavn.findall('k:spraakTekst', NS):
            lang = st.find('k:spraak', NS)
            txt = st.find('k:tekst', NS)
            if lang is not None and lang.text == 'nb_NO' and txt is not None:
                name = txt.text.strip()
                break
        if not name:  # fallback til første spraakTekst
            first = visningsnavn.find('k:spraakTekst/k:tekst', NS)
            if first is not None:
                name = first.text.strip()

    # Hent kort beskrivelse (begrepsreferanse).
    begrep = kode_el.find('k:begrepsreferanse', NS)
    description = begrep.text.strip() if begrep is not None and begrep.text else ''

    # Hent korttype_naeringsspesifikasjon (XML-element-navn).
    korttype_el = kt.find('k:korttypeNaeringsspesifikasjonISme', NS)
    korttype = korttype_el.text.strip() if korttype_el is not None and korttype_el.text else ''

    # Fortegn (positiv/negativ).
    kategori_el = kt.find('k:kategori', NS)
    fortegn = kategori_el.text.strip() if kategori_el is not None and kategori_el.text else ''

    # Verdsettingsrabatt-flagg.
    rabatt_el = kt.find('k:BalansekontoGirVerdsettingsrabatt', NS)
    gir_rabatt = (
        rabatt_el is not None and rabatt_el.text
        and rabatt_el.text.strip().lower() == 'true'
    )

    return {
        'code': tn,
        'name': name,
        'description': description,
        'underkodeliste': underkodeliste_name,
        'fortegn': fortegn,
        'korttype': korttype,
        'gjelder_full': gjelder_full,
        'gir_rabatt': gir_rabatt,
    }


def parse_year(year):
    """Parse ett års kodeliste, returner liste med kode-dicts."""
    root = fetch_kodeliste(year)
    out = []
    for ukl in root.findall('k:underkodeliste', NS):
        ukl_name = ukl.find('k:tekniskNavn', NS).text.strip()
        if ukl_name not in RELEVANT_UNDERKODELISTER:
            continue
        for k in ukl.findall('k:kode', NS):
            parsed = parse_kode(k, ukl_name)
            if parsed:
                parsed['inntektsaar'] = year
                out.append(parsed)
    return out


def xml_escape(s):
    """XML-escape en streng for trygg inkludering i tag-innhold."""
    if not s:
        return ''
    return (
        s.replace('&', '&amp;')
         .replace('<', '&lt;')
         .replace('>', '&gt;')
         .replace('"', '&quot;')
    )


def emit_xml(records, output_path):
    """Skriv Odoo data-XML fra liste med records."""
    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<!--',
        '  AUTOGENERERT av tools/generate_kodetype_data.py.',
        '  IKKE rediger manuelt — endringer overskrives ved neste generering.',
        '  Kjør på nytt: python3 tools/generate_kodetype_data.py',
        '',
        '  Kilde: github.com/Skatteetaten/skattemeldingen — '
        'src/resources/kodeliste/<year>/<year>_resultatregnskapOgBalanse.xml',
        '-->',
        '<odoo noupdate="0">',
    ]

    # Sortér: år desc, så underkodeliste, så code
    records = sorted(records, key=lambda r: (-r['inntektsaar'], r['underkodeliste'], r['code']))

    current_section = None
    for r in records:
        section = f"{r['inntektsaar']} — {r['underkodeliste']}"
        if section != current_section:
            lines.append('')
            lines.append(f'    <!-- {section} -->')
            current_section = section

        xid = f"kodetype_{r['inntektsaar']}_{r['code']}"
        lines.append(f'    <record id="{xid}" model="l10n.no.skattemelding.kodetype">')
        lines.append(f'        <field name="code">{xml_escape(r["code"])}</field>')
        lines.append(f'        <field name="name">{xml_escape(r["name"])}</field>')
        lines.append(f'        <field name="inntektsaar">{r["inntektsaar"]}</field>')
        lines.append(f'        <field name="underkodeliste">{r["underkodeliste"]}</field>')
        if r['fortegn']:
            lines.append(f'        <field name="fortegn">{r["fortegn"]}</field>')
        if r['korttype']:
            lines.append(f'        <field name="korttype_naeringsspesifikasjon">{xml_escape(r["korttype"])}</field>')
        if r['description']:
            # Lang beskrivelse — bruk multiline med text-content
            desc = xml_escape(r['description'])
            lines.append(f'        <field name="description">{desc}</field>')
        if r['gir_rabatt']:
            lines.append('        <field name="gir_verdsettingsrabatt" eval="True"/>')
        if not r['gjelder_full']:
            lines.append('        <field name="gjelder_full_regnskapsplikt" eval="False"/>')
        lines.append('    </record>')

    lines.append('</odoo>')
    output_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(f"Skrev {len(records)} kodetype-records til {output_path}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--years',
        default='2024,2025,2026',
        help='Komma-separert liste inntektsår (default: 2024,2025,2026)',
    )
    parser.add_argument(
        '--output',
        default=None,
        help='Output-path (default: ../data/skattemelding_kodetype_data.xml)',
    )
    args = parser.parse_args()

    years = [int(y.strip()) for y in args.years.split(',') if y.strip()]
    out_path = Path(args.output) if args.output else (
        Path(__file__).parent.parent / 'data' / 'skattemelding_kodetype_data.xml'
    )

    all_records = []
    for year in years:
        print(f"\n=== Inntektsår {year} ===", file=sys.stderr)
        try:
            records = parse_year(year)
            print(f"  {len(records)} relevante koder", file=sys.stderr)
            all_records.extend(records)
        except urllib.error.HTTPError as e:
            print(f"  Skipper {year}: HTTP {e.code}", file=sys.stderr)
            continue

    print(f"\nTotalt {len(all_records)} records på tvers av {len(years)} år", file=sys.stderr)
    emit_xml(all_records, out_path)


if __name__ == '__main__':
    main()
