"""«Koble til Roret» — kundevendt onboarding uten terminal (#199, inkrement 5).

Erstatter prosedyren der en Roret-operatør kjørte
`python -m roret_odoo_core.entrypoints.onboard` på prod-boksen med kundens
API-nøkkel på stdin. Den var holdbar for dogfooding og uholdbar fra kunde nr. 2:

* Nøkkelen passerte gjennom hendene på et menneske hos leverandøren.
* Revisjonssporet i kundens Odoo pekte på den brukeren operatøren tilfeldigvis
  fikk nøkkelen fra — teknisk korrekt, reelt uetterrettelig. Hele poenget med
  per-bruker-lagring i MCP-en (`PRIMARY KEY (tenant_id, user_id)`) er at sporet
  skal peke på et menneske som selv valgte å koble seg til.

Her minter brukeren sin egen nøkkel med ett klikk, og den går rett til Roret.

## Flyten

    Agenten (Claude)                 Denne modulen              Roret MCP
    koble_til_odoo ──► paringskode
                                     kode limes inn
                                     _generate(...)  server-side
                                     POST /onboard ─────────────► innløs kode
                                                                  → (tenant, sub)
                                                                  → KEK → lagre
                                     ◄──────────────────────────  {"status":"ok"}

**Paringskoden er det eneste som autoriserer POSTen** — modulen har ikke noe
bearer token, og kan ikke ha det: den skal jo skaffe oss ett. Koden bestemmer
også HVEM nøkkelen lagres for; ingenting modulen sender kan oppgi en tenant.

## Nøkkelen forlater aldri prosessen i noen annen retning

Klarteksten finnes i én lokal variabel, sendes i én HTTP-forespørsel, og
kastes. Den skrives ikke til et felt (wizarden har ingen), ikke til en logg, og
returneres ikke til nettleseren. Odoo-kjernen logger selve utstedelsen, men
kun scope + login + IP — aldri nøkkelen (`res_users.py:1611`).
"""

import logging

import requests

from odoo import _, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Navnet nøkkelen får i kundens Odoo (Innstillinger → Brukere → API-nøkler).
# Det er også det vi kjenner den igjen på ved rotasjon og frakobling, så det
# er en KONTRAKT, ikke en etikett: endres det, blir gamle nøkler foreldreløse
# — «Koble fra» finner dem ikke lenger, og de blir stående som aktiv
# legitimasjon i kundens base.
NOKKELNAVN = "Roret MCP"

# `rpc` er scopet Odoo selv krever for ekstern XML-RPC/JSON-RPC-autentisering
# (`res_users.py:389`, `ir_http.py:237`). En nøkkel med et annet scope ville
# blitt lagret uten feil og først feilet ved første agentkall.
NOKKELSCOPE = "rpc"

STANDARD_ENDEPUNKT = "https://mcp.roret.no"
ENDEPUNKT_PARAM = "roret_mcp.endepunkt"

# Lang nok til at en treg TLS-oppkobling rekker, kort nok til at brukeren ikke
# sitter og ser på en spinner om Roret er nede.
TIDSAVBRUDD = 30


