"""Eksporter mapping-snapshot for et selskap som CSV/JSON.

Gir en golden-snapshot av (account.code → Skatteetaten kodetype) for
et inntektsår, slik at kunder kan:
  1. Restore mapping etter staging-rebuild
  2. Sammenligne forventet mot faktisk (regresjons-fanger)
  3. Dokumentere semantiske valg gjort av regnskapsfører

Kjør via odoo-bin shell:
    odoo-bin shell -d <db> --no-http
    >>> exec(open('tools/export_mapping_snapshot.py').read())
    >>> export_mapping_snapshot(company_id=1, year=2025)
"""
import csv
import json
from datetime import datetime


def export_mapping_snapshot(company_id, year, output_dir='/tmp'):
    """Eksporter alle mappinger for kontoer aktive i inntektsåret.

    Output: CSV + JSON i output_dir.
    """
    company = env['res.company'].browse(company_id)
    print(f"=== Eksporterer mapping for {company.name} ({year}) ===")

    # Hent aktive kontoer cumulative
    env.cr.execute("""
        SELECT DISTINCT aml.account_id
        FROM account_move_line aml
        JOIN account_move am ON aml.move_id = am.id
        WHERE am.company_id = %s AND am.state = 'posted'
          AND aml.date <= MAKE_DATE(%s, 12, 31)
    """, [company_id, year])
    active_ids = [r[0] for r in env.cr.fetchall()]
    accounts = env['account.account'].sudo().with_company(company).browse(
        active_ids,
    ).filtered('code').sorted('code')

    rows = []
    for acc in accounts:
        kt = acc.l10n_no_skattemelding_kodetype_id
        rows.append({
            'odoo_code': acc.code,
            'odoo_name': acc.name or '',
            'odoo_type': acc.account_type or '',
            'skattekode': kt.code if kt else '',
            'skattenavn': kt.name if kt else '',
            'underkodeliste': kt.underkodeliste if kt else '',
            'verified': acc.l10n_no_skattemelding_kodetype_verified,
        })

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    safe_name = company.name.replace(' ', '_').replace('/', '_')
    base_path = f"{output_dir}/mapping_snapshot_{safe_name}_{year}_{timestamp}"

    # CSV (regneark-vennlig)
    csv_path = f"{base_path}.csv"
    with open(csv_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys() if rows else [])
        writer.writeheader()
        writer.writerows(rows)
    print(f"  Skrev {csv_path} ({len(rows)} rader)")

    # JSON (maskinlesbart for restore-script)
    json_path = f"{base_path}.json"
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump({
            'company': company.name,
            'company_id': company_id,
            'year': year,
            'exported_at': timestamp,
            'mappings': rows,
        }, f, indent=2, ensure_ascii=False)
    print(f"  Skrev {json_path}")

    return rows


def import_mapping_snapshot(json_path, year=None):
    """Restore mappinger fra JSON-fil.

    Match kontoer på code (per-company). Setter mapping + verified=True.
    """
    with open(json_path, encoding='utf-8') as f:
        snapshot = json.load(f)
    company = env['res.company'].browse(snapshot['company_id'])
    use_year = year or snapshot['year']
    print(f"=== Restoring {len(snapshot['mappings'])} mappinger for "
          f"{snapshot['company']}, år {use_year} ===")

    applied = 0
    not_found = []
    for row in snapshot['mappings']:
        if not row['skattekode']:
            continue
        acc = env['account.account'].sudo().with_company(company).search([
            ('code', '=', row['odoo_code']),
            ('company_ids', 'in', company.id),
        ], limit=1)
        kt = env['l10n.no.skattemelding.kodetype'].sudo().search([
            ('code', '=', row['skattekode']),
            ('inntektsaar', '=', use_year),
        ], limit=1)
        if acc and kt:
            acc.write({
                'l10n_no_skattemelding_kodetype_id': kt.id,
                'l10n_no_skattemelding_kodetype_verified': row.get('verified', False),
            })
            applied += 1
        else:
            not_found.append(f"{row['odoo_code']} → {row['skattekode']}")

    print(f"  Anvendte {applied}/{len(snapshot['mappings'])}")
    if not_found:
        print(f"  Mangler: {not_found[:5]}")
