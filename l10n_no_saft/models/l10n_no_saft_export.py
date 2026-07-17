"""SAF-T Financial-eksport (Norwegian SAF-T Financial data v1.30).

Lovpålagt eksportformat (bokføringsforskriften § 7-8): bokføringspliktige
skal kunne levere regnskapsdata som SAF-T Financial på forespørsel fra
Skatteetaten. Fila lastes opp i Altinn (RF-1363) — ingen API-innsending.

Bygget test-først mot Skatteetatens offisielle XSD v1.30 (committet under
schemas/, hentet fra github.com/Skatteetaten/saf-t) — testene XSD-validerer
hele fila. Kontogruppering (GroupingCategory/GroupingCode) mappes mot
næringsspesifikasjonens standardkoder (data/naeringsspesifikasjon_
grouping.csv fra samme repo): eksakt 4-siffer-match, ellers nærmeste
lavere kode innenfor samme kontogruppe (flagges i loggen for review).

v1-avgrensninger (ikke påkrevd av XSD-en, notert i PORTING_BACKLOG):
  - SourceDocuments, Assets, Products/lager og Analysis utelates
  - Kun NOK-beløp (valutabeløp emitteres med CurrencyCode/CurrencyAmount)
"""
import base64
import csv
import logging
import os
from collections import defaultdict

from lxml import etree

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.modules import get_module_path

_logger = logging.getLogger(__name__)

NS = 'urn:StandardAuditFile-Taxation-Financial:NO'
AUDIT_FILE_VERSION = '1.30'

# Standard mva-koder (Skatteetaten Standard Tax Codes) — fallback når
# skattegrid-taggen ikke kan parses: (type_tax_use, sats) → kode.
_FALLBACK_STD_TAX = {
    ('sale', 25.0): '3', ('sale', 15.0): '31', ('sale', 12.0): '33',
    ('sale', 0.0): '5',
    ('purchase', 25.0): '1', ('purchase', 15.0): '11',
    ('purchase', 12.0): '13', ('purchase', 0.0): '0',
}


def _load_grouping_codes():
    """Last (GroupingCategory, GroupingCode)-parene fra Skatteetatens
    næringsspesifikasjons-CSV. Returnerer {kode: kategori}."""
    path = os.path.join(
        get_module_path('l10n_no_saft'), 'data',
        'naeringsspesifikasjon_grouping.csv')
    mapping = {}
    with open(path, encoding='utf-8-sig') as f:
        for row in csv.DictReader(f, delimiter=';'):
            code = (row.get('GroupingCode') or '').strip()
            cat = (row.get('GroupingCategory') or '').strip()
            if code.isdigit() and cat:
                mapping[code] = cat
    return mapping


_GROUPING_CODES = None


def _grouping_codes():
    global _GROUPING_CODES
    if _GROUPING_CODES is None:
        _GROUPING_CODES = _load_grouping_codes()
    return _GROUPING_CODES


def _fmt(amount):
    """SAFmonetaryType: to desimaler, punktum."""
    return '%.2f' % (round(amount + 0.0, 2) + 0.0)


