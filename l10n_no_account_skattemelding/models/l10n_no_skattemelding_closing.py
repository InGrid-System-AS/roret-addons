"""Årsavslutningsbilag for skattemelding.

Når et selskap har levert årets P&L (kjent etter at alle bilag er postet),
lukkes P&L mot 8800 Årsresultat og resultatet overføres til 2050 (annen
egenkapital, overskudd) eller 2080 (udekket tap, underskudd) i balansen.

Norsk god regnskapsskikk (NRS) krever dette for AS — det fjerner P&L-
saldoer fra rapporteringen, gjør balanseregnskap selvstendig, og gjør
neste regnskapsår 'rent'. Tradisjonell norsk regnskapsfører-praksis.

Hvorfor det er integrert i skattemelding-modulen:
  - Skattemelding sendes typisk samtidig som årsavslutning
  - Bilaget må være postet før låsing av regnskapsår (fiscalyear_lock_date)
  - Vår Phase 4 (syntetisk 2080-linje i balanse-XML) krever at vi vet om
    bilaget finnes for å unngå duplikat

Tegn-konvensjon (viktig — invertet fra `account.move.line.balance`):
  Regnskapsresultatet beregnes som: K-saldoer (inntekter) - D-saldoer (kostnader).
  Negativ Odoo-balance på inntektskonti = positiv inntekt → bidrar til overskudd.
  Positiv Odoo-balance på kostnadskonti = positiv kostnad → bidrar til underskudd.
  Derfor: `net_result = -sum(account.balance)` gir konvensjonelt fortegn der
  positivt tall = overskudd, negativt = underskudd.

Bilags-strukturen (overskudd-eksempel, 100 NOK):
  K 3000 Salgsinntekt          100   (lukker inntekt-saldo)
  D 8800 Årsresultat           100   (motposter inntekten)
  D 8800 Årsresultat           100   (overføring til EK)  ← samme konto 2 ganger
  K 2050 Annen egenkapital     100   (overskudd legges til EK)

Bilags-strukturen (underskudd-eksempel, 100 NOK):
  D 7700 Kostnader             100   (lukker kostnad-saldo)
  K 8800 Årsresultat           100   (motposter kostnaden)
  D 2080 Udekket tap           100   (underskudd reduserer EK)
  K 8800 Årsresultat           100   (overføring fra årsresultat)
"""
import logging
from collections import defaultdict
from datetime import date as _date

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class L10nNoSkattemeldingClosing(models.Model):
    _inherit = 'l10n.no.skattemelding'

    # ----------------------------------------------------------------------
    # Konto-resolving — bruker company-konfig hvis satt, ellers
    # NS 4102-kode-søk som fallback
    # ----------------------------------------------------------------------

    # NS 4102-kode → forventet account_type. Forhindrer at fallback-søk
    # treffer feil konto hvis kunden har omdøpt 8800/2050/2080 til en
    # annen type (eks: 8800 brukt som bank-konto). Vi krever at både
    # kode OG type matcher før vi godtar konto.
    _CLOSING_ACCOUNT_TYPES = {
        '8800': ('expense', 'income'),  # transit P&L-konto
        '2050': ('equity',),
        '2080': ('equity',),
    }

    def _resolve_closing_account(self, code, configured_field):
        """Slå opp konto: konfig-felt først, deretter NS 4102-kode + type-søk.

        Args:
            code: NS 4102-kodestreng (eks. '8800', '2050', '2080')
            configured_field: feltnavn på res.company som kan overstyre
                (eks. 'l10n_no_skattemelding_aarsresultat_account_id')

        Raises:
            UserError hvis verken konfig eller kode-søk gir treff.
        """
        self.ensure_one()
        company = self.company_id
        configured = company[configured_field]
        if configured:
            return configured
        ctx_self = self.with_context(allowed_company_ids=company.ids)
        Account = ctx_self.env['account.account']
        expected_types = self._CLOSING_ACCOUNT_TYPES.get(code, ())
        domain = [
            ('company_ids', 'in', company.id),
            ('code', '=', code),
        ]
        if expected_types:
            domain.append(('account_type', 'in', list(expected_types)))
        found = Account.search(domain, limit=1)
        if not found:
            raise UserError(_(
                "Fant ikke konto med kode '%(code)s' og forventet type "
                "%(types)s i kontoplanen for %(company)s. Sett feltet "
                "'%(field)s' på selskapet eller opprett konto med denne "
                "koden (NS 4102-default).",
                code=code,
                types=expected_types or '(any)',
                company=company.name,
                field=configured_field,
            ))
        return found

    def _resolve_closing_journal(self):
        """Velg journal for avslutningsbilaget.

        Konfig først (company.l10n_no_skattemelding_closing_journal_id),
        deretter første general-journal som fallback.
        """
        self.ensure_one()
        company = self.company_id
        configured = company.l10n_no_skattemelding_closing_journal_id
        if configured:
            return configured
        journal = self.env['account.journal'].search([
            ('company_id', '=', company.id),
            ('type', '=', 'general'),
        ], limit=1)
        if not journal:
            raise UserError(_(
                "Fant ingen general-journal for %(c)s. Opprett en "
                "Miscellaneous-journal eller sett "
                "'Avslutningsjournal' på selskapet.",
                c=company.name,
            ))
        return journal

    # ----------------------------------------------------------------------
    # Beregn P&L-saldoer per konto (kun ordinære, ikke avslutningsbilag)
    # ----------------------------------------------------------------------

    def _calculate_pl_lines_for_closing(self):
        """Returner liste av (account_id, balance) for resultatkontoer
        med posted bevegelser i året, EKSKLUDERT eksisterende
        avslutningsbilag (idempotency).

        Balance = debit - credit, returneres som-er. Caller bestemmer
        hvilken side bilaget havner på.
        """
        self.ensure_one()
        year = self.inntektsaar
        company = self.company_id
        ctx_self = self.with_context(allowed_company_ids=company.ids)
        AML = ctx_self.env['account.move.line']
        lines = AML.search([
            ('company_id', '=', company.id),
            ('parent_state', '=', 'posted'),
            ('move_id.l10n_no_skattemelding_closing', '=', False),
            ('account_id.account_type', 'in', (
                'income', 'income_other',
                'expense', 'expense_depreciation',
                'expense_direct_cost', 'expense_other',
            )),
            ('date', '>=', _date(year, 1, 1)),
            ('date', '<=', _date(year, 12, 31)),
        ])
        per_account = defaultdict(lambda: {'debit': 0.0, 'credit': 0.0})
        for ln in lines:
            per_account[ln.account_id.id]['debit'] += ln.debit
            per_account[ln.account_id.id]['credit'] += ln.credit
        # Returner kun konti med ikke-null saldo
        result = []
        for aid, sums in per_account.items():
            bal = sums['debit'] - sums['credit']
            if abs(bal) >= 0.005:
                result.append((aid, sums['debit'], sums['credit'], bal))
        return result

    # ----------------------------------------------------------------------
    # Action: Opprett avslutningsbilag
    # ----------------------------------------------------------------------

    def action_l10n_no_skattemelding_create_closing_entry(self):
        """Opprett 4-linjes årsavslutningsbilag i Miscellaneous-journal.

        Flyt:
          1. Verifiser at det ikke finnes et eksisterende avslutningsbilag
             (closing_entry_id) — hvis ja, åpne det istedet
          2. Beregn P&L-saldoer per konto (kun posted, ikke andre closing-bilag)
          3. Hvis sum = 0 (eller ingen P&L-bevegelser): vis melding, returner
          4. Resolve 8800/2050/2080-konti + journal (konfig eller kode-søk)
          5. Bygg 4-linjes bilag (eller flere hvis flere P&L-kontoer)
          6. Returner form-action på det opprettede bilaget (draft) for
             review før brukeren poster det manuelt
        """
        self.ensure_one()
        year = self.inntektsaar
        company = self.company_id

        # Idempotency: hvis bilag allerede finnes, åpne det
        if self.closing_entry_id:
            return {
                'type': 'ir.actions.act_window',
                'name': _("Eksisterende avslutningsbilag"),
                'res_model': 'account.move',
                'res_id': self.closing_entry_id.id,
                'view_mode': 'form',
                'target': 'current',
            }

        # Beregn P&L-saldoer
        pl_lines = self._calculate_pl_lines_for_closing()
        if not pl_lines:
            raise UserError(_(
                "Ingen P&L-bevegelser i %(y)d som krever avslutning. "
                "Avslutningsbilag er ikke nødvendig (resultat = 0).",
                y=year,
            ))

        # Beregn netto resultat i KONVENSJONELT regnskaps-fortegn:
        # positiv = overskudd, negativ = underskudd.
        #
        # Odoo-balance er D - K. For inntektskonti (K-saldo) gir det
        # negativt tall — men i regnskaps-konvensjon er det positiv
        # inntekt. Vi inverterer derfor sum(bal):
        #   sum(bal) for kostnadskonti  > 0 (D-saldo)
        #   sum(bal) for inntektskonti < 0 (K-saldo)
        #   regnskapsresultat = inntekter - kostnader = -sum(bal)
        accounting_bal_sum = sum(bal for (aid, d, c, bal) in pl_lines)
        net_result = -accounting_bal_sum  # konvensjonelt fortegn

        # Resolve konti og journal
        aarsresultat_acc = self._resolve_closing_account(
            '8800', 'l10n_no_skattemelding_aarsresultat_account_id',
        )
        journal = self._resolve_closing_journal()

        # Bygg bilag-linjer.
        #
        # For HVER P&L-konto: motpost saldo (D-saldo → K-linje, K-saldo → D-linje)
        # 8800: én linje som BALANSERER motpostene + én linje for overføring
        # 2050/2080: én linje som mottar overføringen
        #
        # Trial balance: alle motposter på P&L (sum_motpost) = `accounting_bal_sum`
        # motsatt fortegn. 8800-linje 1 må derfor være +accounting_bal_sum
        # (samme retning som original P&L-saldo) for å balansere mot motpostene.
        #
        # Eksempel overskudd (inntekt 100, accounting_bal_sum = -100, net_result = +100):
        #   3000: D 100 (motpost K-saldo)  → D-total: 100
        #   8800: K 100 (lukker P&L)        → K-total: 100  ✓ balansert
        #   8800: D 100 (overføring)        → D-total: 200
        #   2050: K 100 (mottak)            → K-total: 200  ✓ balansert
        move_lines = []

        # Linje per P&L-konto: motposter saldo (avrund per linje for floats)
        for aid, debit_total, credit_total, bal in pl_lines:
            bal_rounded = round(bal, 2)
            move_lines.append((0, 0, {
                'account_id': aid,
                'name': _("Lukke til årsresultat"),
                'debit': -bal_rounded if bal_rounded < 0 else 0.0,
                'credit': bal_rounded if bal_rounded > 0 else 0.0,
            }))

        # Linje 8800 #1: lukker P&L mot årsresultat-transit-kontoen.
        # 8800 må være MOTSATT side av motposterne for å balansere bilaget.
        #
        # Eksempel underskudd (Gamify): 7798 D-saldo 6500 → motpost K 6500
        # på 7798 → motposter er K-dominante → 8800 må være D for å balansere.
        # accounting_bal_sum = +6500 (D-dominans) → 8800 D abs(net_result).
        #
        # Eksempel overskudd: 3000 K-saldo 10000 → motpost D 10000 på 3000 →
        # motposter er D-dominante → 8800 må være K for å balansere.
        # accounting_bal_sum = -10000 (K-dominans) → 8800 K abs(net_result).
        net_result_abs = round(abs(net_result), 2)
        if net_result_abs < 0.005:
            raise UserError(_(
                "Årsresultat er 0 etter avrunding — avslutningsbilag ikke "
                "nødvendig."
            ))
        if accounting_bal_sum > 0:
            # Underskudd-tilfelle: motposter er K-dominante → 8800 D
            move_lines.append((0, 0, {
                'account_id': aarsresultat_acc.id,
                'name': _("Samlet resultat lukket fra P&L"),
                'debit': net_result_abs,
                'credit': 0.0,
            }))
        else:
            # Overskudd-tilfelle: motposter er D-dominante → 8800 K
            move_lines.append((0, 0, {
                'account_id': aarsresultat_acc.id,
                'name': _("Samlet resultat lukket fra P&L"),
                'debit': 0.0,
                'credit': net_result_abs,
            }))

        # Overføring til EK: positiv net_result = overskudd → 2050
        if net_result > 0:
            ek_acc = self._resolve_closing_account(
                '2050', 'l10n_no_skattemelding_annen_ek_account_id',
            )
            # 8800 motvekt for overføring (samme side som P&L-motposter)
            move_lines.append((0, 0, {
                'account_id': aarsresultat_acc.id,
                'name': _("Overføring av årets overskudd"),
                'debit': net_result_abs,
                'credit': 0.0,
            }))
            move_lines.append((0, 0, {
                'account_id': ek_acc.id,
                'name': _("Årets overskudd → annen egenkapital"),
                'debit': 0.0,
                'credit': net_result_abs,
            }))
        else:
            # Underskudd → 2080
            ek_acc = self._resolve_closing_account(
                '2080', 'l10n_no_skattemelding_udekket_tap_account_id',
            )
            move_lines.append((0, 0, {
                'account_id': ek_acc.id,
                'name': _("Årets underskudd → udekket tap"),
                'debit': net_result_abs,
                'credit': 0.0,
            }))
            move_lines.append((0, 0, {
                'account_id': aarsresultat_acc.id,
                'name': _("Overføring av årets underskudd"),
                'debit': 0.0,
                'credit': net_result_abs,
            }))

        # Sanity-check: trial balance må balansere på rad-nivå før create()
        # Odoo's egen account.move._check_balanced vil ellers kaste en
        # mindre informativ feilmelding.
        total_d = sum(line[2]['debit'] for line in move_lines)
        total_c = sum(line[2]['credit'] for line in move_lines)
        if abs(total_d - total_c) >= 0.005:
            raise UserError(_(
                "Bilag-bygging gav ubalansert resultat (D=%(d).2f, "
                "K=%(c).2f). Dette er en programfeil — kontakt support. "
                "Net result: %(n).2f, accounting bal sum: %(a).2f.",
                d=total_d, c=total_c,
                n=net_result, a=accounting_bal_sum,
            ))

        # Opprett bilag i draft. Bruker poster manuelt etter review.
        # Bruker with_company() for å sette env.company korrekt (påvirker
        # default-verdier i account.move.create — eks. currency_id).
        move = self.with_company(company).env['account.move'].create({
            'date': _date(year, 12, 31),
            'journal_id': journal.id,
            'ref': _(
                "Årsavslutning %(y)d — overføring av årets "
                "%(t)s til EK",
                y=year,
                t=_("overskudd") if net_result > 0 else _("underskudd"),
            ),
            'company_id': company.id,
            'l10n_no_skattemelding_closing': True,
            'line_ids': move_lines,
        })
        self.closing_entry_id = move.id

        _logger.info(
            "Skattemelding %s: opprettet avslutningsbilag %s (move_id=%s, "
            "net_result=%.2f, P&L-konti=%d, total_d=%.2f, total_c=%.2f)",
            self.id, move.name, move.id, net_result,
            len(pl_lines), total_d, total_c,
        )

        # Returner form-action på bilaget — bruker kan review og poste
        return {
            'type': 'ir.actions.act_window',
            'name': _("Avslutningsbilag (utkast — verifiser og poster)"),
            'res_model': 'account.move',
            'res_id': move.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_l10n_no_skattemelding_open_closing_entry(self):
        """Smart-button: åpne det knyttede avslutningsbilaget."""
        self.ensure_one()
        if not self.closing_entry_id:
            raise UserError(_("Ingen avslutningsbilag opprettet enda."))
        return {
            'type': 'ir.actions.act_window',
            'name': _("Avslutningsbilag"),
            'res_model': 'account.move',
            'res_id': self.closing_entry_id.id,
            'view_mode': 'form',
            'target': 'current',
        }

    # ----------------------------------------------------------------------
    # Konto-mapping setup-wizard
    # ----------------------------------------------------------------------

    def _get_accounts_requiring_mapping(self):
        """Returner account.account-recordset som trenger kodetype-mapping
        for XML-bygging.

        En konto "trenger mapping" hvis den vil bidra med data til ENTEN
        resultatregnskap-XML eller balanseregnskap-XML:

          • RESULTATREGNSKAP: kontoer med posted aktivitet i inntektsåret
            som IKKE kommer fra avslutningsbilag (resultatregnskap
            ekskluderer closing-bilag for å vise årets reelle bevegelser).

          • BALANSEREGNSKAP: kontoer med ikke-null kumulativ saldo pr
            31.12 inntektsår (inkl. closing-bilag — vi vil ha 2080/2050-
            saldoene fra avslutningen synlig).

        Bugfix 2026-05-16: tidligere returnerte funksjonen alle konti m.
        bevegelse uavhengig av kilde, så 8800 Årsresultat (kun aktivitet
        fra closing-bilag, netto saldo 0) ble feilaktig flagget som
        påkrevd-mapping. Etter closing nullstilles 8800 — den bidrar
        verken til resultat- eller balanse-XML, og skal derfor ikke
        flagges.

        Multi-company-safety: filtrer til konti som faktisk tilhører
        dette selskapet (account.company_ids inneholder company) for å
        unngå at delte account-records trekkes inn.
        """
        self.ensure_one()
        company = self.company_id
        year = self.inntektsaar
        date_from = _date(year, 1, 1)
        date_to = _date(year, 12, 31)
        AML = self.with_context(allowed_company_ids=company.ids).env[
            'account.move.line'
        ]

        # Resultat-bidrag: årets bevegelser, eksklusiv closing-bilag.
        # Matcher _check_mapping_coverage(cumulative=False) i XML-builderen.
        result_lines = AML.search([
            ('company_id', '=', company.id),
            ('date', '>=', date_from),
            ('date', '<=', date_to),
            ('parent_state', '=', 'posted'),
            ('account_id.code', '!=', False),
            ('move_id.l10n_no_skattemelding_closing', '=', False),
        ])
        result_account_ids = set(result_lines.account_id.ids)

        # Balanse-bidrag: kontoer m. ikke-null kumulativ saldo pr 31.12,
        # inklusiv closing-bilag. Matcher _check_mapping_coverage(
        # cumulative=True). Bruk read_group for å regne saldo per konto
        # uten å fetche enkelt-linjer.
        bal_groups = AML.sudo().read_group(
            [
                ('company_id', '=', company.id),
                ('date', '<=', date_to),
                ('parent_state', '=', 'posted'),
                ('account_id.code', '!=', False),
            ],
            ['account_id', 'balance:sum'],
            ['account_id'],
        )
        balance_account_ids = {
            g['account_id'][0]
            for g in bal_groups
            if abs(g['balance']) > 0.005  # toleranse for fp-støy
        }

        relevant_ids = result_account_ids | balance_account_ids
        if not relevant_ids:
            return self.env['account.account']
        accounts = self.env['account.account'].browse(relevant_ids)
        # Multi-company-safety: shared account-records skal ikke trekkes inn
        return accounts.filtered(lambda a: company in a.company_ids)

    def action_l10n_no_skattemelding_setup_mapping(self):
        """Auto-suggest + åpne mapping-review-liste i én flyt.

        Steg:
          1. Finn alle konti som har posted bevegelser t.o.m. 31.12
             inntektsår OG tilhører dette selskapet (cumulative — fanger
             også balansekontoer med kun historiske bevegelser)
          2. Kjør auto-suggest på de som mangler kodetype
             (NS 4102-prefix-match-algoritme, verified=False)
          3. Returner list-view med alle relevante konti — i editable mode
             slik at brukeren kan korrigere kodetype + bulk-verifisere
             uten å åpne hver konto enkeltvis

        NB: Steg 2 muterer DB (skriver auto-forslag som verified=False).
        Brukere som klikker knappen "bare for å se" får forslag lagret.
        Dette er akseptabelt fordi verified=False blokkerer XML-bygging
        helt til human review er gjort.

        Erstatter den klønete "Gå til Kontoplan → marker → kjør server-
        action"-flyten. Hele setup-en gjøres i én skjerm.
        """
        self.ensure_one()
        year = self.inntektsaar
        company = self.company_id

        accounts = self._get_accounts_requiring_mapping()
        if not accounts:
            raise UserError(_(
                "Ingen posted bilag funnet for %(c)s t.o.m. 31.12.%(y)d. "
                "Bokfør og post (action_post) fakturaer/bilag først, så "
                "kan vi sette opp mappingen.",
                c=company.name, y=year,
            ))

        # Kjør auto-suggest på de som mangler kodetype.
        # Eksisterende kobling overskrives ikke (manuell override beholdes).
        # NB: dette muterer DB — auto-forslag skrives med verified=False.
        accounts.action_l10n_no_skattemelding_auto_suggest(year)

        # Tell hvor mange som fortsatt mangler etter auto-suggest
        # (for å gi bruker forventnings-melding)
        still_unmapped = accounts.filtered(
            lambda a: not a.l10n_no_skattemelding_kodetype_id
        )

        # Returner editable list-view filtrert til relevante konti
        return {
            'type': 'ir.actions.act_window',
            'name': _(
                "Verifiser kontomapping (%(n)d konti, "
                "%(unmapped)d trenger manuelt valg)",
                n=len(accounts), unmapped=len(still_unmapped),
            ),
            'res_model': 'account.account',
            'view_mode': 'list',
            'views': [(
                self.env.ref(
                    'l10n_no_account_skattemelding.view_account_list_mapping_review'
                ).id,
                'list',
            )],
            'domain': [('id', 'in', accounts.ids)],
            'context': {
                'allowed_company_ids': company.ids,
                'create': False,
                'delete': False,
            },
            'target': 'current',
            'help': _(
                "Auto-forslag er fylt inn. Sjekk Skatteetaten-beskrivelsen "
                "for hver konto, korriger om nødvendig, og marker som "
                "Verifisert (multi-edit: marker flere rader og toggl "
                "kolonnen for bulk-godkjenning). Ferdig når alle er "
                "verifisert. %(unmapped)d konti mangler fortsatt forslag "
                "— du må manuelt velge kodetype for dem.",
                unmapped=len(still_unmapped),
            ),
        }
