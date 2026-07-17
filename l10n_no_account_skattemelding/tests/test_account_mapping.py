"""Unit-tester for account.account → skattemelding-kodetype-mapping.

Dekker:
  - _l10n_no_skattemelding_suggest_kodetype: prefix-match-algoritme
  - action_l10n_no_skattemelding_auto_suggest: massevask, respekterer
    manuell override
"""
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install', 'l10n_no_account_skattemelding')
class TestAccountMapping(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.kt_model = cls.env['l10n.no.skattemelding.kodetype']
        cls.account_model = cls.env['account.account']

        # Lag test-kodetyper m. isolert test-år 9999 — unngår kollisjon
        # med data-filen (som har 2024+2025+2026).
        cls.TEST_YEAR = 9999
        cls.kt_1000 = cls.kt_model.create({
            'code': '1000', 'name': 'Test utvikling',
            'inntektsaar': cls.TEST_YEAR,
            'underkodeliste': 'balanseverdiForAnleggsmiddel',
        })
        cls.kt_3000 = cls.kt_model.create({
            'code': '3000', 'name': 'Test salgsinntekt',
            'inntektsaar': cls.TEST_YEAR,
            'underkodeliste': 'salgsinntekt',
        })
        cls.kt_3020 = cls.kt_model.create({
            'code': '3020', 'name': 'Test tjeneste-inntekt',
            'inntektsaar': cls.TEST_YEAR,
            'underkodeliste': 'salgsinntekt',
        })
        cls.kt_6000 = cls.kt_model.create({
            'code': '6000', 'name': 'Test avskrivning',
            'inntektsaar': cls.TEST_YEAR,
            'underkodeliste': 'annenDriftskostnad',
        })

    def _make_account(self, code, name='Test', account_type='expense'):
        return self.account_model.create({
            'code': code,
            'name': name,
            'account_type': account_type,
        })

    # ---- Prefix-match-algoritme -----------------------------------------

    def test_suggest_exact_match(self):
        """Hvis kontens code == kodetype.code → eksakt match."""
        acc = self._make_account('3000', 'Salgsinntekt')
        result = acc._l10n_no_skattemelding_suggest_kodetype(self.TEST_YEAR)
        self.assertEqual(result, self.kt_3000)

    def test_suggest_rounds_down_to_nearest(self):
        """Konto 3015 — ingen eksakt match, runder ned til 3000."""
        acc = self._make_account('3015', 'Spesial salgsinntekt')
        result = acc._l10n_no_skattemelding_suggest_kodetype(self.TEST_YEAR)
        self.assertEqual(result, self.kt_3000)

    def test_suggest_picks_higher_match_when_available(self):
        """Konto 3025 — runder ned til 3020 (ikke 3000).

        Algoritmen velger HØYESTE kodetype.code ≤ konto.code.
        """
        acc = self._make_account('3025', 'Tjeneste-inntekt utenfor')
        result = acc._l10n_no_skattemelding_suggest_kodetype(self.TEST_YEAR)
        self.assertEqual(result, self.kt_3020)

    def test_suggest_respects_ns_series_boundary(self):
        """Konto 2999 (gjeld) skal IKKE foreslå 1000 (utvikling).

        Algoritmen krever samme første-siffer-serie.
        """
        # Det finnes ingen 2xxx-kodetype i fixture for TEST_YEAR
        acc = self._make_account('2999', 'Annen kortsiktig gjeld')
        result = acc._l10n_no_skattemelding_suggest_kodetype(self.TEST_YEAR)
        self.assertFalse(result)

    def test_suggest_returns_empty_for_non_numeric(self):
        """Kontoer m. ikke-numerisk code → ingen forslag."""
        # Lag konto m. False code (e.g., ikke satt enda)
        acc = self.account_model.new({'name': 'Test', 'account_type': 'expense'})
        result = acc._l10n_no_skattemelding_suggest_kodetype(self.TEST_YEAR)
        self.assertFalse(result)

    def test_suggest_returns_empty_for_unknown_year(self):
        """År uten kodeliste-data → ingen forslag (ikke crash)."""
        acc = self._make_account('3000', 'Salgsinntekt')
        # Bruk år 8888 som garantert ikke har data
        result = acc._l10n_no_skattemelding_suggest_kodetype(8888)
        self.assertFalse(result)

    def test_suggest_prefers_decade_marker_over_specialized_subcode(self):
        """Skatteetaten kodeliste har spesialiserte sub-koder (3001-3008
        for petroleum) ved siden av generelle decade-markere (3000, 3100).

        Account 3020 'Salg av tjenester' for ikke-petroleum-foretak skal
        mappe til 3000 (decade-markør), IKKE 3008 (siste petroleum-subkode
        som mekanisk er nærmeste ≤ 3020).

        Bug funnet 2026-05-11 i E2E mot InGrid: 3020 (tjeneste-salg, 2.2M)
        ble mappet til 3008 (petroleum) — vi rapporterte feil kategori.
        """
        # Fjern kt_3020 fra fixture for denne testen — vi vil teste
        # scenarioet hvor det IKKE finnes eksakt match for 3020 og
        # algoritmen må velge mellom 3000 (decade) og 3001 (sub).
        # TransactionCase ruller tilbake unlink etter testen.
        self.kt_3020.unlink()
        # Sett opp Skatteetaten-like fixture: 3000 (decade) + 3001 (sub).
        kt_3001 = self.kt_model.create({
            'code': '3001', 'name': 'Test petroleum-subkode',
            'inntektsaar': self.TEST_YEAR,
            'underkodeliste': 'salgsinntekt',
        })
        # Konto 3020 — algoritmen skal velge 3000 (hundred-markør),
        # ikke 3001 (mekanisk høyeste ≤ 3020).
        acc = self._make_account('3020', 'Salg tjenester')
        result = acc._l10n_no_skattemelding_suggest_kodetype(self.TEST_YEAR)
        self.assertEqual(
            result, self.kt_3000,
            "3020 skal mappe til 3000 (decade-markør), ikke 3001 (subkode)",
        )

    def test_suggest_decade_match_when_exists(self):
        """3025 skal mappe til 3020 hvis 3020 finnes som decade-bucket."""
        # 3020 finnes i fixture (self.kt_3020)
        acc = self._make_account('3025', 'Variant av 3020')
        result = acc._l10n_no_skattemelding_suggest_kodetype(self.TEST_YEAR)
        self.assertEqual(
            result, self.kt_3020,
            "3025 → 3020 via decade-rounding (3025 // 10 * 10 = 3020)",
        )

    def test_suggest_fallback_to_higher_when_no_lower_exists(self):
        """Hvis Skatteetatens kodeliste starter HØYERE enn kontoens code,
        runder vi OPP til laveste kode i samme serie.

        Real-world: Odoo l10n_no har konto 4000 (Purchase), men Skatteetatens
        kodeliste starter på 4001 ('Letekostnader'). Algoritmen må gi forslag
        4001 i stedet for None.
        """
        # Setup: kun 6000 som 6xxx-kode i fixture (allerede der via kt_6000).
        # Lag konto 5000 som har ingen <=5000 i fixture, må runde OPP til 6000?
        # Nei — 5000 og 6000 er ulike serier. Test må være i SAMME serie.
        # Sett opp: lag 4500-kode (ingen 4xxx ≤ 4500 utenfor fixture).
        kt_4500 = self.kt_model.create({
            'code': '4500', 'name': 'Test varekostnad høy',
            'inntektsaar': self.TEST_YEAR,
            'underkodeliste': 'varekostnad',
        })
        # Konto 4000 — ingen 4xxx ≤ 4000 i fixture. Skal runde OPP til 4500.
        acc = self._make_account('4000', 'Konto under første kode')
        result = acc._l10n_no_skattemelding_suggest_kodetype(self.TEST_YEAR)
        self.assertEqual(
            result, kt_4500,
            "Konto 4000 skal runde OPP til 4500 når ingen 4xxx ≤4000 finnes",
        )

    # ---- Auto-suggest action --------------------------------------------

    def test_action_fills_unmapped_accounts(self):
        """Action fyller ut mapping for kontoer som mangler.

        Bruker TEST_YEAR=9999 m. isolerte fixture-kodetyper for determinisme
        (Skatteetatens ekte 2025-kodeliste har flere 3xxx-koder som ville
        gitt eksakt match istedenfor prefix-rounding).
        """
        acc1 = self._make_account('3001', 'Salgsinntekt Norge')
        acc2 = self._make_account('3010', 'Salgsinntekt EU')
        (acc1 + acc2).action_l10n_no_skattemelding_auto_suggest(
            inntektsaar=self.TEST_YEAR,
        )
        # Begge bør ha fått mapping nå — 3001 og 3010 runder ned til 3000
        # (fixture har bare 3000 og 3020 for TEST_YEAR)
        self.assertTrue(acc1.l10n_no_skattemelding_kodetype_id)
        self.assertTrue(acc2.l10n_no_skattemelding_kodetype_id)
        self.assertEqual(
            acc1.l10n_no_skattemelding_kodetype_id.code, '3000',
            "3001 skal runde ned til 3000 (høyeste ≤ 3001 i fixture)",
        )
        self.assertEqual(
            acc2.l10n_no_skattemelding_kodetype_id.code, '3000',
            "3010 skal runde ned til 3000 (3020 > 3010 så velges ikke)",
        )

    # ---- Verifikasjons-flagg (Sikring #1) -------------------------------

    def test_auto_suggest_sets_verified_false(self):
        """Auto-suggest skal alltid sette verified=False — bruker må
        eksplisitt godkjenne."""
        acc = self._make_account('3001', 'Salg variant')
        acc.action_l10n_no_skattemelding_auto_suggest(
            inntektsaar=self.TEST_YEAR,
        )
        self.assertTrue(
            acc.l10n_no_skattemelding_kodetype_id,
            "Auto-suggest skal sette kodetype",
        )
        self.assertFalse(
            acc.l10n_no_skattemelding_kodetype_verified,
            "Auto-suggest skal IKKE sette verified=True",
        )

    def test_verify_action_sets_verified_true(self):
        """Verifikasjons-action skal sette flagget."""
        acc = self._make_account('3000', 'Salg')
        acc.l10n_no_skattemelding_kodetype_id = self.kt_3000
        self.assertFalse(acc.l10n_no_skattemelding_kodetype_verified)
        acc.action_l10n_no_skattemelding_verify_mapping()
        self.assertTrue(acc.l10n_no_skattemelding_kodetype_verified)

    def test_unverify_action_clears_flag(self):
        """Unverify-action skal nulle flagget for re-vurdering."""
        acc = self._make_account('3000', 'Salg')
        acc.write({
            'l10n_no_skattemelding_kodetype_id': self.kt_3000.id,
            'l10n_no_skattemelding_kodetype_verified': True,
        })
        acc.action_l10n_no_skattemelding_unverify_mapping()
        self.assertFalse(acc.l10n_no_skattemelding_kodetype_verified)

    def test_action_respects_manual_override(self):
        """Action skal IKKE overskrive eksisterende mapping."""
        acc = self._make_account('3000', 'Salgsinntekt')
        # Sett manuell mapping til 6000 (urealistisk men test-bart)
        acc.l10n_no_skattemelding_kodetype_id = self.kt_6000
        acc.action_l10n_no_skattemelding_auto_suggest()
        # Forventer at 6000 fortsatt er der, ikke overskrevet til 3000
        self.assertEqual(
            acc.l10n_no_skattemelding_kodetype_id, self.kt_6000,
            "Manuell override skal ikke overskrives av auto-suggest",
        )

    # ---- Coverage-validering ---------------------------------------------

    def test_coverage_empty_when_all_mapped(self):
        """Hvis alle aktive kontoer har mapping → coverage OK (tom recordset)."""
        # Lag et company + skattemelding-record for fresh isolated test
        company = self.env['res.company'].create({
            'name': 'CoverageTest AS',
            'country_id': self.env.ref('base.no').id,
            'currency_id': self.env.ref('base.NOK').id,
            'vat': 'NO936903479MVA',
        })
        self.env.user.company_ids |= company
        sm = self.env['l10n.no.skattemelding'].create({
            'company_id': company.id,
            'inntektsaar': 2025,
            'partsnummer': '1234567890',
        })
        xml_svc = self.env['l10n.no.skattemelding.xml.service']
        # Ingen move-lines → ingen kontoer flagget for review
        result = xml_svc._check_mapping_coverage(sm)
        self.assertFalse(
            result['unmapped'],
            "Tomt regnskap → ingen unmapped-kontoer",
        )
        self.assertFalse(
            result['unverified'],
            "Tomt regnskap → ingen unverified-kontoer",
        )
        self.assertFalse(
            result['incompatible_regnskapsplikt'],
            "Tomt regnskap → ingen incompatible-kontoer",
        )

    # ---- Regnskapsplikt-kompatibilitet (Phase 3b fix) -------------------

    def test_suggest_skips_kodetype_incompatible_with_full_regnskapsplikt(self):
        """Auto-suggest skal hoppe over kodetyper flagget for begrenset
        regnskapsplikt når selskapet er fullRegnskapsplikt.

        Bug funnet 2026-05-11 i E2E mot Eristo: konto 1295 'Driftsmidler
        som avskrives lineært' ble auto-foreslått til en konto 1295 i
        Odoo, men kode 1295 gjelder kun for begrenset regnskapsplikt.
        Innsendingen ble blokkert av N_FEIL_ANLEGGSMIDDELTYPE.
        """
        # Lag kodetype 1295 markert som ikke-gyldig for full regnskapsplikt
        kt_1295 = self.kt_model.create({
            'code': '1295', 'name': 'Test driftsmidler lineær',
            'inntektsaar': self.TEST_YEAR,
            'underkodeliste': 'balanseverdiForAnleggsmiddel',
            'gjelder_full_regnskapsplikt': False,
        })
        # Selskapet er fullRegnskapsplikt (default)
        acc = self._make_account('1295', 'Driftsmidler')
        result = acc._l10n_no_skattemelding_suggest_kodetype(self.TEST_YEAR)
        self.assertNotEqual(
            result, kt_1295,
            "Inkompatibel kode skal ikke foreslås for full regnskapsplikt",
        )
        # Bør fortsatt foreslå NOE — fixture har kt_1000 i samme serie
        # som tjener som fallback (eller tom hvis ingen kompatibel finnes)
        if result:
            self.assertTrue(
                result.gjelder_full_regnskapsplikt,
                "Foreslått kodetype må være kompatibel med full regnskapsplikt",
            )

    def test_coverage_flags_incompatible_regnskapsplikt(self):
        """Kontoer mappet til kodetyper flagget for begrenset regnskapsplikt
        skal samles i 'incompatible_regnskapsplikt'-bøtta når selskapet er
        fullRegnskapsplikt, slik at build_naeringsspesifikasjon_xml kan
        blokkere innsending med klar feilmelding."""
        # Bruk eksisterende kodetype 1295/2025 fra seed-data og overstyr
        # flagget for testen. (Tidligere create kollidert med
        # unique constraint på code+inntektsaar — fikset i P3 #11.)
        kt_bad = self.kt_model.search([
            ('code', '=', '1295'),
            ('inntektsaar', '=', 2025),
        ], limit=1)
        if kt_bad:
            kt_bad.write({'gjelder_full_regnskapsplikt': False})
        else:
            kt_bad = self.kt_model.create({
                'code': '1295', 'name': 'Test inkompatibel',
                'inntektsaar': 2025,
                'underkodeliste': 'balanseverdiForAnleggsmiddel',
                'gjelder_full_regnskapsplikt': False,
            })
        company = self.env['res.company'].create({
            'name': 'IncompatTest AS',
            'country_id': self.env.ref('base.no').id,
            'currency_id': self.env.ref('base.NOK').id,
            'vat': 'NO936903479MVA',
            'l10n_no_skattemelding_regnskapspliktstype': 'fullRegnskapsplikt',
        })
        self.env.user.company_ids |= company

        # Konto m. mapping til inkompatibel kode + posted move-line for året
        acc = self.account_model.with_company(company).create({
            'code': '1295', 'name': 'Driftsmidler lineær',
            'account_type': 'asset_fixed',
            'l10n_no_skattemelding_kodetype_id': kt_bad.id,
            'l10n_no_skattemelding_kodetype_verified': True,
        })
        # Trenger en motpostkonto for å lage balanseført move
        offset = self.account_model.with_company(company).create({
            'code': '2050', 'name': 'Egenkapital',
            'account_type': 'equity',
        })
        journal = self.env['account.journal'].with_company(company).create({
            'name': 'Misc', 'type': 'general', 'code': 'MISC',
        })
        move = self.env['account.move'].with_company(company).create({
            'journal_id': journal.id,
            'date': '2025-06-01',
            'line_ids': [
                (0, 0, {'account_id': acc.id, 'debit': 100, 'credit': 0}),
                (0, 0, {'account_id': offset.id, 'debit': 0, 'credit': 100}),
            ],
        })
        move.action_post()

        sm = self.env['l10n.no.skattemelding'].with_company(company).create({
            'company_id': company.id,
            'inntektsaar': 2025,
            'partsnummer': '1234567890',
        })
        xml_svc = self.env['l10n.no.skattemelding.xml.service']
        result = xml_svc._check_mapping_coverage(sm)
        self.assertIn(
            acc, result['incompatible_regnskapsplikt'],
            "Konto mappet til kode flagget incompat skal være i incompat-bøtta",
        )

    # ---- Coverage-bug 2026-05-16: closing-only-active konti ------------

    def test_coverage_excludes_account_with_only_closing_activity(self):
        """Konti som KUN har aktivitet fra avslutningsbilag og nullstilles
        skal IKKE flagges som påkrevd mapping.

        Bug funnet 2026-05-16 i Roret-test: 8800 Årsresultat fikk
        aktivitet (D 14496 og K 14496) fra avslutningsbilaget, blei
        netto null. Coverage-sjekken brukte tidligere en SQL som
        hentet ALLE konti m. bevegelse uavhengig av kilde — så 8800 ble
        feilaktig flagget som påkrevd mapping selv om XML-aggregeringen
        filtrerer den bort.

        Etter fix: konti m. kun closing-aktivitet OG null kumulativ
        saldo skal være utenfor scope.
        """
        company = self.env['res.company'].create({
            'name': 'ClosingOnlyTest AS',
            'country_id': self.env.ref('base.no').id,
            'currency_id': self.env.ref('base.NOK').id,
            'vat': 'NO936903479MVA',
            'l10n_no_skattemelding_regnskapspliktstype': 'fullRegnskapsplikt',
        })
        self.env.user.company_ids |= company

        acc_8800 = self.account_model.with_company(company).create({
            'code': '8800', 'name': 'Årsresultat',
            'account_type': 'expense',
        })
        offset = self.account_model.with_company(company).create({
            'code': '2080', 'name': 'Udekket tap',
            'account_type': 'equity',
        })
        # Motpost for å balansere closing-bilaget der 8800 nullstilles
        # mens 2080 ender med ikke-null saldo (simulerer underskudd-flow).
        bank = self.account_model.with_company(company).create({
            'code': '1921', 'name': 'Bank',
            'account_type': 'asset_cash',
        })
        journal = self.env['account.journal'].with_company(company).create({
            'name': 'Misc', 'type': 'general', 'code': 'MISC',
        })
        # Closing-bilag skal etterligne underskudd-lukking:
        #   - 8800 nullstilles (D 100 + K 100 = 0)
        #   - 2080 får D 100 (udekket tap til balansen)
        #   - bank motposter for å balansere bilaget
        move = self.env['account.move'].with_company(company).create({
            'journal_id': journal.id,
            'date': '2025-12-31',
            'l10n_no_skattemelding_closing': True,  # <- nøkkelen
            'line_ids': [
                (0, 0, {'account_id': acc_8800.id, 'debit': 100, 'credit': 0}),
                (0, 0, {'account_id': acc_8800.id, 'debit': 0, 'credit': 100}),
                (0, 0, {'account_id': offset.id, 'debit': 100, 'credit': 0}),
                (0, 0, {'account_id': bank.id, 'debit': 0, 'credit': 100}),
            ],
        })
        move.action_post()

        sm = self.env['l10n.no.skattemelding'].with_company(company).create({
            'company_id': company.id,
            'inntektsaar': 2025,
            'partsnummer': '1234567890',
        })
        # Hent active accounts via helper
        active = sm._get_accounts_requiring_mapping()
        self.assertNotIn(
            acc_8800, active,
            "8800 med kun closing-aktivitet OG null saldo skal være utenfor scope",
        )
        # 2080 har ikke-null saldo etter closing → SKAL fortsatt være i scope
        self.assertIn(
            offset, active,
            "2080 m. ikke-null kumulativ saldo skal være i scope (vises i balanse-XML)",
        )

    def test_coverage_includes_account_with_regular_and_closing_activity(self):
        """Konto m. både ordinær aktivitet og closing-aktivitet skal
        fortsatt være i scope. F.eks. 3200 Salgsinntekt har ordinær K
        -2117 fra fakturering + closing D 2117 = netto 0, men den
        ordinære aktiviteten er det som rapporteres i resultat-XML.
        """
        company = self.env['res.company'].create({
            'name': 'MixedActivityTest AS',
            'country_id': self.env.ref('base.no').id,
            'currency_id': self.env.ref('base.NOK').id,
            'vat': 'NO936903479MVA',
            'l10n_no_skattemelding_regnskapspliktstype': 'fullRegnskapsplikt',
        })
        self.env.user.company_ids |= company

        acc_sales = self.account_model.with_company(company).create({
            'code': '3200', 'name': 'Salg',
            'account_type': 'income',
        })
        offset = self.account_model.with_company(company).create({
            'code': '2050', 'name': 'Annen EK',
            'account_type': 'equity',
        })
        bank = self.account_model.with_company(company).create({
            'code': '1921', 'name': 'Bank',
            'account_type': 'asset_cash',
        })
        journal = self.env['account.journal'].with_company(company).create({
            'name': 'Misc', 'type': 'general', 'code': 'MISC',
        })
        # Ordinær fakturering
        regular = self.env['account.move'].with_company(company).create({
            'journal_id': journal.id,
            'date': '2025-06-01',
            'line_ids': [
                (0, 0, {'account_id': bank.id, 'debit': 100, 'credit': 0}),
                (0, 0, {'account_id': acc_sales.id, 'debit': 0, 'credit': 100}),
            ],
        })
        regular.action_post()
        # Closing-bilag lukker 3200
        closing = self.env['account.move'].with_company(company).create({
            'journal_id': journal.id,
            'date': '2025-12-31',
            'l10n_no_skattemelding_closing': True,
            'line_ids': [
                (0, 0, {'account_id': acc_sales.id, 'debit': 100, 'credit': 0}),
                (0, 0, {'account_id': offset.id, 'debit': 0, 'credit': 100}),
            ],
        })
        closing.action_post()

        sm = self.env['l10n.no.skattemelding'].with_company(company).create({
            'company_id': company.id,
            'inntektsaar': 2025,
            'partsnummer': '1234567890',
        })
        active = sm._get_accounts_requiring_mapping()
        # 3200 har ordinær aktivitet (selv om closing nullstiller saldoen)
        self.assertIn(
            acc_sales, active,
            "3200 m. ordinær fakturering skal være i scope (resultat-XML)",
        )