class L10nNoSaftExport(models.Model):
    _name = 'l10n.no.saft.export'
    _description = 'SAF-T Financial-eksport'
    _inherit = ['mail.thread']
    _order = 'id desc'

    name = fields.Char(compute='_compute_name', store=True)
    company_id = fields.Many2one(
        'res.company', required=True, default=lambda self: self.env.company)
    date_from = fields.Date(string="Fra dato", required=True)
    date_to = fields.Date(string="Til dato", required=True)
    kontakt_fornavn = fields.Char(
        string="Kontaktperson fornavn", required=True,
        default=lambda self: (self.env.user.name or '').split(' ')[0])
    kontakt_etternavn = fields.Char(
        string="Kontaktperson etternavn", required=True,
        default=lambda self: (self.env.user.name or '').split(' ')[-1])
    state = fields.Selection(
        [('draft', 'Utkast'), ('generated', 'Generert')],
        default='draft', required=True, copy=False, tracking=True)
    saft_file = fields.Binary(string="SAF-T-fil", copy=False, readonly=True)
    saft_filename = fields.Char(copy=False, readonly=True)
    generated_at = fields.Datetime(copy=False, readonly=True)

    @api.depends('company_id', 'date_from', 'date_to')
    def _compute_name(self):
        for rec in self:
            rec.name = "SAF-T %s %s–%s" % (
                rec.company_id.name or '?', rec.date_from or '?',
                rec.date_to or '?')

    # ------------------------------------------------------------------
    # Hjelpere
    # ------------------------------------------------------------------

    def _orgnr(self):
        company = self.company_id
        orgnr = (company.company_registry or '').replace(' ', '')
        if not orgnr and company.vat:
            digits = ''.join(c for c in company.vat if c.isdigit())
            orgnr = digits[:9]
        if not orgnr:
            raise UserError(_(
                "Selskapet %(c)s mangler organisasjonsnummer "
                "(Company Registry).", c=company.name))
        return orgnr

    @api.model
    def _grouping_for_account(self, code):
        """(GroupingCategory, GroupingCode) for en kontokode.

        Eksakt 4-siffer-match mot næringsspesifikasjonen, ellers nærmeste
        lavere kode i samme 2-siffer-gruppe, ellers i samme 1-siffer-
        gruppe. Flagges i loggen når matchen er heuristisk."""
        codes = _grouping_codes()
        code4 = (code or '')[:4].ljust(4, '0')
        if code4 in codes:
            return codes[code4], code4
        candidates = sorted(c for c in codes if c < code4)
        for prefix_len in (2, 1):
            prefix = code4[:prefix_len]
            group = [c for c in candidates if c.startswith(prefix)]
            if group:
                match = group[-1]
                _logger.info(
                    "SAF-T: konto %s mappet heuristisk til "
                    "grupperingskode %s — verifiser.", code, match)
                return codes[match], match
        _logger.warning(
            "SAF-T: fant ingen grupperingskode for konto %s — bruker "
            "koden selv. Verifiser mot næringsspesifikasjonen.", code)
        return 'annet', code4

    def _std_tax_code(self, tax):
        """Standard mva-kode for en account.tax: parse skattegrid-taggen
        ('3 Base' → '3'); fallback på sats/type."""
        for line in tax.invoice_repartition_line_ids:
            for tag in line.tag_ids:
                token = (tag.name or '').lstrip('-+').split(' ')[0]
                if token.isdigit():
                    return token
        return _FALLBACK_STD_TAX.get(
            (tax.type_tax_use, tax.amount), '0')

    def _move_lines_domain(self, up_to_only=False):
        domain = [
            ('company_id', '=', self.company_id.id),
            ('parent_state', '=', 'posted'),
            ('date', '<=', self.date_to),
            # Seksjons-/notatlinjer har ingen konto — uten filteret gir
            # _read_group en tom kontogruppe som ble til AccountID 'False'
            # i MasterFiles (funnet ved validering mot ekte produksjonsdata)
            ('account_id', '!=', False),
        ]
        if not up_to_only:
            domain.append(('date', '>=', self.date_from))
        return domain

    @staticmethod
    def _sub(parent, tag, text=None):
        el = etree.SubElement(parent, '{%s}%s' % (NS, tag))
        if text is not None:
            el.text = str(text)
        return el

    def _emit_balance_pair(self, parent, prefix, amount):
        """Emit <{prefix}DebitBalance> eller <{prefix}CreditBalance>
        (XSD-choice) fra en signert saldo (debet positiv)."""
        if amount >= 0:
            self._sub(parent, '%sDebitBalance' % prefix, _fmt(amount))
        else:
            self._sub(parent, '%sCreditBalance' % prefix, _fmt(-amount))

    def _emit_amount(self, parent, tag, amount, currency=None,
                     currency_amount=None):
        el = self._sub(parent, tag)
        self._sub(el, 'Amount', _fmt(amount))
        if currency is not None and currency != 'NOK':
            self._sub(el, 'CurrencyCode', currency)
            self._sub(el, 'CurrencyAmount', _fmt(currency_amount or 0.0))
        return el

    # ------------------------------------------------------------------
    # Generering
    # ------------------------------------------------------------------

    def action_generate(self):
        self.ensure_one()
        orgnr = self._orgnr()
        company = self.company_id

        root = etree.Element('{%s}AuditFile' % NS, nsmap={None: NS})
        self._build_header(root, orgnr)
        self._build_master_files(root)
        self._build_general_ledger_entries(root)

        xml_bytes = etree.tostring(
            root, xml_declaration=True, encoding='utf-8',
            pretty_print=True)
        filename = 'SAF-T Financial_%s_%s.xml' % (
            orgnr, fields.Datetime.now().strftime('%Y%m%d%H%M%S'))
        self.write({
            'saft_file': base64.b64encode(xml_bytes),
            'saft_filename': filename,
            'state': 'generated',
            'generated_at': fields.Datetime.now(),
        })
        self.message_post(body=_(
            "SAF-T Financial generert for %(fra)s–%(til)s (%(navn)s).",
            fra=self.date_from, til=self.date_to, navn=filename))
        return True

    def _build_header(self, root, orgnr):
        company = self.company_id
        module = self.env['ir.module.module'].sudo().search(
            [('name', '=', 'l10n_no_saft')], limit=1)
        header = self._sub(root, 'Header')
        self._sub(header, 'AuditFileVersion', AUDIT_FILE_VERSION)
        self._sub(header, 'AuditFileCountry', 'NO')
        self._sub(header, 'AuditFileDateCreated',
                  fields.Date.context_today(self).isoformat())
        self._sub(header, 'SoftwareCompanyName', 'Eristo AS')
        self._sub(header, 'SoftwareID', 'Roret (Odoo Community)')
        self._sub(header, 'SoftwareVersion',
                  module.installed_version or '19.0.1.0.0')

        comp = self._sub(header, 'Company')
        self._sub(comp, 'RegistrationNumber', orgnr)
        self._sub(comp, 'Name', company.name)
        addr = self._sub(comp, 'Address')
        if company.street:
            self._sub(addr, 'StreetName', company.street)
        self._sub(addr, 'City', company.city or '-')
        self._sub(addr, 'PostalCode', company.zip or '-')
        self._sub(addr, 'Country',
                  company.country_id.code or 'NO')
        contact = self._sub(comp, 'Contact')
        person = self._sub(contact, 'ContactPerson')
        self._sub(person, 'FirstName', self.kontakt_fornavn)
        self._sub(person, 'LastName', self.kontakt_etternavn)
        if company.phone:
            self._sub(contact, 'Telephone', company.phone[:35])
        if company.email:
            self._sub(contact, 'Email', company.email)
        tax_reg = self._sub(comp, 'TaxRegistration')
        self._sub(tax_reg, 'TaxRegistrationNumber', '%sMVA' % orgnr)
        self._sub(tax_reg, 'TaxAuthority', 'Skatteetaten')

        self._sub(header, 'DefaultCurrencyCode', 'NOK')
        sel = self._sub(header, 'SelectionCriteria')
        self._sub(sel, 'SelectionStartDate', self.date_from.isoformat())
        self._sub(sel, 'SelectionEndDate', self.date_to.isoformat())
        self._sub(header, 'TaxAccountingBasis', 'A')

    def _account_balances(self):
        """{account: (aapning, lukking)} — kumulative saldoer (debet+)."""
        Aml = self.env['account.move.line']
        opening = defaultdict(float)
        closing = defaultdict(float)
        groups = Aml._read_group(
            domain=[
                ('company_id', '=', self.company_id.id),
                ('parent_state', '=', 'posted'),
                ('date', '<', self.date_from),
                ('account_id', '!=', False),  # jf. _move_lines_domain
            ],
            groupby=['account_id'], aggregates=['balance:sum'])
        for account, total in groups:
            opening[account] += total
            closing[account] += total
        groups = Aml._read_group(
            domain=self._move_lines_domain(),
            groupby=['account_id'], aggregates=['balance:sum'])
        for account, total in groups:
            closing[account] += total
        return opening, closing

    def _partner_balances(self, account_types):
        """{partner: (aapning, lukking)} over reskontro-kontotyper."""
        Aml = self.env['account.move.line']
        base = [
            ('company_id', '=', self.company_id.id),
            ('parent_state', '=', 'posted'),
            ('account_id.account_type', 'in', account_types),
            ('partner_id', '!=', False),
        ]
        opening = defaultdict(float)
        closing = defaultdict(float)
        for partner, total in Aml._read_group(
                domain=base + [('date', '<', self.date_from)],
                groupby=['partner_id'], aggregates=['balance:sum']):
            opening[partner] += total
            closing[partner] += total
        for partner, total in Aml._read_group(
                domain=base + [('date', '>=', self.date_from),
                               ('date', '<=', self.date_to)],
                groupby=['partner_id'], aggregates=['balance:sum']):
            closing[partner] += total
        return opening, closing

    def _build_master_files(self, root):
        company = self.company_id
        master = self._sub(root, 'MasterFiles')

        # --- Kontoplan med saldoer
        opening, closing = self._account_balances()
        accounts = sorted(
            set(opening) | set(closing),
            key=lambda a: a.with_company(company).code or '')
        gl = self._sub(master, 'GeneralLedgerAccounts')
        for account in accounts:
            code = account.with_company(company).code or str(account.id)
            acc_el = self._sub(gl, 'Account')
            self._sub(acc_el, 'AccountID', code)
            self._sub(acc_el, 'AccountDescription', account.name or '-')
            category, gcode = self._grouping_for_account(code)
            self._sub(acc_el, 'GroupingCategory', category)
            self._sub(acc_el, 'GroupingCode', gcode)
            self._sub(acc_el, 'AccountType', 'GL')
            self._emit_balance_pair(acc_el, 'Opening', opening.get(account, 0.0))
            self._emit_balance_pair(acc_el, 'Closing', closing.get(account, 0.0))

        # --- Kunder / leverandører med reskontro-saldoer
        def party_block(container_tag, item_tag, id_tag, account_types,
                        default_account):
            p_opening, p_closing = self._partner_balances(account_types)
            partners = sorted(
                set(p_opening) | set(p_closing), key=lambda p: p.id)
            if not partners:
                return
            container = self._sub(master, container_tag)
            for partner in partners:
                el = self._sub(container, item_tag)
                if partner.company_registry or partner.vat:
                    self._sub(el, 'RegistrationNumber',
                              (partner.company_registry
                               or ''.join(c for c in partner.vat
                                          if c.isdigit())[:9]))
                self._sub(el, 'Name', partner.display_name)
                addr = self._sub(el, 'Address')
                if partner.street:
                    self._sub(addr, 'StreetName', partner.street)
                self._sub(addr, 'City', partner.city or '-')
                self._sub(addr, 'PostalCode', partner.zip or '-')
                self._sub(addr, 'Country', partner.country_id.code or 'NO')
                self._sub(el, id_tag, partner.id)
                bal = self._sub(el, 'BalanceAccount')
                if default_account:
                    self._sub(bal, 'AccountID', default_account)
                self._emit_balance_pair(
                    bal, 'Opening', p_opening.get(partner, 0.0))
                self._emit_balance_pair(
                    bal, 'Closing', p_closing.get(partner, 0.0))

        receivable = self.env['account.account'].search([
            ('company_ids', 'in', company.id),
            ('account_type', '=', 'asset_receivable')], limit=1)
        payable = self.env['account.account'].search([
            ('company_ids', 'in', company.id),
            ('account_type', '=', 'liability_payable')], limit=1)
        party_block('Customers', 'Customer', 'CustomerID',
                    ['asset_receivable'],
                    receivable.with_company(company).code)
        party_block('Suppliers', 'Supplier', 'SupplierID',
                    ['liability_payable'],
                    payable.with_company(company).code)

        # --- Mva-tabell
        taxes = self.env['account.tax'].search([
            ('company_id', '=', company.id),
        ])
        if taxes:
            table = self._sub(master, 'TaxTable')
            entry = self._sub(table, 'TaxTableEntry')
            self._sub(entry, 'TaxType', 'MVA')
            self._sub(entry, 'Description', 'Merverdiavgift')
            for tax in taxes:
                det = self._sub(entry, 'TaxCodeDetails')
                self._sub(det, 'TaxCode', self._std_tax_code(tax))
                self._sub(det, 'Description', tax.name)
                self._sub(det, 'TaxPercentage', '%.2f' % tax.amount)
                self._sub(det, 'Country', 'NO')
                self._sub(det, 'StandardTaxCode', self._std_tax_code(tax))
                self._sub(det, 'BaseRate', '100')

    def _build_general_ledger_entries(self, root):
        company = self.company_id
        moves = self.env['account.move'].search([
            ('company_id', '=', company.id),
            ('state', '=', 'posted'),
            ('date', '>=', self.date_from),
            ('date', '<=', self.date_to),
        ], order='journal_id, date, id')

        total_debit = sum(moves.line_ids.mapped('debit'))
        total_credit = sum(moves.line_ids.mapped('credit'))

        gle = self._sub(root, 'GeneralLedgerEntries')
        self._sub(gle, 'NumberOfEntries', len(moves))
        self._sub(gle, 'TotalDebit', _fmt(total_debit))
        self._sub(gle, 'TotalCredit', _fmt(total_credit))

        by_journal = defaultdict(list)
        for move in moves:
            by_journal[move.journal_id].append(move)

        for journal in sorted(by_journal, key=lambda j: j.code or ''):
            j_el = self._sub(gle, 'Journal')
            self._sub(j_el, 'JournalID', journal.code or str(journal.id))
            self._sub(j_el, 'Description', journal.name)
            self._sub(j_el, 'Type', 'GL')
            for move in by_journal[journal]:
                self._build_transaction(j_el, move)

    def _build_transaction(self, j_el, move):
        company = self.company_id
        t = self._sub(j_el, 'Transaction')
        self._sub(t, 'TransactionID', move.name)
        self._sub(t, 'Period', move.date.month)
        self._sub(t, 'PeriodYear', move.date.year)
        self._sub(t, 'TransactionDate', move.date.isoformat())
        self._sub(t, 'Description',
                  (move.ref or move.name or '-')[:250])
        self._sub(t, 'SystemEntryDate',
                  (move.create_date or fields.Datetime.now())
                  .date().isoformat())
        self._sub(t, 'GLPostingDate', move.date.isoformat())

        for line in move.line_ids.filtered(
                lambda l: l.display_type not in
                ('line_section', 'line_note')):
            self._build_line(t, move, line)

    def _build_line(self, t, move, line):
        company = self.company_id
        el = self._sub(t, 'Line')
        self._sub(el, 'RecordID', line.id)
        self._sub(el, 'AccountID',
                  line.account_id.with_company(company).code
                  or str(line.account_id.id))
        if line.partner_id and line.account_id.account_type \
                == 'asset_receivable':
            self._sub(el, 'CustomerID', line.partner_id.id)
        if line.partner_id and line.account_id.account_type \
                == 'liability_payable':
            self._sub(el, 'SupplierID', line.partner_id.id)
        self._sub(el, 'Description', (line.name or move.name or '-')[:250])

        currency = line.currency_id.name \
            if line.currency_id != company.currency_id else None
        if line.debit or not line.credit:
            self._emit_amount(el, 'DebitAmount', line.debit,
                              currency, abs(line.amount_currency))
        else:
            self._emit_amount(el, 'CreditAmount', line.credit,
                              currency, abs(line.amount_currency))

        if line.tax_line_id:
            info = self._sub(el, 'TaxInformation')
            self._sub(info, 'TaxType', 'MVA')
            self._sub(info, 'TaxCode', self._std_tax_code(line.tax_line_id))
            self._sub(info, 'TaxPercentage',
                      '%.2f' % line.tax_line_id.amount)
            self._sub(info, 'Country', 'NO')
            if line.tax_base_amount:
                self._sub(info, 'TaxBase', _fmt(abs(line.tax_base_amount)))
            if line.debit:
                self._emit_amount(info, 'DebitTaxAmount', line.debit)
            else:
                self._emit_amount(info, 'CreditTaxAmount', line.credit)

        if move.ref:
            self._sub(el, 'ReferenceNumber', move.ref[:35])
        kid = (move.payment_reference or '').replace(' ', '')
        if kid.isdigit() and line.account_id.account_type in (
                'asset_receivable', 'liability_payable'):
            self._sub(el, 'CID', kid)
        if line.date_maturity:
            self._sub(el, 'DueDate', line.date_maturity.isoformat())
