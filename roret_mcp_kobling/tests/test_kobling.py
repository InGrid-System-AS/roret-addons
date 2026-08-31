"""Testene ER spesifikasjonen for «Koble til Roret» (#199, inkrement 5).

Modulen håndterer en API-nøkkel i klartekst. Nesten alt som kan gå galt her,
går galt STILLE:

* En nøkkel som tilhører feil bruker gir et revisjonsspor som ser riktig ut og
  peker på feil menneske.
* En nøkkel som arver gruppepolicyens levetid virker gjennom hele innføringen
  og er død et kvartal senere, når ingen lenger forbinder feilen med oppsettet.
* En nøkkel som blir liggende igjen etter et mislykket forsøk er aktiv
  legitimasjon ingen vet om.
* Et feltnavn som ikke matcher Rorets kontrakt gir HTTP 400 først i produksjon.

Ingen av dem gjør noe rødt av seg selv. Derfor står de her.
"""

import datetime
import json
import pathlib
import re
from xml.etree import ElementTree
from unittest.mock import patch

from odoo import Command, fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase
from odoo.tools.safe_eval import safe_eval

from ..wizard.roret_mcp_kobling import (
    ENDEPUNKT_PARAM,
    NOKKELNAVN,
    NOKKELSCOPE,
)

MODUL = 'odoo.addons.roret_mcp_kobling.wizard.roret_mcp_kobling'


class FalsktSvar:
    """Minimal stand-in for `requests.Response` — kun det koden leser."""

    def __init__(self, status_code=200, kropp=None):
        self.status_code = status_code
        self._kropp = kropp

    @property
    def ok(self):
        """Speiler `requests.Response.ok` — sann under 400, altså også for 3xx.

        Tatt med selv om koden ikke bruker den: uten `ok` ville en refaktor til
        `if svar.ok:` blitt rød på en `AttributeError` her i stedet for på
        regelen den bryter, og feilmeldingen ville pekt på testens stand-in
        framfor på at en redirect ble behandlet som suksess.
        """
        return self.status_code < 400

    def json(self):
        if self._kropp is None:
            raise ValueError("ingen JSON")
        return self._kropp


