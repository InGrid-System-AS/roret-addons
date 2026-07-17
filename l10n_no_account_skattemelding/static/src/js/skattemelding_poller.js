/** @odoo-module **/

/*
 * Auto-polling widget for skattemelding-record.
 *
 * Når state er 'uploaded' (utkast lastet opp til Altinn, venter på
 * bruker-signering) eller 'submitted' (bruker har signert, venter på
 * Skatteetaten-kvittering), poller vi automatisk hvert 10. sekund.
 *
 * Stopper polling når:
 *   - State endres til 'mottatt' (Skatteetaten har behandlet)
 *   - Etter MAX_POLLS-iterasjoner (~5 min) — bruker kan da klikke
 *     manuelt
 *   - Komponenten unmountes (navigerer bort)
 *
 * Self-contained: kaller fetch_receipt-action på recorden, samme som
 * "Sjekk status hos Altinn"-knappen.
 */

import { registry } from "@web/core/registry";
import { Component, onMounted, onWillUnmount } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";

const POLL_INTERVAL_MS = 10000;  // 10 sek
const MAX_POLLS = 30;  // 30 * 10s = 5 min total
const POLLING_STATES = ["uploaded", "submitted"];

class SkattemeldingPollerWidget extends Component {
    static template = "l10n_no_account_skattemelding.SkattemeldingPollerWidget";
    static props = {
        "*": true,
    };

    setup() {
        this.orm = useService("orm");
        this.intervalId = null;
        this.pollCount = 0;

        onMounted(() => this._maybeStartPolling());
        onWillUnmount(() => this._stopPolling());
    }

    _maybeStartPolling() {
        const state = this.props.record?.data?.state;
        if (!POLLING_STATES.includes(state)) return;
        if (this.intervalId) return;

        this.intervalId = setInterval(() => this._tick(), POLL_INTERVAL_MS);
    }

    _stopPolling() {
        if (this.intervalId) {
            clearInterval(this.intervalId);
            this.intervalId = null;
        }
    }

    async _tick() {
        this.pollCount += 1;

        if (this.pollCount > MAX_POLLS) {
            this._stopPolling();
            return;
        }

        const state = this.props.record?.data?.state;
        if (!POLLING_STATES.includes(state)) {
            this._stopPolling();
            return;
        }

        const recordId = this.props.record?.resId;
        if (!recordId) {
            this._stopPolling();
            return;
        }

        try {
            await this.orm.call(
                "l10n.no.skattemelding",
                "action_l10n_no_skattemelding_fetch_receipt",
                [[recordId]]
            );
            await this.props.record.load();
        } catch (err) {
            // Stille feil — retry neste tick. Logge for diagnose.
            console.warn("Skattemelding-poller: fetch_receipt feilet", err);
        }
    }
}

registry.category("view_widgets").add("skattemelding_poller", {
    component: SkattemeldingPollerWidget,
});
