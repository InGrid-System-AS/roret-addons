/** @odoo-module **/

/*
 * Auto-polling widget for onboarding-wizarden.
 *
 * Når wizarden er i state='pending' (kunde har klikket "Start aktivering"
 * og venter på godkjenning i Altinn), poller vi automatisk
 * action_check_status hvert 4. sekund. Når state endrer seg
 * (accepted/rejected/timeout/error) stopper polling og form refreshes.
 *
 * Stopper også etter MAX_POLLS-iterasjoner (5 min total) som sikkerhets-
 * ventil — hvis kunden glemte å godkjenne, vil de manuelt klikke
 * "Sjekk status" eller starte ny aktivering.
 *
 * Designvalg:
 *   - View-widget (ikke field-widget) — vi trenger ingen verdi-binding,
 *     bare en mount-hook på rekorden
 *   - Self-contained: klikker på "Sjekk status"-knappen via ORM-call
 *     istedenfor å duplisere logikk
 *   - Defensive: silent failure ved nettverksfeil, retry neste tick
 */

import { registry } from "@web/core/registry";
import { Component, onMounted, onWillUnmount } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";

const POLL_INTERVAL_MS = 4000;
const MAX_POLLS = 75;  // 75 * 4s = 5 min total

class OnboardingPollerWidget extends Component {
    static template = "l10n_no_eristo_base.OnboardingPollerWidget";
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
        // Start kun hvis state er pending. Hvis allerede accepted/etc,
        // ikke poll — det er ingen vits.
        const state = this.props.record?.data?.state;
        if (state !== "pending") return;
        if (this.intervalId) return;  // allerede aktivt

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

        // Safety: stopp etter 5 min uansett
        if (this.pollCount > MAX_POLLS) {
            this._stopPolling();
            return;
        }

        // Stopp hvis state har endret seg (loadet inn andre verdier)
        const state = this.props.record?.data?.state;
        if (state !== "pending") {
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
                "l10n.no.eristo.onboarding.wizard",
                "action_check_status",
                [[recordId]]
            );
            // Re-load rekorden så UI gjenspeiler ny state
            await this.props.record.load();
        } catch (err) {
            // Stille feil — retry neste tick. Bare logge til konsoll.
            console.warn("Onboarding-poller: check_status feilet", err);
        }
    }
}

registry.category("view_widgets").add("onboarding_poller", {
    component: OnboardingPollerWidget,
});