class TestKobling(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env['ir.config_parameter'].sudo().set_param(
            'web.base.url', 'https://kunde.example.com')
        cls.env['ir.config_parameter'].sudo().set_param(
            ENDEPUNKT_PARAM, 'https://mcp.example.com')

        # En HELT VANLIG ansatt, ikke administrator. Det er den brukeren
        # modulen finnes for, og den eneste som avslører policy-fella med
        # nøkkellevetid — for en Settings-bruker er `_check_expiration_date`
        # en no-op uansett.
        cls.ansatt = cls.env['res.users'].create({
            'name': "Test Ansatt",
            'login': 'ansatt@example.com',
            'group_ids': [Command.set([cls.env.ref('base.group_user').id])],
        })
        cls.annen = cls.env['res.users'].create({
            'name': "Annen Ansatt",
            'login': 'annen@example.com',
            'group_ids': [Command.set([cls.env.ref('base.group_user').id])],
        })

    # ------------------------------------------------------------------
    # Hjelpere
    # ------------------------------------------------------------------

    def _wizard(self, bruker=None, kode='paringskode-abc'):
        bruker = bruker or self.ansatt
        return self.env['roret.mcp.kobling'].with_user(bruker).create({
            'paringskode': kode,
        })

    def _nokler(self, bruker=None, navn=NOKKELNAVN):
        bruker = bruker or self.ansatt
        return self.env['res.users.apikeys'].sudo().search([
            ('user_id', '=', bruker.id),
            ('name', '=', navn),
        ])

    def _koble(self, wizard=None, svar=None, sideeffekt=None):
        """Kjør action_koble med POSTen stubbet. Returnerer mocken."""
        wizard = wizard if wizard is not None else self._wizard()
        with patch(f'{MODUL}.requests.post') as post:
            if sideeffekt is not None:
                post.side_effect = sideeffekt
            else:
                post.return_value = svar or FalsktSvar(200)
            wizard.action_koble()
        return post

    def _forvent_feil(self, kall):
        """Kjør `kall`, krev en UserError, og LA BASEN STÅ som den ble.

        **`assertRaises` kan ikke brukes når testen skal inspisere basen
        etterpå.** Odoos `BaseCase._assertRaises` legger en savepoint rundt
        blokka og ruller den tilbake når den forventede feilen kommer
        (`odoo/tests/common.py:502-520`). Det er riktig hygiene for testen og
        fatalt for oss: den angrer nøyaktig den skrivingen vi vil bevise at
        koden selv rydder opp i.

        Dette er ikke teori. Første versjon av testene her brukte
        `assertRaises`, og med `ny._remove()` fjernet fra `action_koble` var
        de fortsatt grønne — den muterte koden lot en aktiv API-nøkkel bli
        liggende, og seks tester påsto at den ikke gjorde det. Målt 2026-08-05.

        En `UserError` er en ren Python-feil, ikke en databasefeil, så
        transaksjonen er brukbar videre uten savepointen.
        """
        try:
            kall()
        except UserError as e:
            return e
        self.fail("forventet UserError, men kallet gikk gjennom")

    # ------------------------------------------------------------------
    # Hvem nøkkelen tilhører
    # ------------------------------------------------------------------

    def test_nokkelen_tilhorer_brukeren_som_klikket(self):
        """Hele revisjonspoenget i #199.

        `_mint_nokkel` bruker `sudo()`, og den nærliggende feilen er å tro at
        det gjør nøkkelen til superbrukerens. `sudo()` setter `su`-flagget, den
        endrer ikke `uid` — men den forskjellen er usynlig i koden og fatal i
        praksis: en nøkkel som tilhører superbrukeren gir agenten ubegrenset
        tilgang OG et revisjonsspor som peker på ingen.
        """
        self._koble(self._wizard(self.ansatt))

        nokler = self._nokler(self.ansatt)
        self.assertEqual(len(nokler), 1)
        self.assertEqual(nokler.user_id, self.ansatt)
        self.assertEqual(nokler.scope, NOKKELSCOPE)
        # Superbrukeren skal ikke ha fått noe.
        self.assertFalse(self.env['res.users.apikeys'].sudo().search([
            ('user_id', '=', self.env.ref('base.user_root').id),
            ('name', '=', NOKKELNAVN),
        ]))

    def test_nokkelen_er_permanent(self):
        """En utløpsdato her er en tidsinnstilt stille feil.

        Se kontrolltesten under for hvorfor dette ikke er gratis.
        """
        self._koble(self._wizard(self.ansatt))
        self.assertFalse(
            self._nokler(self.ansatt).expiration_date,
            "nøkkelen har utløpsdato — koblingen dør av seg selv")

    def test_uten_sudo_ville_nokkelen_utlopt(self):
        """Kontrollen som gjør testen over meningsfull.

        Uten denne kunne `sudo()` i `_mint_nokkel` vært ren dekorasjon, og
        `test_nokkelen_er_permanent` bestått uten å bevise noe. Her måles
        kjernens faktiske adferd for NØYAKTIG denne brukeren, i begge retninger:
        hun kan hverken få en permanent nøkkel eller en som varer lenge nok til
        at utløpet slutter å være et problem.

        Adferd, ikke tall: taket er 90 dager i dag (`base.group_user` i
        `base_groups.xml:45`), men å låse testen til 90 ville gjort den rød
        neste gang Odoo justerer sin egen default — uten at noe hos oss var
        galt. Det vi trenger å vite er at taket FINNES.
        """
        apikeys = self.env['res.users.apikeys'].with_user(self.ansatt)

        # Permanent: avvist uten `su`.
        with self.assertRaises(ValidationError):
            apikeys._generate(NOKKELSCOPE, NOKKELNAVN, None)

        # Og «lenge nok til å ikke bry seg» er også avvist.
        om_ti_aar = fields.Datetime.now() + datetime.timedelta(days=3650)
        with self.assertRaises(ValidationError):
            apikeys._generate(NOKKELSCOPE, NOKKELNAVN, om_ti_aar)

    def test_koble_fra_rorer_ikke_andre_brukeres_nokler(self):
        """Frakobling er per menneske, ikke per database."""
        self._koble(self._wizard(self.ansatt))
        self._koble(self._wizard(self.annen))
        self.assertTrue(self._nokler(self.ansatt))
        self.assertTrue(self._nokler(self.annen))

        self._wizard(self.ansatt).action_koble_fra()

        self.assertFalse(self._nokler(self.ansatt))
        self.assertTrue(
            self._nokler(self.annen),
            "frakobling slettet en annen brukers nøkkel")

    def test_brukerens_egne_nokler_rores_ikke(self):
        """Vi rydder i VÅRE nøkler, ikke i brukerens.

        `_mine_roret_nokler` filtrerer på både bruker og navn. Faller
        navnefilteret bort, sletter «Koble fra» — og rotasjonen i
        `action_koble` — nøkler kunden har laget til sine egne integrasjoner.
        Den skaden er stille: den oppdages først når et helt annet system
        slutter å komme inn.
        """
        # `with_user` FØR `sudo` — motsatt rekkefølge nullstiller `su`, og
        # `_generate(..., None)` avvises da for en vanlig ansatt.
        egen = self.env['res.users.apikeys'].with_user(self.ansatt).sudo()
        egen._generate('rpc', "Min egen integrasjon", None)

        self._koble(self._wizard(self.ansatt))          # rotasjon rydder
        self._wizard(self.ansatt).action_koble_fra()    # frakobling rydder

        self.assertTrue(
            self._nokler(self.ansatt, navn="Min egen integrasjon"),
            "brukerens egen API-nøkkel ble slettet")

    def test_menyen_er_synlig_og_ligger_der_guidingen_lover(self):
        """PLASSERINGEN, ikke navnet — det er den som ble valgt bevisst.

        Menyen ligger på toppnivå og ikke under Innstillinger fordi
        Innstillinger krever `base.group_system`, og hele poenget med flyten er
        at hver ansatt skal kunne koble SEG SELV. Flyttes den dit i morgen —
        eller byttes `groups` til `group_system` — står navnene urørt, og en
        vakt som bare måler navn er fortsatt grønn mens en vanlig ansatt igjen
        ikke finner noe. (Review-funn på #236.)

        **Begge ledd av stien voktes.** `koble_til_odoo` lover «Roret» → «Koble
        til Roret». At begge er synlige er ikke nok: gis menyvalget en annen
        forelder, er begge fortsatt synlige og roten fortsatt på toppnivå,
        mens kunden leter under «Roret» etter noe som ligger et annet sted.
        (Review-funn på #236, andre runde.)

        `load_menus` er OFFENTLIG API (`@api.model`, ingen `_`-prefiks) — det
        webklienten selv kaller for å tegne menyen. Første versjon brukte
        `_visible_menu_ids()`, som er privat, og CLAUDE.md-beslutning 6 ber oss
        unngå avhengigheter til flyktige interne API-er. `load_menus` er
        dessuten strengere: den filtrerer bort menyer som ikke henger under en
        SYNLIG app-rot, altså nøyaktig det nettleseren viser.

        ⚠ Måles dette med en mutasjon, må det være `-i` mot en FERSK base, ikke
        `-u`. Odoos oppdatering av `groups` på en `<menuitem>` LEGGER TIL
        gruppen, den erstatter ikke — målt: etter `-u` med
        `groups="base.group_system"` sto rotmenyen med BÅDE «Role / User» og
        «Role / Administrator», og mutasjonen var nøytralisert. Samme mutasjon
        er rød på `-i`, som er det CI kjører.
        """
        rot = self.env.ref('roret_mcp_kobling.menu_roret_root')
        valg = self.env.ref('roret_mcp_kobling.menu_roret_kobling')

        self.assertFalse(
            rot.parent_id,
            f"«{rot.name}» er ikke lenger en toppnivåmeny (ligger under "
            f"«{rot.parent_id.name}») — en vanlig ansatt når den kanskje ikke")
        self.assertEqual(
            valg.parent_id, rot,
            f"«{valg.name}» ligger ikke lenger under «{rot.name}» — guidingen "
            "i koble_til_odoo lover nettopp den stien")

        menyer = self.env['ir.ui.menu'].with_user(self.ansatt).load_menus(False)
        self.assertIn(
            rot.id, menyer, "en vanlig ansatt ser ikke Roret-menyen")
        self.assertIn(
            valg.id, menyer,
            "en vanlig ansatt ser ikke «Koble til Roret» — da kan hen ikke "
            "koble seg selv, og nøkkelen må mintes av en administrator")

        # Og at klikket faktisk åpner OSS. Navn, plassering og synlighet kan
        # alle være riktige mens `action=` peker et annet sted — da klikker
        # kunden «Koble til Roret» og får opp brukerlista. Menyen finnes, står
        # der guidingen lover, er synlig, og gjør noe annet.
        # (Review-funn på #236.)
        self.assertTrue(
            valg.action,
            f"«{valg.name}» har ingen handling — klikket gjør ingenting")
        self.assertEqual(
            valg.action.res_model, self.env['roret.mcp.kobling']._name,
            f"«{valg.name}» åpner {valg.action.res_model!r}, ikke "
            f"oppkoblingsdialogen")

        # Actionen har sin EGEN gruppebegrensning, uavhengig av menyens.
        # `load_menus` fanger en strammet `groups` på `ir.ui.menu`, men
        # `ir.actions.act_window.group_ids` er et annet felt: settes den til
        # `base.group_system`, står menyen synlig og skjemaet uendret, mens
        # kunden nektes selve handlingen. Kjernen gater slik:
        #
        #     if groups and not any(user.has_group(x) for x in groups): continue
        #
        # (`ir_actions.py:168-170`.) Speilet her på gruppemedlemskap.
        # (Review-funn på #236.)
        grupper = valg.action.group_ids
        self.assertTrue(
            not grupper or (grupper & self.ansatt.all_group_ids),
            f"actionen er begrenset til {grupper.mapped('name')}, som en "
            f"vanlig ansatt ikke er med i — menyen er synlig, men handlingen "
            f"nektes")

        # Actionens RENDRINGSKONFIGURASJON, ikke bare hvilken modell den
        # åpner. `get_view(..., 'form')` gir arch-en uansett hva actionen
        # gjør med den, og `target` er den som stille fjerner knappen:
        # «Koble til» ligger i en `<footer>`, og en footer kompileres BARE
        # når skjemaet tegnes som dialog. Med `target="current"` er arch-en
        # identisk — vakten grønn — mens kunden får en full side med
        # paringskode-feltet og ingenting å klikke.
        #
        # `view_mode` er smalere, men gratis i samme sjekk: med `list` gir
        # klikket en liste over transiente records. (Review-funn på #236.)
        self.assertEqual(
            valg.action.target, 'new',
            f"actionen åpner som {valg.action.target!r}, ikke dialog — "
            f"«Koble til» ligger i en <footer> og kompileres da ikke")
        self.assertEqual(
            valg.action.view_mode, 'form',
            f"actionen har view_mode={valg.action.view_mode!r} — klikket "
            f"åpner da ikke oppkoblingsskjemaet")

        # Og at SKJEMAET som faktisk åpnes har knappen guidingen lover.
        # `res_model` sier hvilken modell som åpnes, ikke hvilket view: en
        # `view_id`-overstyring på actionen ville sendt kunden til et annet
        # skjema på samme modell. Vakten i test_onboarding_tool.py leser fila,
        # ikke den ferdige arch-en, så den ser det ikke.
        # (Review-funn på #236.)
        # Hentes MED actionens context. Klienten kjører actionen med sin egen
        # context merget inn, og `ir.ui.view._get_view_id` leser
        # `form_view_ref` derfra FØR den faller tilbake på default-viewen. Et
        # `{'form_view_ref': '…'}` styrer altså hvilket skjema som tegnes uten
        # å røre `view_id` — og uten denne linja rendrer testen et annet
        # skjema enn kunden ser. (Review-funn på #236.)
        #
        # Å forby `form_view_ref` ville vært den smale rettelsen; å rendre slik
        # klienten gjør det dekker også de andre `*_view_ref`-nøklene og
        # alt annet context-en måtte styre.
        # View-iden slås opp i `action.views` — det webklienten selv leser.
        # Feltet computes fra `view_ids` (One2many til
        # `ir.actions.act_window.view`), så en overstyrings-record der bytter
        # skjema uten å røre hverken `view_id` eller `context`. Å lese
        # `view_id` direkte ville derfor rendret noe annet enn kunden ser.
        # (Review-funn på #236.)
        #
        # Samme flytt som `res_model`-bindingen og context-rendringen: mål
        # UTFALLET, ikke mekanismen. Da trenger testen ikke kjenne de tre
        # inngangene til view-oppslaget, bare svaret de gir til sammen.
        ctx = safe_eval(valg.action.context or '{}')
        form_view_id = next(
            (vid or None for vid, mode in valg.action.views if mode == 'form'),
            None)
        arch = (self.env['roret.mcp.kobling']
                .with_user(self.ansatt).with_context(**ctx)
                .get_view(form_view_id, 'form')['arch'])
        rot_arch = ElementTree.fromstring(arch)
        forelder = {barn: mor for mor in rot_arch.iter() for barn in mor}
        knapper = {b.get('name'): b for b in rot_arch.iter('button')}
        koble = knapper.get('action_koble')
        self.assertIsNotNone(
            koble,
            f"skjemaet «{valg.name}» åpner har ingen action_koble-knapp — "
            f"knappene er {sorted(k for k in knapper if k)}. Guidingen i "
            f"koble_til_odoo lover den.")
        self.assertEqual(
            koble.get('string'), "Koble til",
            f"knappen heter «{koble.get('string')}», ikke «Koble til» som "
            f"guidingen lover")

        # Og at den ikke er BETINGET SKJULT. Å finnes i arch-en er ikke det
        # samme som å være klikkbar: `invisible="er_koblet"` ville fjernet
        # knappen for nøyaktig de brukerne skjemaets egen tekst lover noe til
        # («Limer du inn en ny kode, erstattes koblingen»). Rotasjonen er
        # testet gjennom Python lenger nede, men det sier bare at den VIRKER —
        # ikke at kunden kommer til den. (Review-funn på #236.)
        #
        # Hele forelderkjeden, ikke bare knappen selv: et vilkår ett nivå opp
        # skjuler den like effektivt. Det er ikke en konstruert form — fila
        # bruker allerede mønsteret (`<div invisible="not er_koblet">` rundt
        # infoboksen, og `invisible="not er_koblet"` på «Koble fra»), så å
        # gruppere de to koble-knappene i en container med et vilkår er
        # nettopp redigeringen man gjør her. (Review-funn på #236.)
        # Attributtet tas som parameter. Første versjon gikk oppover for
        # `invisible` og sjekket `readonly` med én `.get()` på feltnoden —
        # samme asymmetri som forelderkjeden nettopp fjernet for det andre
        # attributtet. Feltet ligger allerede i en `<group>`, så en
        # `readonly="1"` der ville gitt kunden identisk utfall (feltet
        # synlig, knappen klikkbar, koden kan ikke limes inn) med grønn
        # vakt. (Review-funn på #236.)
        def krev_uten(element, attributt, hva, hvorfor):
            node = element
            while node is not None:
                self.assertFalse(
                    node.get(attributt),
                    f"{hva} har {attributt}={node.get(attributt)!r} satt på "
                    f"<{node.tag}>. {hvorfor}")
                node = forelder.get(node)

        def krev_synlig(element, hva):
            krev_uten(
                element, 'invisible', hva,
                "Skjemaet lover at en ny kode erstatter koblingen, og da må "
                "dette være der for en bruker som allerede er koblet til.")

        krev_synlig(koble, "«Koble til»")

        # Og at et klikk faktisk KJØRER noe. `special` kortslutter `name` og
        # `type`: klienten kompilerer `special="cancel"` til «lukk dialogen»
        # og ser aldri på hva knappen heter. En knapp med
        # `name="action_koble" special="cancel"` består alle asserts over —
        # og kunden limer inn koden, klikker «Koble til», dialogen lukker
        # seg, ingenting skjer, ingen feilmelding.
        #
        # Formen er ikke konstruert: `special="cancel"` står allerede på
        # «Avbryt» i samme skjema. Det er samme utfall som «ekte knapp omdøpt
        # + avbryt tar navnet» fra en tidligere runde, nådd uten å røre
        # navnet. (Review-funn på #236.)
        self.assertFalse(
            koble.get('special'),
            f"«Koble til» har special={koble.get('special')!r} — det "
            f"kortslutter name/type, så klikket lukker dialogen i stedet for "
            f"å koble til, uten feilmelding")
        # `type` er belte-og-bukseseler: `type="action"` med et metodenavn
        # avvises allerede av Odoos egen view-validering, og modulen nekter å
        # installere (målt — CI ville stoppet før testene). Asserten fanger
        # altså ikke noe kjent tilfelle, men skriver ned kravet.
        self.assertEqual(
            koble.get('type'), 'object',
            f"«Koble til» har type={koble.get('type')!r}, ikke 'object' — "
            f"klikket kaller da ikke action_koble")

        # Guidingen ber om TO handlinger: «lim inn koden OG klikk «Koble til»».
        # Klikket var voktet, innlimingen ikke — og det er der hele flyten
        # hviler. Fjernes feltet, åpner riktig dialog med knappen synlig, og
        # kunden har ingen steder å lime koden; klikker hun likevel, får hun
        # «Lim inn paringskoden …», altså beskjed om å gjøre noe skjemaet ikke
        # lar henne gjøre.
        #
        # Python-testene ser det ikke: `_wizard()` setter feltet direkte i
        # `create()`, så de beviser at flyten VIRKER uten å røre view-laget.
        # Samme mønster som knappen, ett felt til venstre. (Review-funn på
        # #236.)
        felt = {f.get('name'): f for f in rot_arch.iter('field')}
        kode = felt.get('paringskode')
        self.assertIsNotNone(
            kode,
            f"skjemaet «{valg.name}» åpner har ikke feltet «paringskode» — "
            f"feltene er {sorted(f for f in felt if f)}. Da har kunden ingen "
            f"steder å lime inn koden guidingen ber om.")
        krev_synlig(kode, "paringskode-feltet")
        krev_uten(
            kode, 'readonly', "paringskode-feltet",
            "Kunden kan da ikke lime inn koden guidingen ber om.")

    # ------------------------------------------------------------------
    # Feilstier: basen skal se ut som før
    # ------------------------------------------------------------------

    def test_feilet_post_etterlater_ingen_nokkel(self):
        """En mintet nøkkel som ikke nådde fram er legitimasjon uten eier.

        Vi kastet klarteksten, så ingen kan bruke den — men den står som en
        aktiv nøkkel i kundens base, og den forsvinner ikke av seg selv.
        """
        import requests

        wizard = self._wizard(self.ansatt)
        self._forvent_feil(
            lambda: self._koble(wizard,
                                sideeffekt=requests.ConnectionError("nede")))

        self.assertFalse(
            self._nokler(self.ansatt),
            "nøkkelen ble liggende igjen etter et mislykket forsøk")

    def test_feilet_rotasjon_beholder_den_gamle_koblingen(self):
        """Et mislykket forsøk skal ikke koste kunden koblingen hun HAR.

        Rekkefølgen «mint ny → send → fjern gammel» er hele forskjellen. Ryddes
        den gamle først, står kunden uten agent fordi Roret var nede i ti
        sekunder — og hun har ingen måte å få den tilbake på selv.
        """
        import requests

        self._koble(self._wizard(self.ansatt))
        gammel = self._nokler(self.ansatt)
        self.assertEqual(len(gammel), 1)

        self._forvent_feil(
            lambda: self._koble(self._wizard(self.ansatt),
                                sideeffekt=requests.ConnectionError("nede")))

        self.assertEqual(
            self._nokler(self.ansatt), gammel,
            "den fungerende koblingen forsvant på et mislykket forsøk")

    def test_vellykket_rotasjon_erstatter_den_gamle(self):
        self._koble(self._wizard(self.ansatt))
        gammel = self._nokler(self.ansatt)

        self._koble(self._wizard(self.ansatt))

        ny = self._nokler(self.ansatt)
        self.assertEqual(len(ny), 1, "rotasjon etterlot flere nøkler")
        self.assertNotEqual(ny, gammel, "den gamle nøkkelen ble ikke fjernet")

    def test_endepunkt_over_http_minter_ingen_nokkel(self):
        """Valideringen må skje FØR mintingen.

        Rekkefølgen er ikke kosmetikk: validerer vi etterpå, har vi allerede
        laget en nøkkel vi må rydde bort — og feilstien som rydder er nettopp
        den som er lettest å gjøre feil.
        """
        self.env['ir.config_parameter'].sudo().set_param(
            ENDEPUNKT_PARAM, 'http://mcp.example.com')
        self._forvent_feil(lambda: self._koble(self._wizard(self.ansatt)))
        self.assertFalse(self._nokler(self.ansatt))

    def test_lokal_base_url_minter_ingen_nokkel(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'web.base.url', 'http://localhost:8069')
        self._forvent_feil(lambda: self._koble(self._wizard(self.ansatt)))
        self.assertFalse(self._nokler(self.ansatt))

    def test_tom_kode_sender_ingenting(self):
        wizard = self._wizard(self.ansatt, kode='   ')
        with patch(f'{MODUL}.requests.post') as post:
            self._forvent_feil(wizard.action_koble)
        post.assert_not_called()
        self.assertFalse(self._nokler(self.ansatt))

    def test_koble_fra_uten_kobling_feiler_tydelig(self):
        with self.assertRaises(UserError):
            self._wizard(self.ansatt).action_koble_fra()

    # ------------------------------------------------------------------
    # Kontrakten mot Roret
    # ------------------------------------------------------------------

    def test_payloaden_har_feltene_roret_krever(self):
        """Feltnavnene er en kontrakt mot `POST /onboard`, ikke et valg.

        Lista leses ut av MCP-ens egen kildekode i dette repoet, ikke skrevet
        av på nytt her. En avskrift ville vært grønn i nøyaktig det tilfellet
        den skal fange: at den ene siden endrer seg.

        Modulen distribueres også ALENE til kunder med egen Odoo, og der finnes
        ikke serverkilden. Da må testen hoppe over — men skillet er viktig:

        * `agent/` mangler helt → vi står utenfor repoet. Hopp over.
        * `agent/` finnes, men fila ikke → noen har flyttet serverkilden, og
          kontrakten er ikke lenger sjekket. **Det skal være rødt.**

        Uten det skillet ville en flyttet fil gjort testen stille grønn — og en
        vakt som forsvinner uten lyd er verre enn ingen vakt. Det er ikke
        hypotetisk: fram til den ble målt, hoppet denne testen over seg selv i
        hver eneste lokale kjøring, fordi dev-containeren ikke monterer
        `agent/`. Første gang den faktisk kjørte, fanget den mutasjonen.
        """
        rot = pathlib.Path(__file__).resolve().parents[3]
        if not (rot / 'agent').is_dir():
            self.skipTest("kjører utenfor Roret-repoet — ingen MCP-kilde å måle mot")
        kilde = rot / 'agent' / 'mcp' / 'roret_odoo_core' / 'entrypoints' / 'http.py'
        self.assertTrue(
            kilde.exists(),
            f"{kilde} finnes ikke — er MCP-entrypointet flyttet? Kontrakten "
            "mot POST /onboard er da usjekket.")

        treff = re.search(
            r'mangler\s*=\s*\[f for f in \(([^)]*)\)', kilde.read_text('utf-8'))
        self.assertTrue(treff, "fant ikke feltlista i http.py — er ruten endret?")
        paakrevd = set(re.findall(r'"([^"]+)"', treff.group(1)))
        self.assertTrue(paakrevd, "tom feltliste lest fra http.py")

        post = self._koble(self._wizard(self.ansatt))
        sendt = post.call_args.kwargs['json']
        self.assertLessEqual(
            paakrevd, set(sendt),
            f"Roret krever {sorted(paakrevd)}, vi sender {sorted(sendt)}")

    def test_nokkelen_som_sendes_autentiserer_faktisk(self):
        """Det vi POSTer må være selve nøkkelen, ikke f.eks. indeksen.

        Uten dette ville en hvilken som helst streng i `odoo_api_key` bestått
        alle de andre testene: Roret lagrer den kryptert uten å prøve den, og
        feilen dukker først opp ved kundens første agentkall.
        """
        post = self._koble(self._wizard(self.ansatt))
        sendt = post.call_args.kwargs['json']['odoo_api_key']

        uid = self.env['res.users.apikeys'].sudo()._check_credentials(
            scope=NOKKELSCOPE, key=sendt)
        self.assertEqual(uid, self.ansatt.id)

    def test_koden_og_brukeren_sendes_som_de_er(self):
        post = self._koble(self._wizard(self.ansatt, kode='  kode-123  '))
        sendt = post.call_args.kwargs['json']
        self.assertEqual(sendt['kode'], 'kode-123', "koden ble ikke trimmet")
        self.assertEqual(sendt['odoo_username'], self.ansatt.login)
        self.assertEqual(sendt['odoo_url'], 'https://kunde.example.com')
        self.assertEqual(sendt['odoo_database'], self.env.cr.dbname)

    def test_403_og_429_forklares_for_mennesket(self):
        for status, ord_i_melding in ((403, 'kode'), (429, 'vent')):
            with self.subTest(status=status):
                feil = self._forvent_feil(
                    lambda: self._koble(self._wizard(self.ansatt),
                                        svar=FalsktSvar(status)))
                self.assertIn(ord_i_melding, str(feil).lower())
                self.assertFalse(self._nokler(self.ansatt))

    def test_ukjent_status_tar_med_rorets_forklaring(self):
        feil = self._forvent_feil(
            lambda: self._koble(self._wizard(self.ansatt),
                                svar=FalsktSvar(400, {'feil': 'mangler felt: kode'})))
        self.assertIn('mangler felt: kode', str(feil))
        self.assertFalse(
            self._nokler(self.ansatt),
            "nøkkelen ble liggende igjen etter et avvist forsøk")

    def test_vi_folger_aldri_en_redirect(self):
        """https-kravet i `_endepunkt` gjelder bare den FØRSTE URL-en.

        `requests` følger opptil 30 redirects som default, og på 307/308
        replayes metode OG kropp mot den nye adressen — også `http://`.
        Kroppen her er API-nøkkelen. (`requests` fjerner
        `Authorization`-headeren ved kryssvert-redirect, men den beskyttelsen
        gjelder ikke en hemmelighet i kroppen.)

        **Asserten er på argumentet, ikke på adferden, og det er med vilje:**
        `requests.post` er mocket, så en stubbet respons kan ikke faktisk
        redirecte. Garantien vi har er nettopp parameteret, og da er det
        parameteret som må voktes. (Review-funn på #227.)
        """
        post = self._koble(self._wizard(self.ansatt))
        self.assertIs(
            post.call_args.kwargs.get('allow_redirects'), False,
            "POSTen følger redirects — en 307 ville sendt nøkkelen videre til "
            "en vilkårlig adresse, også http")

    def test_en_3xx_er_ikke_suksess(self):
        """Motstykket: nekter vi å følge, må svaret behandles som en feil.

        `svar.ok` ville sagt suksess for 3xx, og vi hadde lagret en nøkkel
        ingen tok imot. Derfor eksplisitt 2xx.
        """
        self._forvent_feil(
            lambda: self._koble(self._wizard(self.ansatt), svar=FalsktSvar(307)))
        self.assertFalse(self._nokler(self.ansatt))

    def test_hele_2xx_er_suksess(self):
        """Ruten svarer 200 i dag, men `== 200` er strengere enn kontrakten.

        Byttet serveren til 201, ville «avvist»-stien slettet en nøkkel Roret
        ALLEREDE hadde lagret — den ene retningen feilhåndteringen ikke tåler:
        død kobling og oppbrukt paringskode.
        """
        self._koble(self._wizard(self.ansatt), svar=FalsktSvar(201))
        self.assertEqual(len(self._nokler(self.ansatt)), 1)

    # ------------------------------------------------------------------
    # Nøkkelen skal ikke finnes noe annet sted
    # ------------------------------------------------------------------

    def test_nokkelen_havner_ikke_i_loggen(self):
        """Logger havner i backup, i loggaggregatorer og på skjermer.

        Odoo-kjernen logger selve utstedelsen (scope, login, IP) — aldri
        nøkkelen. Denne testen låser at vi ikke legger den til.
        """
        wizard = self._wizard(self.ansatt)
        with self.assertLogs(level='DEBUG') as logg:
            post = self._koble(wizard)
        sendt = post.call_args.kwargs['json']['odoo_api_key']

        for linje in logg.output:
            self.assertNotIn(sendt, linje, "API-nøkkelen ble logget")

    def test_nokkelen_lagres_ikke_paa_wizarden(self):
        """En TransientModel er en tabell i kundens base.

        Et felt som «midlertidig» holder klarteksten ville lagt API-nøkler i en
        tabell som ryddes en gang i blant — altså akkurat den lagringen hele
        modulen finnes for å unngå.
        """
        wizard = self._wizard(self.ansatt)
        post = self._koble(wizard)
        sendt = post.call_args.kwargs['json']['odoo_api_key']

        wizard.invalidate_recordset()
        lest = wizard.read()[0]
        self.assertNotIn(
            sendt, json.dumps(lest, default=str),
            "API-nøkkelen ligger i et felt på wizarden")

    # ------------------------------------------------------------------
    # Odoo.sh: databasenavnet endres ved rebuild
    # ------------------------------------------------------------------

    def _eristo_base(self):
        modul = self.env['ir.module.module'].sudo().search(
            [('name', '=', 'l10n_no_eristo_base')], limit=1)
        if not modul:
            self.skipTest("l10n_no_eristo_base finnes ikke i denne addons-stien")
        return modul

    def test_discover_er_av_naar_modulen_bare_ligger_pa_disk(self):
        """`ir_module_module` har en rad for HVER modul i addons-stien.

        Det er `state` som teller, ikke at raden finnes. Uten state-filteret
        ville discovery slått seg på for enhver instans som bare har modulen
        liggende — inkludert hver Roret Cloud-instans og hver
        monorepo-checkout — og hvert kalde oppslag hos Roret kostet en bomtur
        mot en 404.

        Testen treffer det EKTE domenet. De patchede testene under beviser at
        feltet videreformidles, men de kan per konstruksjon ikke se domenet:
        med `search_count` stubbet er både modulnavnet og state-filteret
        usynlig. (Review-funn på #269.)
        """
        self._eristo_base().write({'state': 'uninstalled'})
        self.assertFalse(self._wizard()._kan_oppdage_db())

    def test_discover_er_pa_naar_modulen_er_installert(self):
        """Motsatt vei, samme ekte domene."""
        self._eristo_base().write({'state': 'installed'})
        self.assertTrue(self._wizard()._kan_oppdage_db())

    def test_discover_ser_ikke_en_annen_installert_modul(self):
        """Domenet må binde NAVNET, ikke bare state.

        Uten `('name', '=', 'l10n_no_eristo_base')` ville enhver installert
        modul slått på discovery — og i en Odoo finnes det alltid mange.
        """
        self._eristo_base().write({'state': 'uninstalled'})
        self.assertTrue(
            self.env['ir.module.module'].sudo().search_count(
                [('state', '=', 'installed')]) > 0,
            "forutsetningen holder ikke: ingen moduler er installert")
        self.assertFalse(self._wizard()._kan_oppdage_db())

    def test_discover_feltet_videreformidles_til_roret(self):
        """At valget faktisk havner i kroppen, begge veier.

        Her er `search_count` stubbet med vilje — testen måler ledningen fra
        `_kan_oppdage_db` til JSON-feltet, ikke selve valget. Valget måles av
        de tre testene over, mot det ekte domenet.
        """
        for retur, ventet in ((1, True), (0, False)):
            with self.subTest(installert=bool(retur)):
                with patch.object(type(self.env['ir.module.module']),
                                  'search_count', return_value=retur):
                    post = self._koble()
                self.assertIs(
                    post.call_args.kwargs['json']['odoo_discover_database'],
                    ventet)

    def test_db_navnet_sendes_alltid_som_fallback(self):
        """Discovery er feiltolerant i Roret-enden: svarer ikke endepunktet,
        faller den tilbake på navnet vi sendte. Derfor er `odoo_database`
        ikke valgfritt selv når discover er sant — det ER fallbacken."""
        for finnes in (0, 1):
            with self.subTest(endepunkt_finnes=bool(finnes)):
                with patch.object(type(self.env['ir.module.module']),
                                  'search_count', return_value=finnes):
                    post = self._koble()
                sendt = post.call_args.kwargs['json']
                self.assertEqual(sendt['odoo_database'], self.env.cr.dbname)
