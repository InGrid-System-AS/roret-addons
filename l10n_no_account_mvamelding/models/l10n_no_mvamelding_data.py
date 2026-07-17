"""Datauttrekk fra Odoos norske Tax Report → mva-melding-linjer.

Den store gevinsten: Odoos Tax Report (account.report ``l10n_no.tax_report``)
har allerede rapportlinjer kodet med Skatteetatens standard mva-koder. Hver
linje har et expression med en stabil ``code`` (``BASE_3``, ``TAX_3``,
``TAX_1`` …) og engine ``tax_tags``. Vi kjører Odoos egen rapport-motor for
terminens datointervall og leser verdiene — da matcher tallene nøyaktig det
brukeren ser i Tax Report-UIet (kilden de kopierer fra manuelt i dag).

Fortegn (verifisert mot rapportens expression-formler + "Tax to pay"-
aggregeringen 2026-06-05):

  * Salg/utgående (3,31,32,33,5,6,51,52): formel ``-N Base``/``-N Tax`` →
    rapportverdien er POSITIV (grunnlag + utgående mva positivt).
  * Fradrag/inngående (1,11,12,13,14,15): formel ``N Tax`` → rapportverdien
    er POSITIV (fradragsberettiget inngående mva). I "Tax to pay" TREKKES
    disse fra, så i mva-meldingen får de NEGATIV merverdiavgift.
  * Import/omvendt avgiftsplikt (81–92, 85): formel ``N Base``/``N Tax`` →
    positiv, og LEGGES TIL i "Tax to pay" (utgående-side).

Dermed: fastsattMerverdiavgift == Σ(linjenes merverdiavgift), som er
nøyaktig Odoo-rapportens "Tax to pay"-linje. Vi beregner fastsatt som
summen av de (avrundede) emitterte linjene slik at meldingen alltid er
internt konsistent (Skatteetaten validerer fastsatt mot linjesummen).

Avgrensning v1: import/omvendt-avgiftsplikt-koder (81/83/86/88/91) med
fradragsrett rapporteres som én utgående linje hver (slik Odoos "Tax to
pay" behandler dem), ikke splittet i utgående + fradrag slik enkelte
Skatteetaten-eksempler viser. For InGrid/Eristo brukes i praksis kun kode
3 og 1; eventuelle avvik fanges synkront i valideringssteget.
"""
import logging
from decimal import ROUND_HALF_UP, Decimal

from odoo import _, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

_TAX_REPORT_XMLID = 'l10n_no.tax_report'

# mvaKode → (kind, sats). kind styrer hvilke felt linjen får:
#   'output'    → grunnlag (BASE_n) + sats + merverdiavgift (+TAX_n)
#   'deduction' → kun merverdiavgift (-TAX_n), ingen grunnlag/sats
#   'zerorate'  → grunnlag (BASE_n) + sats '0' + merverdiavgift 0
# Sats som streng med komma-desimal (Skatteetatens Sats-kodeliste).
_MVA_KODER = {
    '3':  ('output', '25'),
    '31': ('output', '15'),
    '32': ('output', '11,11'),
    '33': ('output', '12'),
    '5':  ('zerorate', '0'),
    '6':  ('zerorate', '0'),
    '51': ('zerorate', '0'),
    '52': ('zerorate', '0'),
    '85': ('zerorate', '0'),
    '1':  ('deduction', None),
    '11': ('deduction', None),
    '12': ('deduction', None),
    '13': ('deduction', None),
    '14': ('deduction', None),
    '15': ('deduction', None),
    '81': ('output', '25'),
    '82': ('output', '25'),
    '83': ('output', '15'),
    '84': ('output', '15'),
    '86': ('output', '25'),
    '87': ('output', '25'),
    '88': ('output', '12'),
    '89': ('output', '12'),
    '91': ('output', '25'),
    '92': ('output', '25'),
}

# Rekkefølge på mvaKode i XML — stigende numerisk (lesbarhet; rekkefølgen
# på mvaSpesifikasjonslinje er ikke XSD-bundet, men deterministisk output
# gjør testing/diff enklere).
_MVA_KODE_ORDER = sorted(_MVA_KODER, key=lambda c: int(c))


def _nok(value):
    """Avrund til hele kroner (ROUND_HALF_UP) og returner int."""
    return int(Decimal(str(value or 0.0)).quantize(Decimal('1'), ROUND_HALF_UP))


