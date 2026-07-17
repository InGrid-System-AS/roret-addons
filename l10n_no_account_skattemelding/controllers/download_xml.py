"""Controller for nedlasting av skattemelding-XML.

Vi bruker ikke /web/content/{attachment_id} fordi Odoo.sh dev-miljøet
returnerer HTTP 503 på dette endepunktet (selv med access_token). I
stedet streamer vi XML-payloaden direkte fra l10n.no.skattemelding-
record-en via en custom controller.
"""
import werkzeug.exceptions

from odoo import http
from odoo.http import request


class L10nNoSkattemeldingDownload(http.Controller):

    @http.route(
        '/l10n_no_skattemelding/download/<int:record_id>/<string:xml_type>',
        type='http', auth='user', methods=['GET'],
    )
    def download_xml(self, record_id, xml_type, **kwargs):
        """Stream skattemelding-XML som nedlasting.

        Args:
            record_id: l10n.no.skattemelding-record-id
            xml_type: 'skattemelding' | 'naeringsspesifikasjon' | 'konvolutt'
        """
        record = request.env['l10n.no.skattemelding'].browse(record_id)
        # check_access raiser AccessError hvis brukeren mangler rettigheter
        record.check_access('read')
        record.exists() or werkzeug.exceptions.abort(404)

        mapping = {
            'skattemelding': (record.skattemelding_xml, 'skattemelding'),
            'naeringsspesifikasjon': (
                record.naeringsspesifikasjon_xml, 'naeringsspesifikasjon'),
            'konvolutt': (record.konvolutt_xml, 'konvolutt'),
        }
        xml_str, label = mapping.get(xml_type, (None, None))
        if not xml_str:
            return werkzeug.exceptions.abort(
                404, 'Ingen XML lagret for denne typen.')

        # Pretty-print for revisjon
        pretty = record._pretty_xml(xml_str) or xml_str
        filename = (
            f"{label}_{record.company_id.name.replace(' ', '_')}_"
            f"{record.inntektsaar}.xml"
        )
        return request.make_response(
            pretty.encode('utf-8'),
            headers=[
                ('Content-Type', 'application/xml; charset=utf-8'),
                ('Content-Disposition',
                 f'attachment; filename="{filename}"'),
            ],
        )
