"""Slett admin-secreten fra ir.config_parameter.

`l10n_no_eristo.onboard_admin_secret` var hovednøkkelen til gatewayens
kunderegister — den som kan opprette og endre HVILKEN SOM HELST kunde,
ikke bare selskapet den lå hos. Customer-onboard-wizarden som brukte den
er fjernet i denne versjonen; onboarding kjøres nå på gatewayen selv, fra
internt nett.

Å bare slutte å lese parameteren ville latt secreten bli liggende i
databasen. Den må aktivt bort: en Odoo-admin med server-action-rettigheter
kan lese ir.config_parameter, og i DB-per-kunde-modellen ville én kundes
DB dermed inneholdt hovednøkkelen for alle. Det er samme resonnement som
holder Maskinporten-nøkkelen utenfor Odoo-prosessen (beslutning 7).

Merk: verdien kan ha rukket å bli lest av noen. Slettingen her lukker
lagringen, ikke eventuell tidligere eksponering — roter secreten på
gatewayen (GATEWAY_ADMIN_SECRET i .env.prod) hvis den har vært i bruk.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return  # fresh install — parameteren har aldri eksistert
    cr.execute("""
        DELETE FROM ir_config_parameter
        WHERE key = 'l10n_no_eristo.onboard_admin_secret'
    """)
    # MÅ leses her: cr.rowcount gjelder siste setning, og DROP TABLE
    # under gir -1. Uten dette logget migreringen «Slettet -1 rad(er)»
    # på hver eneste oppgradering — også når det ikke fantes noen rad.
    slettet = cr.rowcount
    # Wizard-tabellen overlever ellers oppgraderingen: Odoos
    # ir.model._drop_table() slår opp modellen i registryet, og den er
    # borte når Python-klassen er slettet. Kolonnen api_key_display holdt
    # kundens API-key i KLARTEKST (vist én gang som backup), så tabellen
    # skal ikke bli liggende igjen som et glemt lager. Transient-modeller
    # vacuumeres normalt, men etter at modellen er borte kjører ingen
    # vacuum på den igjen.
    cr.execute("DROP TABLE IF EXISTS l10n_no_eristo_customer_onboard_wizard")
    if slettet > 0:
        _logger.warning(
            "Slettet %s rad(er) av l10n_no_eristo.onboard_admin_secret. "
            "Hadde denne en gyldig verdi, bør GATEWAY_ADMIN_SECRET roteres "
            "på gatewayen — den har ligget lesbar for Odoo-admins.",
            slettet,
        )