class L10nNoMvamelding(models.Model):
    _inherit = 'l10n.no.mvamelding'

    def _tax_report_code_values(self):
        """Beregn Tax Report-verdiene for terminen → {code: verdi}.

        Returnerer en dict fra rapportlinje-code (``BASE_3``, ``TAX_1`` …) til
        float-verdi for selskapet i periodens datointervall.

        Community/OCA-port: rapport-DEFINISJONEN (l10n_no.tax_report med
        tax_tags-expressions) ligger i community l10n_no, men beregnings-
        motoren (account_reports) er Enterprise. Vi regner derfor selv, med
        nøyaktig samme semantikk som Odoo 19s tag-modell (verifisert mot
        community account/models/account_account_tag.py 2026-07-10):

          * Én tag per formel-navn: name = formula.lstrip('-'),
            country + applicability='taxes' (account.account.tag,
            _get_tax_tags).
          * balance_negate = formelen starter med '-' → rapportverdi er
            -SUM(balance), ellers +SUM(balance), over posterte
            move-linjer med taggen i datointervallet.

        Fortegns-fasit (samme som Enterprise-UIet): kundefaktura 100 kr @
        25 % kode 3 → inntektslinje balance -100 med tag «3 Base», formel
        «-3 Base» → BASE_3 = +100.
        """
        self.ensure_one()
        report = self.env.ref(_TAX_REPORT_XMLID, raise_if_not_found=False)
        if not report:
            raise UserError(_(
                "Fant ikke norsk Tax Report (%(x)s). Er l10n_no "
                "installert?", x=_TAX_REPORT_XMLID,
            ))
        _element, _value, date_from, date_to = self._periode_spec()
        company = self.company_id
        Tag = self.env['account.account.tag']
        Aml = self.env['account.move.line']

        code_values = {}
        for line in report.line_ids:
            code = line.code
            if not code:
                continue
            exprs = line.expression_ids.filtered(
                lambda e: e.engine == 'tax_tags')
            if not exprs:
                # Aggregerings-linjer (f.eks. «Tax to pay») beregnes ikke
                # her — fastsatt regnes fra de emitterte linjene i stedet
                # (internt konsistent, jf. modul-docstringen).
                continue
            total = 0.0
            for expr in exprs:
                formula = (expr.formula or '').strip()
                if not formula:
                    continue
                tags = Tag._get_tax_tags(formula, report.country_id.id)
                if not tags:
                    _logger.info(
                        "Mvamelding %s: ingen tag for formel %r (kode %s) — "
                        "tolker som 0.", self.id, formula, code)
                    continue
                groups = Aml._read_group(
                    domain=[
                        ('tax_tag_ids', 'in', tags.ids),
                        ('company_id', '=', company.id),
                        ('date', '>=', date_from),
                        ('date', '<=', date_to),
                        ('parent_state', '=', 'posted'),
                    ],
                    aggregates=['balance:sum'],
                )
                raw = groups[0][0] or 0.0
                total += -raw if formula.startswith('-') else raw
            if code in code_values and code_values[code] != total:
                raise UserError(_(
                    "Tax Report har to ulike verdier for kode %(c)s "
                    "(%(a)s vs %(b)s) — tvetydig, avbryter for å unngå feil "
                    "beløp.", c=code, a=code_values[code], b=total))
            code_values[code] = total
        return code_values

    def _collect_mva_lines(self):
        """Bygg mva-melding-linjer fra Tax Report.

        Returnerer ``(lines, fastsatt)`` der ``lines`` er en liste av dicts:
            {'mva_kode': str, 'grunnlag': int|None, 'sats': str|None,
             'merverdiavgift': int}
        og ``fastsatt`` er hele kroner (Σ merverdiavgift).
        """
        self.ensure_one()
        code_values = self._tax_report_code_values()

        lines = []
        for kode in _MVA_KODE_ORDER:
            kind, sats = _MVA_KODER[kode]
            base = _nok(code_values.get('BASE_%s' % kode, 0.0))
            tax = _nok(code_values.get('TAX_%s' % kode, 0.0))

            if kind == 'output':
                if base == 0 and tax == 0:
                    continue
                lines.append({
                    'mva_kode': kode,
                    'grunnlag': base,
                    'sats': sats,
                    'merverdiavgift': tax,
                })
            elif kind == 'deduction':
                if tax == 0:
                    continue
                # Fradrag reduserer skyldig MVA → negativt fortegn.
                lines.append({
                    'mva_kode': kode,
                    'grunnlag': None,
                    'sats': None,
                    'merverdiavgift': -tax,
                })
            else:  # zerorate
                if base == 0:
                    continue
                lines.append({
                    'mva_kode': kode,
                    'grunnlag': base,
                    'sats': sats,
                    'merverdiavgift': 0,
                })

        fastsatt = sum(line['merverdiavgift'] for line in lines)
        return lines, fastsatt
