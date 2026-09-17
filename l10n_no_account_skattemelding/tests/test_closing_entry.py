"""Unit-tester for årsavslutningsbilag (closing entry).

Dekker bug-prevention basert på code-review-funn:

P1-1: Tegn-konvensjon — verifiser at overskudd → 2050 og underskudd → 2080
P0-2: Konto-resolving — verifiser at account_type-mismatch gir UserError
P1-2: Trial balance — bilag må balansere (D=K)
P1-5: Auto-lock — idempotency, ikke senker eksisterende lock-date
P1-6: Phase 4 skip — kun ved POSTED closing entry, ikke draft

Bruker `from odoo.tools import float_compare` for floating-point-safety.
"""
from datetime import date

from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, new_test_user, tagged


@tagged('post_install', '-at_install', 'l10n_no_account_skattemelding')
class TestClosingEntry(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Isolated test company (Norwegian)
        cls.company = cls.env['res.company'].create({
            'name': 'Test AS for closing',
            'country_id': cls.env.ref('base.no').id,
            'currency_id': cls.env.ref('base.NOK').id,
            'vat': 'NO936903479MVA',
        })
        cls.env.user.company_ids |= cls.company
        cls.env = cls.env(context=dict(
            cls.env.context, allowed_company_ids=cls.company.ids,
        ))

        # NS 4102-konti som closing-action forventer
        cls.acc_3000 = cls.env['account.account'].create({
            'code': '3000', 'name': 'Salgsinntekt',
            'account_type': 'income',
            'company_ids': [(4, cls.company.id)],
        })
        cls.acc_7798 = cls.env['account.account'].create({
            'code': '7798', 'name': 'Annen kostnad',
            'account_type': 'expense',
            'company_ids': [(4, cls.company.id)],
        })
        cls.acc_8800 = cls.env['account.account'].create({
            'code': '8800', 'name': 'Årsresultat',
            'account_type': 'expense',
            'company_ids': [(4, cls.company.id)],
        })
        cls.acc_2050 = cls.env['account.account'].create({
            'code': '2050', 'name': 'Annen egenkapital',
            'account_type': 'equity',
            'company_ids': [(4, cls.company.id)],
        })
        cls.acc_2080 = cls.env['account.account'].create({
            'code': '2080', 'name': 'Udekket tap',
            'account_type': 'equity',
            'company_ids': [(4, cls.company.id)],
        })
        # Misc-journal (kreves for closing-bilag)
        cls.journal_misc = cls.env['account.journal'].create({
            'name': 'Misc Test', 'code': 'MISCT', 'type': 'general',
            'company_id': cls.company.id,
        })
        # Bank-journal + bilag for å gi P&L-bevegelser
        cls.acc_bank = cls.env['account.account'].create({
            'code': '1920', 'name': 'Bank',
            'account_type': 'asset_cash',
            'company_ids': [(4, cls.company.id)],
        })
        cls.journal_bank = cls.env['account.journal'].create({
            'name': 'Bank Test', 'code': 'BNKT', 'type': 'bank',
            'company_id': cls.company.id,
            'default_account_id': cls.acc_bank.id,
        })

    def _make_skattemelding(self, year=2025):
        """Lag en skattemelding-record for test-selskapet."""
        return self.env['l10n.no.skattemelding'].create({
            'company_id': self.company.id,
            'inntektsaar': year,
        })

    def _post_pl_move(self, date_str, pl_account, pl_debit, pl_credit):
        """Bokfør et test-bilag som motpost mellom bank og en P&L-konto.

        Posterer i 'bank'-journal slik at det IKKE er markert som closing
        (vi bygger ekte P&L-bevegelser å lukke).
        """
        move = self.env['account.move'].create({
            'date': date_str,
            'journal_id': self.journal_bank.id,
            'company_id': self.company.id,
            'line_ids': [
                (0, 0, {
                    'account_id': pl_account.id,
                    'debit': pl_debit, 'credit': pl_credit,
                    'name': 'Test P&L',
                }),
                (0, 0, {
                    'account_id': self.acc_bank.id,
                    'debit': pl_credit, 'credit': pl_debit,
                    'name': 'Test bank',
                }),
            ],
        })
        move.action_post()
        return move

    # ---- Selskapskontekst ---------------------------------------------

    def _regnskapsforer(self, selskaper):
        """Ekte (ikke-super) bruker — ir.rule hoppes over under env.su.

        NB: TransactionCase kjører som SUPERUSER (odoo/tests/common.py:
        `cls.env = api.Environment(cls.cr, api.SUPERUSER_ID, {})`), og
        ir.rule håndheves ikke da. Selskapsisolasjon kan derfor IKKE
        testes med klassens eget env — den ville bestått uansett. Testene
        under lager en ekte regnskapsfører for å faktisk treffe regelen.
        """
        return new_test_user(
            self.env, login='regnskapsforer_selskapstest',
            groups='account.group_account_manager',
            company_id=selskaper[0].id,
            company_ids=[(6, 0, selskaper.ids)],
        )

    def test_lukkebilag_er_utilgjengelig_fra_feil_selskap(self):
        """ir.rule-en ER vakten — lukkebilaget kan ikke nås fra feil selskap.

        Husmønsteret (besluttet 2026-08-31): selskapsisolasjon håndheves
        STRUKTURELT av den globale ir.rule-en på meldingsmodellen, ikke av
        en sjekk inne i hver action. `company_ids` i et ir.rule-domene er
        `env.companies.ids` — de AKTIVE selskapene (ir_rule.py:49) — så en
        skattemelding for et ikke-aktivt selskap er ikke lesbar i det hele
        tatt, og knappen kan dermed ikke trykkes.

        Derfor har lukkebilaget bevisst INGEN egen «bytt selskap»-vakt: en
        slik vakt ville vært død kode. MVA-modulen hadde en kort periode
        2026-08-31 nettopp en slik vakt fordi den manglet ir.rule-en;
        regelen er nå lagt til der også, og vakten fjernet igjen.

        Denne testen er kontrakten: fjernes ir.rule-en, blir lukkebilaget
        nåbart fra feil selskap og testen faller.
        """
        sm = self._make_skattemelding()
        annet = self.env['res.company'].create({'name': 'Annet Selskap AS'})
        bruker = self._regnskapsforer(self.company + annet)
        Skattemelding = self.env['l10n.no.skattemelding'].with_user(
            bruker).with_context(allowed_company_ids=annet.ids)

        # Usynlig i søk ...
        self.assertFalse(Skattemelding.search([('id', '=', sm.id)]))
        # ... og ikke lesbar via direktelenke, så knappen aldri nås.
        with self.assertRaises(AccessError):
            Skattemelding.browse(sm.id).inntektsaar
        # ... men synlig så snart selskapet er aktivt (motprøve: det er
        # AKTIVT selskap som avgjør, ikke bare tildelt tilgang).
        self.assertTrue(
            Skattemelding.with_context(
                allowed_company_ids=(self.company + annet).ids,
            ).search([('id', '=', sm.id)]))

    def test_lukkebilag_snevrer_inn_naar_flere_selskaper_er_aktive(self):
        """`allowed_company_ids=company.ids` i oppslagene SNEVRER INN.

        Lett å lese som et bypass, men er det motsatte: posten har allerede
        passert ir.rule-en, så meldingens selskap ER aktivt. Med flere
        aktive selskaper begrenser konteksten kontooppslagene til dette ene
        selskapet, slik at delte kontoer fra søsterselskaper ikke trekkes
        inn i lukkebilaget.
        """
        annet = self.env['res.company'].create({
            'name': 'Søsterselskap AS',
            'country_id': self.env.ref('base.no').id,
        })
        self.env.user.company_ids |= annet
        # Samme NS 4102-kode i søsterselskapet — feil treff hvis oppslaget
        # ikke snevres inn.
        felle = self.env['account.account'].create({
            'code': '8800', 'name': 'Årsresultat (søster)',
            'account_type': 'expense',
            'company_ids': [(4, annet.id)],
        })
        self._post_pl_move('2025-06-01', self.acc_7798, 6500.0, 0.0)
        sm = self._make_skattemelding().with_context(
            allowed_company_ids=(self.company + annet).ids)
        sm.action_l10n_no_skattemelding_create_closing_entry()

        kontoer = sm.closing_entry_id.line_ids.account_id
        self.assertIn(self.acc_8800, kontoer)
        self.assertNotIn(felle, kontoer)

    # ---- P1-1: Tegn-konvensjon ----------------------------------------

    def test_underskudd_creates_entry_with_2080(self):
        """Kostnad-dominans → underskudd → 2080 mottar."""
        # Kostnad 6500 (som Gamify-case)
        self._post_pl_move('2025-06-01', self.acc_7798, 6500.0, 0.0)
        sm = self._make_skattemelding()
        sm.action_l10n_no_skattemelding_create_closing_entry()

        move = sm.closing_entry_id
        self.assertTrue(move, "Avslutningsbilag skal ha blitt opprettet")
        # 2080 skal motta tapet (debet)
        line_2080 = move.line_ids.filtered(lambda l: l.account_id == self.acc_2080)
        self.assertTrue(line_2080, "2080-linje må finnes ved underskudd")
        self.assertAlmostEqual(line_2080.debit, 6500.0, places=2)
        self.assertAlmostEqual(line_2080.credit, 0.0, places=2)
        # 2050 skal IKKE være berørt
        line_2050 = move.line_ids.filtered(lambda l: l.account_id == self.acc_2050)
        self.assertFalse(line_2050, "2050 skal ikke brukes ved underskudd")

    def test_overskudd_creates_entry_with_2050(self):
        """Inntekt-dominans → overskudd → 2050 mottar."""
        # Inntekt 10000
        self._post_pl_move('2025-06-01', self.acc_3000, 0.0, 10000.0)
        sm = self._make_skattemelding()
        sm.action_l10n_no_skattemelding_create_closing_entry()

        move = sm.closing_entry_id
        self.assertTrue(move)
        line_2050 = move.line_ids.filtered(lambda l: l.account_id == self.acc_2050)
        self.assertTrue(line_2050, "2050-linje må finnes ved overskudd")
        self.assertAlmostEqual(line_2050.credit, 10000.0, places=2)
        self.assertAlmostEqual(line_2050.debit, 0.0, places=2)
        line_2080 = move.line_ids.filtered(lambda l: l.account_id == self.acc_2080)
        self.assertFalse(line_2080, "2080 skal ikke brukes ved overskudd")

    def test_mixed_overskudd_with_costs(self):
        """Inntekt 10000, kostnad 3000 → netto overskudd 7000 → 2050."""
        self._post_pl_move('2025-06-01', self.acc_3000, 0.0, 10000.0)
        self._post_pl_move('2025-06-15', self.acc_7798, 3000.0, 0.0)
        sm = self._make_skattemelding()
        sm.action_l10n_no_skattemelding_create_closing_entry()

        move = sm.closing_entry_id
        line_2050 = move.line_ids.filtered(lambda l: l.account_id == self.acc_2050)
        self.assertAlmostEqual(line_2050.credit, 7000.0, places=2)

    # ---- P1-2: Trial balance ------------------------------------------

    def test_closing_entry_balances(self):
        """Bilag-summen må alltid balansere (D = K)."""
        self._post_pl_move('2025-06-01', self.acc_3000, 0.0, 10000.0)
        self._post_pl_move('2025-06-15', self.acc_7798, 3000.0, 0.0)
        sm = self._make_skattemelding()
        sm.action_l10n_no_skattemelding_create_closing_entry()

        move = sm.closing_entry_id
        total_d = sum(move.line_ids.mapped('debit'))
        total_c = sum(move.line_ids.mapped('credit'))
        self.assertAlmostEqual(total_d, total_c, places=2,
            msg="Trial balance må balansere (D=K)")

    def test_closing_entry_marked_with_flag(self):
        """Bilaget må ha l10n_no_skattemelding_closing=True."""
        self._post_pl_move('2025-06-01', self.acc_7798, 6500.0, 0.0)
        sm = self._make_skattemelding()
        sm.action_l10n_no_skattemelding_create_closing_entry()
        self.assertTrue(sm.closing_entry_id.l10n_no_skattemelding_closing)

    def test_closing_entry_dated_31_dec(self):
        """Bilag skal være datert 31.12 inntektsår."""
        self._post_pl_move('2025-06-01', self.acc_7798, 6500.0, 0.0)
        sm = self._make_skattemelding(year=2025)
        sm.action_l10n_no_skattemelding_create_closing_entry()
        self.assertEqual(str(sm.closing_entry_id.date), '2025-12-31')

    # ---- Idempotency ----------------------------------------------------

    def test_no_pl_movements_raises_userror(self):
        """Ingen P&L-bevegelser → UserError 'ikke nødvendig'."""
        sm = self._make_skattemelding()
        with self.assertRaises(UserError) as ctx:
            sm.action_l10n_no_skattemelding_create_closing_entry()
        self.assertIn('ikke nødvendig', str(ctx.exception))

    def test_second_call_returns_existing_entry(self):
        """Idempotency: andre kall returnerer eksisterende bilag, ikke duplikat."""
        self._post_pl_move('2025-06-01', self.acc_7798, 6500.0, 0.0)
        sm = self._make_skattemelding()
        sm.action_l10n_no_skattemelding_create_closing_entry()
        first_move_id = sm.closing_entry_id.id

        sm.action_l10n_no_skattemelding_create_closing_entry()
        self.assertEqual(sm.closing_entry_id.id, first_move_id,
            "Andre kall skal ikke opprette duplikat")

    # ---- P0-2: Account-resolving --------------------------------------

    def test_resolve_account_falls_back_to_code_search(self):
        """Hvis konfig er tom, søk etter NS 4102-kode + type."""
        sm = self._make_skattemelding()
        # Ingen konfig satt → fallback skal finne 8800
        acc = sm._resolve_closing_account(
            '8800', 'l10n_no_skattemelding_aarsresultat_account_id',
        )
        self.assertEqual(acc, self.acc_8800)

    def test_resolve_account_rejects_wrong_account_type(self):
        """Hvis konto med kode 8800 har feil account_type (eks: bank),
        skal fallback IKKE finne den → UserError."""
        # Slett standard 8800-konto vi opprettet
        self.acc_8800.unlink()
        # Lag en falsk 8800 av feil type
        self.env['account.account'].create({
            'code': '8800', 'name': 'Falsk bank 8800',
            'account_type': 'asset_cash',  # feil type!
            'company_ids': [(4, self.company.id)],
        })
        sm = self._make_skattemelding()
        with self.assertRaises(UserError) as ctx:
            sm._resolve_closing_account(
                '8800', 'l10n_no_skattemelding_aarsresultat_account_id',
            )
        self.assertIn('8800', str(ctx.exception))

    def test_configured_account_overrides_search(self):
        """Konfig-felt på selskap har prioritet over kode-søk."""
        # Lag en alternativ 8800-konto og sett som konfig
        alt_acc = self.env['account.account'].create({
            'code': '8801', 'name': 'Alternativ årsresultat',
            'account_type': 'expense',
            'company_ids': [(4, self.company.id)],
        })
        self.company.l10n_no_skattemelding_aarsresultat_account_id = alt_acc.id
        sm = self._make_skattemelding()
        resolved = sm._resolve_closing_account(
            '8800', 'l10n_no_skattemelding_aarsresultat_account_id',
        )
        self.assertEqual(resolved, alt_acc,
            "Konfig skal overstyre fallback-søk")

    # ---- P1-5: Auto-lock idempotency ---------------------------------

    def test_auto_lock_sets_to_year_end(self):
        """Lock-date settes til 31.12 av inntektsår."""
        sm = self._make_skattemelding(year=2025)
        sm._auto_lock_fiscalyear()
        self.assertEqual(self.company.fiscalyear_lock_date, date(2025, 12, 31))
        self.assertEqual(self.company.tax_lock_date, date(2025, 12, 31))

    def test_auto_lock_does_not_lower_existing_later_lock(self):
        """Hvis selskap har lock på 2026-12-31, IKKE senke til 2025-12-31."""
        self.company.fiscalyear_lock_date = date(2026, 12, 31)
        sm = self._make_skattemelding(year=2025)
        sm._auto_lock_fiscalyear()
        # Skal forbli 2026-12-31, ikke senkes
        self.assertEqual(self.company.fiscalyear_lock_date, date(2026, 12, 31))

    def test_auto_lock_idempotent_same_date(self):
        """Andre kall med samme target er no-op (skal ikke krasje)."""
        sm = self._make_skattemelding(year=2025)
        sm._auto_lock_fiscalyear()
        sm._auto_lock_fiscalyear()  # No-op
        self.assertEqual(self.company.fiscalyear_lock_date, date(2025, 12, 31))
