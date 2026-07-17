"""Phase 2 Steg 2: rens ut Phase 1 stub-kodetype-records.

Phase 1 hadde en hand-skrevet stub-data-fil med ~28 records. XML-ID-ene
brukte format 'kodetype_3000' (uten år-prefiks). Phase 2 generer 669
records fra Skatteetatens offisielle kodeliste m. xmlid-format
'kodetype_<år>_<kode>'.

Hvis vi bare loadet det nye data-filen ville (code, inntektsaar)-unique
constraint blokkere innsetting fordi gamle records har samme verdier.
Pre-migration sletter gamle records eksplisitt.

Idempotent: hvis kjørt på fresh install (ingen gamle records), no-op.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        # Fresh install — ingen gamle records å rense
        return

    # Slett records m. xmlid-format 'kodetype_<kode>' (Phase 1-stub).
    # Phase 2-records bruker 'kodetype_<år>_<kode>' så ny prefiks-pattern
    # er trygt.
    cr.execute("""
        DELETE FROM ir_model_data
        WHERE module = 'l10n_no_account_skattemelding'
          AND model = 'l10n.no.skattemelding.kodetype'
          AND name ~ '^kodetype_[0-9]+$'
        RETURNING res_id, name
    """)
    deleted = cr.fetchall()

    if deleted:
        res_ids = [r[0] for r in deleted]
        # Slett selve records
        cr.execute(
            "DELETE FROM l10n_no_skattemelding_kodetype WHERE id = ANY(%s)",
            [res_ids],
        )
        _logger.info(
            "Phase 2 migration: ryddet %d Phase 1-stub kodetype-records: %s",
            len(deleted), [r[1] for r in deleted],
        )
    else:
        _logger.info("Phase 2 migration: ingen Phase 1-stub-records funnet (fresh install)")
