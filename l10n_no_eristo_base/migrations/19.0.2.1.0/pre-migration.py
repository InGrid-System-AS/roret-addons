"""Drop char-versjonen av l10n_no_eristo_active_scopes før Json re-opprettes.

Phase 3a (v19.0.2.0.0) introduserte feltet som Char med komma-separasjon.
v19.0.2.1.0 endrer til fields.Json (JSONB i Postgres). Odoo's auto-schema
kan ikke endre kolonne-type fra varchar til jsonb in-place — vi må droppe
og la den re-opprettes.

Trygt fordi feltet er nytt og ingen produksjons-data ennå (kun introdusert
2026-05-11, ingen kunder har aktivert via wizarden enda).
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return  # fresh install — ikke noe å droppe
    cr.execute("""
        ALTER TABLE res_company
        DROP COLUMN IF EXISTS l10n_no_eristo_active_scopes
    """)
    _logger.info(
        "Phase 3a v2.1: droppet l10n_no_eristo_active_scopes (Char) — "
        "Odoo re-oppretter som JSONB i post-init."
    )