class RoretMcpKobling(models.TransientModel):
    _name = 'roret.mcp.kobling'
    _description = 'Koble denne Odoo-en til Roret-agenten'

    paringskode = fields.Char(
        string="Paringskode",
        help="Koden du fikk av Roret-agenten da du ba den koble til Odoo. "
             "Den er kortlevd og kan bare brukes én gang.",
    )
    # Ingen felt for nøkkelen, og det er ikke en forglemmelse: et felt på en
    # TransientModel er en kolonne i kundens base, og en API-nøkkel i klartekst
    # i en tabell som ryddes «etter hvert» er nøyaktig det denne modulen finnes
    # for å unngå.

    er_koblet = fields.Boolean(
        string="Allerede koblet til",
        compute='_compute_er_koblet',
        help="Om denne brukeren allerede har en aktiv Roret-nøkkel.",
    )

    def _compute_er_koblet(self):
        for post in self:
            post.er_koblet = bool(post._mine_roret_nokler())

    # ------------------------------------------------------------------
    # Oppslag
    # ------------------------------------------------------------------

    def _mine_roret_nokler(self):
        """Roret-nøklene som tilhører den innloggede brukeren.

        `sudo()` på SØKET, ikke på eierskapet: `res.users.apikeys` er ikke
        lesbar for en vanlig bruker, men domenet binder treffet til
        `self.env.uid`, så en bruker ser aldri en annens nøkler.
        """
        return self.env['res.users.apikeys'].sudo().search([
            ('user_id', '=', self.env.uid),
            ('name', '=', NOKKELNAVN),
        ])

    def _endepunkt(self):
        """URL-en nøkkelen sendes til. https er et krav, ikke en preferanse.

        Parameteret kan bare endres av en Settings-bruker, og det er den
        riktige arbeidsdelingen: **administratoren bestemmer HVOR nøkler går,
        den enkelte brukeren bestemmer OM hens egen nøkkel skal dit.**

        Uten https-kravet ville en feilskrevet eller ondsinnet verdi sendt
        kundens legitimasjon til eget regnskap i klartekst over nettet.
        """
        verdi = (self.env['ir.config_parameter'].sudo().get_param(
            ENDEPUNKT_PARAM, STANDARD_ENDEPUNKT) or '').strip().rstrip('/')
        if not verdi.startswith('https://'):
            raise UserError(_(
                "Roret-endepunktet (%(param)s) må være en https-adresse, men er "
                "«%(verdi)s». API-nøkkelen sendes dit, og over http ville den "
                "gått i klartekst. Rett parameteret i Innstillinger → Teknisk → "
                "Systemparametere.",
                param=ENDEPUNKT_PARAM, verdi=verdi or "tom",
            ))
        return verdi

    def _odoo_url(self):
        """Adressen Roret skal nå denne Odoo-en på.

        Bevisst en TYNN sjekk: Roret validerer denne verdien for alvor
        (`onboarding.valider_odoo_url` — https, ingen legitimasjon, globalt
        rutbar, fullt vertsnavn). To fullverdige validatorer i to repoer ville
        drevet fra hverandre, og den som ikke er nærmest nøkkelen ville tapt.

        Det som gjøres her, gjøres fordi feilen ellers kommer som en naken
        HTTP 400 tilbake fra Roret: at `web.base.url` fortsatt står på
        Odoo-installasjonens default er den desidert vanligste årsaken, og den
        brukeren selv kan rette.
        """
        url = (self.env['ir.config_parameter'].sudo().get_param(
            'web.base.url', '') or '').strip().rstrip('/')
        if not url.startswith('https://'):
            raise UserError(_(
                "Adressen til denne Odoo-en (web.base.url) er «%(url)s». Roret "
                "må kunne nå den utenfra over https for å hente data på dine "
                "vegne. Sett den til den adressen dere faktisk bruker, i "
                "Innstillinger → Teknisk → Systemparametere.",
                url=url or "tom",
            ))
        return url

    # ------------------------------------------------------------------
    # Handlinger
    # ------------------------------------------------------------------

    def action_koble(self):
        """Mint nøkkel, send den til Roret, og rydd bort den gamle.

        Rekkefølgen er hele sikkerheten i metoden:

        1. **Valider oppsettet FØRST.** En feil i `web.base.url` skal ikke ha
           brent paringskoden — brukeren måtte da bedt agenten om en ny for en
           feil hen selv kan rette.
        2. **Mint FØR POST.** Vi har ingenting å sende ellers.
        3. **Fjern den nye nøkkelen hvis POSTen feiler.** Uten dette ligger en
           aktiv RPC-legitimasjon igjen i kundens base som ingen holder — vi
           kastet klarteksten. Ryddingen er EKSPLISITT og ikke overlatt til at
           transaksjonen rulles tilbake: kalles metoden fra en server action
           eller en annen metode som fanger `UserError`, skjer ingen rollback.
        4. **Fjern den GAMLE nøkkelen helt til slutt.** Det er rotasjon gjort
           riktig, og samme råd Odoo-kjernen selv gir (`generate`-docstringen:
           «generate the new one, store it, and then call revoke on the
           previous one»). Feiler POSTen, står den gamle koblingen fortsatt og
           virker.

        **Ett unntak, og det er iboende:** ryker forbindelsen ETTER at Roret
        har behandlet forespørselen men før svaret kommer tilbake, har Roret
        lagret den nye nøkkelen mens vi fjerner den lokalt og beholder den
        gamle. Roret kjenner da en nøkkel som ikke finnes i basen, og basen har
        en Roret ikke kjenner — koblingen er død, og paringskoden er brukt opp.
        Det er en to-fasers-commit-svakhet ingen enkelt side kan lukke.
        Gjenopprettingen er å be agenten om en ny kode og koble til på nytt.
        (Review-funn på #227.)
        """
        self.ensure_one()
        kode = (self.paringskode or '').strip()
        if not kode:
            raise UserError(_(
                "Lim inn paringskoden du fikk fra Roret-agenten."))

        endepunkt = self._endepunkt()
        odoo_url = self._odoo_url()
        gamle = self._mine_roret_nokler()

        nokkel, ny = self._mint_nokkel()
        try:
            self._send_til_roret(
                endepunkt, kode=kode, odoo_url=odoo_url, api_key=nokkel)
        except Exception:
            # Bredt med vilje: ALT som går galt etter mintingen — nettverk,
            # HTTP-feil, en uventet bug her — skal etterlate basen som før.
            ny._remove()
            raise

        gamle._remove()
        _logger.info(
            "Roret-kobling opprettet for '%s' (#%s)",
            self.env.user.login, self.env.uid)
        return self._kvittering(_(
            "Odoo er koblet til Roret. Agenten kan nå jobbe i denne "
            "Odoo-en som deg — og bare det du selv har tilgang til."))

    def action_koble_fra(self):
        """Fjern brukerens Roret-nøkkel.

        Dette dreper koblingen umiddelbart og fullstendig: Roret har fortsatt
        en kryptert kopi av nøkkelen, men en nøkkel som ikke finnes i denne
        basen autentiserer ingenting.

        **Denne knappen eier bare sin egen halvdel.** Raden hos Roret —
        ciphertext, URL og brukernavn — tømmes av `koble_fra_odoo` i
        MCP-en, som autoriseres av brukerens eget Keycloak-token (#226).
        Raden slettes ikke: rollen har `UPDATE`, ikke `DELETE`.
        Knappen her kan ikke gjøre det selv: den har ingenting å bevise med
        hvem den er, og en sletting som bare tok imot en bruker-id ville latt
        hvem som helst koble fra hvem som helst.

        Kvitteringen sier derfor fra om den andre halvdelen, slik
        `koble_fra_odoo` sier fra om denne.
        """
        self.ensure_one()
        nokler = self._mine_roret_nokler()
        if not nokler:
            raise UserError(_(
                "Du har ingen aktiv Roret-kobling å koble fra."))
        nokler._remove()
        _logger.info(
            "Roret-kobling fjernet for '%s' (#%s)",
            self.env.user.login, self.env.uid)
        return self._kvittering(_(
            "Roret-nøkkelen din er slettet. Agenten kommer ikke lenger inn i "
            "denne Odoo-en på dine vegne.\n\n"
            "Roret kan fortsatt ha en kryptert kopi av den døde nøkkelen "
            "lagret. Be agenten «koble fra Odoo» for å fjerne den også."))

    # ------------------------------------------------------------------
    # Innmat
    # ------------------------------------------------------------------

    def _mint_nokkel(self):
        """Utsted en nøkkel for den innloggede brukeren. (klartekst, record)

        **`sudo()` her endrer IKKE hvem nøkkelen tilhører.** `_generate` binder
        raden til `self.env.user.id` (`res_users.py:1610`), og `sudo()` setter
        bare `su`-flagget — `uid` er uendret. Nøkkelen tilhører altså mennesket
        som klikket, som er hele revisjonspoenget i #199.

        Det `sudo()` gjør, er å slippe oss forbi `_check_expiration_date`, som
        returnerer tidlig når `env.is_system()` (`su or user._is_system()`).
        Uten det MÅ nøkkelen ha en utløpsdato, og den kan ikke gå lenger enn
        `max(gruppenes api_key_duration)` — `base.group_user` setter 90 dager
        (`base_groups.xml:45`), så enhver intern bruker treffer det taket.

        En kobling som dør etter 90 dager er verre enn en som dør med én gang:
        den virker gjennom hele innføringen, og slutter å virke et kvartal
        senere, når ingen lenger forbinder feilen med oppsettet.

        Prisen er at gruppepolicyen for nøkkellevetid ikke gjelder her. Det er
        akseptabelt fordi brukeren aldri får se nøkkelen: den går til den
        adressen administratoren har satt, og ingen andre steder. Den som vil
        ha den bort, trykker «Koble fra».

        Kjernens `base.enable_programmatic_api_keys`-sperre gjelder ikke oss —
        `_ensure_can_manage_keys_programmatically()` kalles kun fra de
        RPC-eksponerte `generate()`/`revoke()` (`res_users.py:1653`, `1693`),
        ikke fra interne `_generate()`. Kunden slipper å skru på noe.
        """
        apikeys = self.env['res.users.apikeys'].sudo()
        for_minting = apikeys.search([('user_id', '=', self.env.uid)])
        # `expiration_date=None` = permanent. Se over.
        nokkel = apikeys._generate(NOKKELSCOPE, NOKKELNAVN, None)
        # `_generate` returnerer klarteksten, ikke raden — den gjør en rå
        # INSERT. Differansen mot settet vi så rett før er den nye raden, uten
        # å måtte gjette på «nyeste id».
        ny = apikeys.search([('user_id', '=', self.env.uid)]) - for_minting
        if len(ny) != 1:
            # Skal ikke kunne skje. Skjer det, er alternativet å returnere en
            # nøkkel vi ikke kan rydde bort igjen ved feil.
            raise UserError(_(
                "Klarte ikke å identifisere den nye API-nøkkelen. Ingenting er "
                "sendt til Roret. Prøv igjen."))
        return nokkel, ny

    def _kan_oppdage_db(self):
        """Skal Roret slå opp databasenavnet i runtime i stedet for å stole
        på det vi sender nå?

        **På Odoo.sh endres databasenavnet ved hver rebuild.** Sendte vi bare
        `cr.dbname`, ville koblingen sluttet å virke ved neste rebuild — for
        alle brukere samtidig, uten at noen hadde rørt noe. CLI-en har
        `--discover` av nøyaktig den grunnen; knappen manglet det.

        Roret oppdager navnet via `/eristo/env-info`, som serveres av
        `l10n_no_eristo_base`. Denne modulen avhenger med vilje kun av `base`
        og kan stå alene, så endepunktet finnes ikke nødvendigvis. Vi slår
        derfor på oppdagelse KUN når modulen faktisk er installert:

        * installert  → Roret spør endepunktet og overlever rebuilds. På en
          selvhostet instans med stabilt navn svarer det samme navn hver
          gang, altså ufarlig.
        * ikke installert → hvert kalde oppslag hos Roret ville brukt en
          bomtur mot en rute som ikke finnes. Navnet vi sender er da det
          eneste vi har, og Roret bruker det.

        Oppdagelsen er uansett feiltolerant i Roret-enden: svarer ikke
        endepunktet, faller den tilbake på navnet vi sendte. Derfor sendes
        `odoo_database` ALLTID, også når dette er sant — det er fallbacken.
        """
        return bool(self.env['ir.module.module'].sudo().search_count([
            ('name', '=', 'l10n_no_eristo_base'),
            ('state', '=', 'installed'),
        ]))

    def _send_til_roret(self, endepunkt, *, kode, odoo_url, api_key):
        """POST /onboard. Kroppen logges ALDRI — den bærer nøkkelen."""
        try:
            svar = requests.post(
                f"{endepunkt}/onboard",
                json={
                    'kode': kode,
                    'odoo_url': odoo_url,
                    'odoo_username': self.env.user.login,
                    'odoo_api_key': api_key,
                    'odoo_database': self.env.cr.dbname,
                    'odoo_discover_database': self._kan_oppdage_db(),
                },
                timeout=TIDSAVBRUDD,
                # INGEN redirects. https-kravet i `_endepunkt` gjelder bare den
                # FØRSTE URL-en; på 307/308 replayer `requests` metode OG kropp
                # mot en vilkårlig ny adresse — også `http://`. Kroppen her ER
                # nøkkelen. (`requests` fjerner `Authorization`-headeren ved
                # kryssvert-redirect, men den beskyttelsen finnes ikke for en
                # hemmelighet som ligger i kroppen, og det er vårt tilfelle.)
                #
                # Kontrakten fra #199 svarer 200 direkte, så det finnes ingen
                # legitim redirect å følge. (Review-funn på #227.)
                allow_redirects=False,
            )
        except requests.RequestException as e:
            # `str(e)` fra requests kan gjengi URL-en, men aldri kroppen. Vi
            # tar likevel bare typen med videre til brukeren.
            raise UserError(_(
                "Fikk ikke kontakt med Roret på %(url)s (%(feil)s). Ingen "
                "nøkkel er sendt. Sjekk at serveren er tilgjengelig og prøv "
                "igjen.",
                url=endepunkt, feil=type(e).__name__,
            )) from e

        if 200 <= svar.status_code < 300:
            return

        # Ikke `svar.ok`: den er sann for 3xx også, og med `allow_redirects=
        # False` er en 3xx nettopp det vi nekter å følge. Å behandle den som
        # suksess ville vært å lagre en nøkkel ingen har tatt imot.
        #
        # Og ikke `== 200`: byttet ruten til 201, ville denne grenen slettet en
        # nøkkel serveren ALLEREDE har lagret. Det er den ene retningen
        # feilhåndteringen ikke tåler — vi ville stått igjen med en død kobling
        # og en oppbrukt paringskode. (Review-funn på #227.)

        if svar.status_code == 403:
            raise UserError(_(
                "Paringskoden ble ikke godtatt. Den er kortlevd og kan bare "
                "brukes én gang — be agenten om en ny og prøv igjen."))
        if svar.status_code == 429:
            raise UserError(_(
                "Roret har stoppet flere forsøk fra denne serveren. Vent noen "
                "minutter og prøv igjen."))

        # Alt annet: ta med Rorets egen forklaring hvis den finnes. Den er
        # skrevet for å kunne vises — feltnavn, aldri verdier.
        detalj = ''
        try:
            detalj = (svar.json() or {}).get('feil', '')
        except ValueError:
            pass
        raise UserError(_(
            "Roret avviste tilkoblingen (HTTP %(kode)s)%(detalj)s",
            kode=svar.status_code,
            detalj=f": {detalj}" if detalj else ".",
        ))

    def _kvittering(self, melding):
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _("Roret"),
                'message': melding,
                'type': 'success',
                'sticky': False,
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }
