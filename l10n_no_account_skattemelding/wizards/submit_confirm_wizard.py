"""Custom bekreftelses-wizard for Send inn via Altinn.

Erstatter den statiske confirm-attributtet på knappen med en dynamisk
dialog som viser selskaps-navn, inntektsår og antall veiledninger.
"""
import re

from markupsafe import escape

from odoo import _, api, fields, models


class L10nNoSkattemeldingSubmitConfirm(models.TransientModel):
    _name = 'l10n.no.skattemelding.submit.confirm'
    _description = 'Bekreft innsending av skattemelding'

    skattemelding_id = fields.Many2one(
        'l10n.no.skattemelding',
        required=True,
        ondelete='cascade',
    )
    company_name = fields.Char(related='skattemelding_id.company_id.name')
    inntektsaar = fields.Integer(related='skattemelding_id.inntektsaar')
    partsnummer = fields.Char(related='skattemelding_id.partsnummer')
    is_korreksjon = fields.Boolean(
        compute='_compute_summary',
    )
    veiledning_count = fields.Integer(
        compute='_compute_summary',
        help="Antall ikke-blokkerende veiledninger fra valideringen",
    )
    confirm_html = fields.Html(
        compute='_compute_confirm_html',
        sanitize=False,
    )

    @api.depends('skattemelding_id')
    def _compute_summary(self):
        for w in self:
            sm = w.skattemelding_id
            w.is_korreksjon = bool(sm.erstatter_skattemelding_id)
            # Tell merknadStandard-veiledninger fra last_response
            count = 0
            body = sm.last_response or ''
            if isinstance(body, str) and '<veiledning' in body:
                # Tell veiledninger med betjeningsstrategi != faktiskFeil
                veiledninger = re.findall(
                    r'<(?:[a-zA-Z0-9]+:)?veiledning(?:\s[^>]*)?>(.*?)'
                    r'</(?:[a-zA-Z0-9]+:)?veiledning>',
                    body, re.DOTALL,
                )
                for v in veiledninger:
                    if 'faktiskFeil' not in v:
                        count += 1
            w.veiledning_count = count

    @api.depends('skattemelding_id', 'is_korreksjon', 'veiledning_count')
    def _compute_confirm_html(self):
        """Bygg HTML-bekreftelse for to-stegs innsending.

        P1 #9: alle dynamiske verdier (selskapsnavn, orgnr, inntektsår,
        partsnummer, antall veiledninger) escapes via markupsafe.escape()
        før innsetting i HTML. Selve HTML-strukturen er statisk.
        """
        for w in self:
            sm = w.skattemelding_id
            # Escape alle dynamiske verdier — beskytter mot XSS hvis et
            # selskapsnavn skulle inneholde <script> e.l.
            company_name = escape(sm.company_id.name or '')
            company_vat = escape(sm.company_id.vat or '–')
            inntektsaar = escape(str(sm.inntektsaar or ''))
            partsnummer = escape(sm.partsnummer or '–')
            veiledning_count = escape(str(w.veiledning_count or 0))

            korreksjon_note = ''
            if w.is_korreksjon:
                korreksjon_note = (
                    '<div class="alert alert-warning" role="alert" '
                    'style="margin-top:8px;">'
                    '<strong>Korreksjons-innsending:</strong> Denne erstatter '
                    'tidligere innsending. Skatteetaten arkiverer den gamle '
                    'og bruker denne i stedet.'
                    '</div>'
                )
            if w.veiledning_count:
                veiledning_line = (
                    f'<li><strong>{veiledning_count} '
                    f'ikke-blokkerende veiledninger</strong> — kun råd, '
                    f'ikke obligatorisk å rette</li>'
                )
            else:
                veiledning_line = (
                    '<li>Ingen veiledninger eller advarsler fra validering</li>'
                )
            w.confirm_html = f"""
                <div>
                    <p>Skattemeldingen lastes opp som <strong>utkast</strong> i Altinn for:</p>
                    <ul style="margin-bottom:12px;">
                        <li><strong>{company_name}</strong>
                            (orgnr {company_vat})</li>
                        <li>Inntektsår: <strong>{inntektsaar}</strong></li>
                        <li>Partsnummer: <strong>{partsnummer}</strong></li>
                        {veiledning_line}
                    </ul>
                    {korreksjon_note}
                    <div class="alert alert-info" role="alert">
                        <strong>To-stegs innsending:</strong>
                        <ol style="margin-bottom:0;">
                            <li><strong>Nå:</strong> Vi laster opp utkast til Altinn
                                (10–30 sek).</li>
                            <li><strong>Etterpå:</strong> Du må logge inn på altinn.no
                                med BankID og bekrefte/signere innsendingen.
                                Først da er den endelig.</li>
                        </ol>
                    </div>
                </div>
            """

    def action_confirm(self):
        """Bruker bekreftet — kjør faktisk submit-action."""
        self.ensure_one()
        return self.skattemelding_id.action_l10n_no_skattemelding_submit()
