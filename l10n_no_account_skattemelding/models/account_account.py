"""Skattemelding-mapping på account.account.

Hver kontoen kobles til en Skatteetaten-kodetype som forteller hvilken
XML-grein kontosaldoen havner i ved næringsspesifikasjon-generering.

Mapping-strategi (tre-trinns prioritet):
  1. Manuell override (l10n_no_skattemelding_kodetype_id satt på record)
  2. Auto-suggest ved hjelp av NS 4102-prefix-match
     (action_l10n_no_skattemelding_auto_suggest fyller inn forslag)
  3. Manglende mapping → coverage-validering blokkerer XML-bygging

Mapping er global (ikke company_dependent) — to selskaper som deler
samme konto-record antas å ville rapportere det til samme Skatteetaten-
kode. For unntak kan selskapene ha hver sin konto-record m. ulik
mapping.
"""
import logging

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)


class AccountAccount(models.Model):
    _inherit = 'account.account'

    l10n_no_skattemelding_kodetype_id = fields.Many2one(
        'l10n.no.skattemelding.kodetype',
        string="Skatteetaten kodetype",
        ondelete='restrict',
        help="Skatteetatens resultatregnskap-/balanse-kode som denne kontoen "
             "rapporteres under i næringsspesifikasjon. Sett manuelt eller "
             "bruk 'Foreslå kodetyper'-actionen på selskapet for å auto-"
             "fylle basert på NS 4102-prefix-match.",
    )
    l10n_no_skattemelding_kodetype_verified = fields.Boolean(
        string="Mapping verifisert",
        default=False,
        copy=False,
        help="Når denne er True har en bruker eksplisitt godkjent at "
             "kodetype-mappingen er semantisk korrekt for denne kontoen. "
             "Auto-suggest setter den til False — bruker må manuelt verifisere "
             "før skattemelding-XML kan bygges. Hindrer at maskinell heuristikk "
             "produserer skattemelding uten human review.",
    )
    # Related-felt for å vise Skatteetaten-beskrivelsen direkte i UI ved
    # siden av M2O-en (Sikring #2 mot semantisk feilmapping)
    l10n_no_skattemelding_kodetype_description = fields.Text(
        related='l10n_no_skattemelding_kodetype_id.description',
        string="Skatteetaten beskrivelse",
        readonly=True,
        help="Skatteetatens definisjon av valgt kode. Les denne nøye — "
             "den må matche semantisk hva kontoen brukes til.",
    )
    l10n_no_skattemelding_kodetype_underkodeliste = fields.Selection(
        related='l10n_no_skattemelding_kodetype_id.underkodeliste',
        string="Skatteetaten underkodeliste",
        readonly=True,
    )

    def _l10n_no_skattemelding_suggest_kodetype(self, inntektsaar):
        """Foreslå Skatteetaten-kodetype for denne kontoen for et inntektsår.

        Algoritme (fem-trinns prioritet):
          1. Eksakt match (kontens code == kodetype.code)
          2. Decade-rundet match (3020 → 3020-bucket, 3025 → 3020-bucket)
             Søk kodetype.code = (account.code // 10 * 10)
          3. Hundred-rundet match (3020 → 3000, 3025 → 3000)
             Søk kodetype.code = (account.code // 100 * 100)
             **Dette løser bug**: 3020 'Services sales' havnet under 3008
             'Petroleum' fordi 3008 mekanisk var nærmeste ≤ 3020.
             Now: 3020 mapper til 3000 (decade-marker) som er semantisk
             korrekt for generelle salgsinntekter.
          4. Høyeste kodetype.code ≤ kontens code i samme første-siffer-serie
             (legacy fallback for koder som verken er decade- eller
             hundred-runde markører)
          5. Laveste kodetype.code ≥ kontens code i samme serie (siste
             fallback — Skatteetatens kodeliste starter høyere enn
             Odoo-koden, eks: 4000 → 4001)

        Returnerer ett kodetype-record (eller tom recordset).

        Alle trinn filtrerer bort kodetyper som er inkompatible med
        selskapets regnskapspliktstype (eks: kode 1295 'Driftsmidler som
        avskrives lineært' gjelder kun ved begrenset regnskapsplikt og
        kan ikke foreslås til et fullRegnskapsplikt-selskap — det ville
        utløse N_FEIL_ANLEGGSMIDDELTYPE-avvik fra Skatteetaten).

        Eksempler:
          '3020 Sales services'  → 3000 (Trinn 3 — hundred-marker)
          '3000 Sales merchandise'→ 3000 (Trinn 1 — eksakt)
          '1239 Vans zero emission' → 1238 (Trinn 4 — legacy fallback)
          '1080 Goodwill'        → 1080 (Trinn 1 — eksakt)
          '4000 Purchase'        → 4001 (Trinn 5 — fallback opp)
        """
        self.ensure_one()
        if not self.code or not self.code[0].isdigit():
            return self.env['l10n.no.skattemelding.kodetype']
        try:
            code_int = int(self.code)
        except ValueError:
            return self.env['l10n.no.skattemelding.kodetype']
        first_digit = self.code[0]
        kt = self.env['l10n.no.skattemelding.kodetype']
        # Bygg regnskapspliktstype-filter ut fra selskapets innstilling
        # — kodetyper Skatteetaten har flagget inkompatible skal ikke
        # foreslås.
        # NB: account.account har company_ids (Many2many) i Odoo 19, ikke
        # company_id. Vi bruker første tilknyttede selskap, eller faller
        # tilbake på env.company hvis kontoen ikke er knyttet til noen.
        company = (self.company_ids and self.company_ids[0]) or self.env.company
        compat_domain = []
        if (
            (company.l10n_no_skattemelding_regnskapspliktstype
             or 'fullRegnskapsplikt') == 'fullRegnskapsplikt'
        ):
            compat_domain.append(('gjelder_full_regnskapsplikt', '=', True))

        # Trinn 1: eksakt match
        match = kt.search(compat_domain + [
            ('inntektsaar', '=', inntektsaar),
            ('code', '=', self.code),
        ], limit=1)
        if match:
            return match

        # Trinn 2: decade-rundet (3025 → 3020)
        decade = (code_int // 10) * 10
        if decade != code_int:
            match = kt.search(compat_domain + [
                ('inntektsaar', '=', inntektsaar),
                ('code', '=', str(decade)),
            ], limit=1)
            if match:
                return match

        # Trinn 3: hundred-rundet (3020 → 3000)
        # Dette løser bug: 3020 burde IKKE havne under 3008 (petroleum).
        hundred = (code_int // 100) * 100
        if hundred != code_int and hundred != decade:
            match = kt.search(compat_domain + [
                ('inntektsaar', '=', inntektsaar),
                ('code', '=', str(hundred)),
            ], limit=1)
            if match:
                return match

        # Trinn 4: legacy prefix-fallback (høyeste ≤ kontens code i serien)
        match = kt.search(compat_domain + [
            ('inntektsaar', '=', inntektsaar),
            ('code', '<=', self.code),
            ('code', '=like', f'{first_digit}%'),
        ], order='code desc', limit=1)
        if match:
            return match

        # Trinn 5: ekstrem fallback (laveste ≥, samme serie)
        return kt.search(compat_domain + [
            ('inntektsaar', '=', inntektsaar),
            ('code', '>=', self.code),
            ('code', '=like', f'{first_digit}%'),
        ], order='code asc', limit=1)

    def action_l10n_no_skattemelding_auto_suggest(self, inntektsaar=None):
        """Fyll inn auto-forslag for alle kontoer i selvalget som mangler mapping.

        Args:
          inntektsaar: hvilket inntektsår kodelisten skal hentes fra. Hvis
            None brukes forrige år (NÅVÆRENDE året - 1). Eksplisitt
            parameter brukes av tester for determinisme.

        Kun kontoer uten manuell mapping oppdateres — eksisterende kobling
        overskrives ikke.
        """
        if inntektsaar is None:
            from datetime import date
            inntektsaar = date.today().year - 1
        updated = 0
        not_found = []
        for account in self:
            if account.l10n_no_skattemelding_kodetype_id:
                continue  # respekter manuell override
            suggestion = account._l10n_no_skattemelding_suggest_kodetype(inntektsaar)
            if suggestion:
                # KRITISK: verified=False — bruker må eksplisitt verifisere
                # før XML kan bygges. Hindrer at maskinell heuristikk
                # produserer feil mapping uten human review.
                account.write({
                    'l10n_no_skattemelding_kodetype_id': suggestion.id,
                    'l10n_no_skattemelding_kodetype_verified': False,
                })
                updated += 1
            elif account.code:
                not_found.append(account.code)

        _logger.info(
            "Skattemelding auto-suggest: %d kontoer oppdatert, %d uten "
            "match (%s)", updated, len(not_found), ', '.join(not_found[:10]),
        )

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'warning' if updated else 'warning',
                'title': _("Auto-forslag fullført — KREVER VERIFISERING"),
                'message': _(
                    "%(updated)d kontoer fikk auto-forslag for inntektsår "
                    "%(year)d. %(not_found)d uten match.\n\n"
                    "VIKTIG: Forslagene er IKKE verifisert. Du må manuelt "
                    "godkjenne hver mapping før XML kan bygges. Klikk på "
                    "hver konto og bekreft Skatteetaten-beskrivelsen matcher "
                    "semantikken, så trykk 'Marker som verifisert'.",
                    updated=updated, year=inntektsaar, not_found=len(not_found),
                ),
                'sticky': True,
            },
        }

    def action_l10n_no_skattemelding_verify_mapping(self):
        """Marker valgte kontoers mapping som verifisert (human-confirmed).

        Brukes etter at en regnskapsfører/regnskapsansvarlig har gjennomgått
        auto-forslagene og bekreftet semantisk korrekthet mot Skatteetaten-
        beskrivelsen. Verified=True er en forutsetning for XML-bygging.
        """
        verified_count = 0
        for account in self:
            if account.l10n_no_skattemelding_kodetype_id:
                account.l10n_no_skattemelding_kodetype_verified = True
                verified_count += 1
            else:
                _logger.warning(
                    "Konto %s har ingen kodetype-mapping — kan ikke verifiseres",
                    account.code,
                )
        _logger.info(
            "Skattemelding mapping verifisert: %d kontoer av %d",
            verified_count, len(self),
        )
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'success',
                'title': _("Mappinger verifisert"),
                'message': _(
                    "%(n)d kontoer markert som verifisert. Klar for XML-bygging.",
                    n=verified_count,
                ),
                'sticky': False,
            },
        }

    def action_l10n_no_skattemelding_unverify_mapping(self):
        """Fjern verifisering — brukes hvis mapping må re-vurderes."""
        self.write({'l10n_no_skattemelding_kodetype_verified': False})
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'warning',
                'title': _("Verifisering fjernet"),
                'message': _("%(n)d kontoer er nå unverified.", n=len(self)),
                'sticky': False,
            },
        }
