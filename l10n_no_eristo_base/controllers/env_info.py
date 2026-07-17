"""Public environment-info endpoint.

Lets an external client (e.g. the eristo-odoo MCP) discover the database name
of whichever Odoo.sh build is currently serving a stable custom domain.

Odoo.sh couples both the build subdomain AND the database name to the build id
(``in-grid-staging-<id>``), and both change on every rebuild. A stable custom
domain solves HTTP reachability, but XML-RPC still needs the exact database
name — which is not exposed unauthenticated anywhere on the host. This endpoint
closes that gap: the client hits the stable domain, reads the current db name
here, then authenticates over XML-RPC as usual. No build id to track, ever.

Only the database name and server version are returned — neither is sensitive
(the db name is literally the public subdomain when no custom domain is used).
"""

import odoo
from odoo import http
from odoo.http import request


class EristoEnvInfo(http.Controller):
    @http.route(
        "/eristo/env-info",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
        save_session=False,
    )
    def env_info(self, **kwargs):
        """Return the current database name and server version as JSON."""
        return request.make_json_response(
            {
                "db": request.env.cr.dbname,
                "server_version": odoo.release.version,
            }
        )
