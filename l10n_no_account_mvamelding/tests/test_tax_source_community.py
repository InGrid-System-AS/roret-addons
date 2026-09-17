"""Integrasjonstester for Community-tallkilden + oppgjør.

Verifiserer at den egne tag-summeringen (_tax_report_code_values) gir
samme tall som Enterprise-motoren ville vist, med EKTE bilag på norsk
kontoplan: kundefaktura 1000 kr @ 25 % → BASE_3=1000/TAX_3=250,
leverandørfaktura 400 kr @ 25 % → TAX_1=100, fastsatt = 150.

Dekker også oppgjørssteget som erstatter Enterprise account.return:
oppgjørsbilag med 2740 i hele kroner. Betalingsordre-flyten (OCA) testes
i bro-modulen l10n_no_account_mvamelding_payment — denne modulen (og
dens tester) skal være kjørbar UTEN OCA installert (Produkt 2).
"""
from odoo import Command
from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged


@tagged('post_install_l10n', 'post_install', '-at_install')
class TestTaxSourceCommunity(AccountTestInvoicingCommon):
    @classmethod
    @AccountTestInvoicingCommon.setup_country('no')
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.company_data['company']
        # Syntetisk Tenor-orgnr så _orgnr resolver (samme mønster som
        # modulens øvrige tester)
        cls.company.sudo().l10n_no_eristo_test_orgnr = '310200808'
        cls.Tax = cls.env['account.tax']
        cls.sale_tax_25 = cls.Tax.search([
            ('company_id', '=', cls.company.id),
            ('type_tax_use', '=', 'sale'),
            ('amount', '=', 25.0),
            ('amount_type', '=', 'percent'),
        ], limit=1)
        cls.purchase_tax_25 = cls.Tax.search([
            ('company_id', '=', cls.company.id),
            ('type_tax_use', '=', 'purchase'),
            ('amount', '=', 25.0),
            ('amount_type', '=', 'percent'),
        ], limit=1)
        assert cls.sale_tax_25 and cls.purchase_tax_25, \
            "Norsk kontoplan mangler 25 %-avgifter"

        cls._post_invoice('out_invoice', 1000.0, cls.sale_tax_25,
                          '2026-03-15')
        cls._post_invoice('in_invoice', 400.0, cls.purchase_tax_25,
                          '2026-04-02')

        cls.mva = cls.env['l10n.no.mvamelding'].create({
            'company_id': cls.company.id,
            'aar': 2026,
            'periode': '2',  # mars–april
        })

    @classmethod
    def _post_invoice(cls, move_type, amount, tax, date_str):
        invoice = cls.env['account.move'].create({
            'move_type': move_type,
            'partner_id': cls.partner_a.id,
            'invoice_date': date_str,
            'date': date_str,
            'company_id': cls.company.id,
            'invoice_line_ids': [(0, 0, {
                'name': 'Testlinje',
                'quantity': 1,
                'price_unit': amount,
                'tax_ids': [(6, 0, tax.ids)],
            })],
        })
        invoice.action_post()
        return invoice

    # ---- tallkilde ----------------------------------------------------

    def test_code_values_from_real_moves(self):
        values = self.mva._tax_report_code_values()
        self.assertEqual(round(values.get('BASE_3', 0)), 1000)
        self.assertEqual(round(values.get('TAX_3', 0)), 250)
        self.assertEqual(round(values.get('TAX_1', 0)), 100)

    def test_collect_lines_and_fastsatt(self):
        lines, fastsatt = self.mva._collect_mva_lines()
        by_code = {ln['mva_kode']: ln for ln in lines}
        self.assertEqual(by_code['3']['grunnlag'], 1000)
        self.assertEqual(by_code['3']['merverdiavgift'], 250)
        self.assertEqual(by_code['1']['merverdiavgift'], -100)
        self.assertEqual(fastsatt, 150)

    def test_period_filter_excludes_other_terms(self):
        """Bilag utenfor terminen skal ikke telle med."""
        self._post_invoice('out_invoice', 500.0, self.sale_tax_25,
                           '2026-05-10')  # 3. termin
        values = self.mva._tax_report_code_values()
        self.assertEqual(round(values.get('BASE_3', 0)), 1000)

    # ---- oppgjør -------------------------------------------------------

    def _generate(self):
        self.mva.action_generate_xml()

    def test_oppgjor_posts_settlement_move(self):
        self._generate()
        self.mva.action_bokfor_oppgjor()
        move = self.mva.oppgjor_move_id
        self.assertEqual(move.state, 'posted')
        settlement = self.mva._l10n_no_get_settlement_account()
        line = move.line_ids.filtered(
            lambda l: l.account_id == settlement)
        self.assertEqual(len(line), 1)
        # Skyldig 150 kr → kreditlinje på oppgjørskontoen, hele kroner
        self.assertEqual(line.balance, -150.0)
        # MVA-kontoene skal være tømt for terminen (netto 0 inkl. oppgjør)
        tax_accounts = move.line_ids.account_id - settlement
        for account in tax_accounts.filtered(
                lambda a: a.code and a.code.startswith('27')):
            total = sum(self.env['account.move.line'].search([
                ('account_id', '=', account.id),
                ('company_id', '=', self.company.id),
                ('date', '>=', '2026-03-01'), ('date', '<=', '2026-04-30'),
                ('parent_state', '=', 'posted'),
            ]).mapped('balance'))
            self.assertAlmostEqual(total, 0.0, places=2)

    def test_oppgjor_idempotent(self):
        self._generate()
        self.mva.action_bokfor_oppgjor()
        with self.assertRaises(UserError) as ctx:
            self.mva.action_bokfor_oppgjor()
        self.assertIn('allerede bokført', str(ctx.exception))

    def test_mvamelding_er_utilgjengelig_fra_feil_selskap(self):
        """ir.rule-en ER vakten — meldingen kan ikke nås fra feil selskap.

        Husmønsteret (besluttet 2026-08-31, felles med skattemelding-
        modulen): selskapsisolasjon håndheves STRUKTURELT av den globale
        ir.rule-en på meldingsmodellen, ikke av sjekker inne i hver action.

        Dette er regresjonstesten for produksjonsfeilen 2026-08-31.
        MVA-modulen manglet regelen, så meldingen var synlig og klikkbar
        fra feil selskap mens ALLE account.*-oppslagene bak knappene ble
        filtrert bort av kjernens egne selskapsregler — og oppgjøret
        diagnostiserte det som manglende kontoplan. Med regelen på plass
        stopper det ett steg tidligere, med Odoos egen melding som
        NAVNGIR selskapet man må bytte til.
        """
        annet = self.env['res.company'].create({'name': 'Annet Selskap AS'})
        self.env.user.company_ids = [Command.link(annet.id)]

        # Selve feilen fra produksjon: kontoene FINNES, men er usynlige i
        # feil selskapskontekst. Uten denne asserten kunne testen bestått
        # fordi fixturen manglet kontoplan — altså av feil grunn.
        domain = [
            ('company_id', '=', self.company.id),
            ('use_in_tax_closing', '=', True),
            ('account_id', '!=', False),
        ]
        Rep = self.env['account.tax.repartition.line']
        self.assertTrue(Rep.search_count(domain))
        self.assertFalse(
            Rep.with_context(allowed_company_ids=annet.ids)
               .search_count(domain))

        # Regelen skal stoppe det FØR knappen: meldingen er hverken
        # synlig i lista eller lesbar via direktelenke.
        Mva = self.env['l10n.no.mvamelding'].with_context(
            allowed_company_ids=annet.ids)
        self.assertFalse(Mva.search([('id', '=', self.mva.id)]))
        with self.assertRaises(AccessError):
            Mva.browse(self.mva.id).aar

    def test_oppgjor_ok_naar_selskapet_er_ett_av_flere_aktive(self):
        """Regelen skal ikke slå ut når selskapet ER blant de aktive.

        Motprøven til testen over. `company_ids` i ir.rule-domenet er
        env.companies.ids, altså ALLE aktive selskaper — ikke bare det
        første. Oppgjøret skal derfor gå igjennom for en bruker som
        kjører to selskaper aktive samtidig, som er normalflyten i et
        konsern med felles regnskapsfører.
        """
        self._generate()
        annet = self.env['res.company'].create({'name': 'Annet Selskap AS'})
        self.env.user.company_ids = [Command.link(annet.id)]
        self.mva.with_context(
            allowed_company_ids=(annet + self.company).ids,
        ).action_bokfor_oppgjor()
        self.assertEqual(self.mva.oppgjor_move_id.state, 'posted')

    def test_merknad_er_utilgjengelig_fra_feil_selskap(self):
        """Merknaden trenger EGEN regel — ir.rule cascader ikke.

        Merknaden er en selvstendig modell med egen tabell, så regelen på
        l10n.no.mvamelding beskytter den ikke. Uten sin egen regel ville
        merknadene vært lesbare fra feil selskap selv om meldingen de
        hører til ikke er det.

        Innholdet er fritekst som forklarer selskapets avgiftsforhold til
        Skatteetaten — typisk hvorfor et fradrag er tilbakeført. Det hører
        ikke hjemme hos et søsterselskap.
        """
        merknad = self.env['l10n.no.mvamelding.merknad'].create({
            'mvamelding_id': self.mva.id,
            'mva_kode': '1',
            'beskrivelse': "Tilbakeføring av uberettiget fradrag.",
        })
        # company_id er en LAGRET related fra meldingen — uten den ville
        # domenet i regelen ikke hatt noe å treffe på.
        self.assertEqual(merknad.company_id, self.company)

        annet = self.env['res.company'].create({'name': 'Annet Selskap AS'})
        self.env.user.company_ids = [Command.link(annet.id)]

        Merknad = self.env['l10n.no.mvamelding.merknad'].with_context(
            allowed_company_ids=annet.ids)
        self.assertFalse(Merknad.search([('id', '=', merknad.id)]))
        with self.assertRaises(AccessError):
            Merknad.browse(merknad.id).beskrivelse

    def test_merknad_synlig_naar_selskapet_er_aktivt(self):
        """Motprøven: regelen skal ikke skjule egne merknader."""
        merknad = self.env['l10n.no.mvamelding.merknad'].create({
            'mvamelding_id': self.mva.id,
            'mva_kode': '1',
            'beskrivelse': "Tilbakeføring av uberettiget fradrag.",
        })
        annet = self.env['res.company'].create({'name': 'Annet Selskap AS'})
        self.env.user.company_ids = [Command.link(annet.id)]

        Merknad = self.env['l10n.no.mvamelding.merknad'].with_context(
            allowed_company_ids=(annet + self.company).ids)
        self.assertEqual(
            Merknad.browse(merknad.id).beskrivelse,
            "Tilbakeføring av uberettiget fradrag.")

    def test_oppgjor_med_ore_rest_fra_flere_aktive_selskaper(self):
        """Avrundingsstien, kjørt med «feil» selskap først blant de aktive.

        ir.rule-en garanterer at meldingens selskap er BLANT de aktive — ikke
        at det er `env.company`. Det betyr noe her: `code` på account.account
        er company_dependent i Odoo 19 (`code_store`), og `_search_code`
        resolves mot `env.company.root_id` (account_account.py:341).

        Testen fastholder BÅDE mekanismen og utfallet:

        1. Premisset — kodeoppslaget bommer med feil selskap aktivt. Uten
           denne asserten ville resten stått uten begrunnelse.
        2. Utfallet — oppgjøret går igjennom og avrundingslinjen havner på
           MELDINGENS egen konto.

        ÆRLIG OM DEKNINGEN: mutasjonstest viser at innsnevringen i
        action_bokfor_oppgjor ikke kan observeres via denne stien i dag.
        _l10n_no_get_rounding_account() faller tilbake på
        ('name', '=', 'Rounding'), og `name` er IKKE selskapsavhengig — så
        fallbacken finner kontoen selv når kodeoppslaget bommer. Innsnevringen
        er derfor defense-in-depth her: den fjerner avhengigheten av en
        fallback som forsvinner i det noen døper kontoen «Avrunding».
        Premiss-asserten under er det som faktisk fanger regresjonen, ved å
        låse mekanismen fast uavhengig av fallbacken.

        Motprøven over når uansett ikke hit: fixturen der går opp i hele
        kroner, så diff == 0 og avrundingsgrenen kjøres aldri.
        """
        # 1000,40 @ 25 % = 250,10 → øre-rest, så avrundingsgrenen kjøres
        self._post_invoice('out_invoice', 1000.40, self.sale_tax_25,
                           '2026-03-20')
        self._generate()

        annet = self.env['res.company'].create({'name': 'Annet Selskap AS'})
        self.env.user.company_ids = [Command.link(annet.id)]

        # 1. Premisset: samme søk, ulikt aktivt selskap, ulikt svar.
        Acc = self.env['account.account']
        kode_domene = [('code', '=', '7740'),
                       ('company_ids', 'in', self.company.id)]
        self.assertTrue(Acc.with_company(self.company).search(kode_domene))
        self.assertFalse(
            Acc.with_company(annet).search(kode_domene),
            "code er company_dependent — oppslaget SKAL bomme fra feil "
            "selskap. Slår denne feil, har Odoo endret semantikken og "
            "innsnevringen i action_bokfor_oppgjor kan revurderes.")

        # 2. Utfallet: annet FØRST ⇒ env.company = annet, den vonde konteksten.
        self.mva.with_context(
            allowed_company_ids=(annet + self.company).ids,
        ).action_bokfor_oppgjor()

        move = self.mva.oppgjor_move_id
        self.assertEqual(move.state, 'posted')
        rounding = self.mva._l10n_no_get_rounding_account()
        linje = move.line_ids.filtered(lambda l: l.account_id == rounding)
        self.assertTrue(
            linje, "Avrundingslinjen mangler — oppslaget etter 7740 bommet")
        self.assertIn(self.company, linje.account_id.company_ids)

    def test_merknad_sorteres_numerisk(self):
        """mva_kode er en Selection av strenger — leksikalsk sortering ville
        gitt 1, 11, 13, 3, i utakt med Selection-rekkefølgen (key=int)."""
        for kode in ('13', '3', '1', '11'):
            self.env['l10n.no.mvamelding.merknad'].create({
                'mvamelding_id': self.mva.id,
                'mva_kode': kode,
                'beskrivelse': "Forklaring for kode %s." % kode,
            })
        self.assertEqual(
            self.mva.merknad_ids.mapped('mva_kode'), ['1', '3', '11', '13'])

    def test_core_only_ingen_oca_avhengighet(self):
        """Kjernens kontrakt (Produkt 2): modulen skal ikke kreve OCA.

        Verifiserer at manifestet ikke drar inn account_payment_order/
        sepa — betalingsordre-flyten hører til i bro-modulen
        l10n_no_account_mvamelding_payment.
        """
        module = self.env['ir.module.module'].search([
            ('name', '=', 'l10n_no_account_mvamelding')])
        deps = set(module.dependencies_id.mapped('name'))
        self.assertNotIn('account_payment_order', deps)
        self.assertNotIn('account_banking_sepa_credit_transfer', deps)
