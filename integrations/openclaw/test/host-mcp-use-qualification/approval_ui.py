"""RM39/AC35: automated native UI clicks, never approval-manager injection.

The caller must first obtain a genuine, exactly bound native request. This
driver supplies only an observed UI action; a separate native resolution
receipt and server/file evidence are still required. The current capability
tier cannot obtain that request and therefore leaves these trials not_run.
"""


def click_native_decision(
    page, approval_id: str, session_key: str, detail: str, decision: str
) -> dict:
    """Click one pinned English Control UI card with a bounded Playwright wait."""
    if decision not in {"allow-once", "deny"} or not all((approval_id, session_key, detail)):
        raise ValueError("approval_binding_unavailable")
    widget = page.locator("openclaw-exec-approval")
    widget.wait_for(state="attached", timeout=3000)
    expected = dict(id=approval_id, session=session_key, detail=detail, decision=decision)
    guard = """(el, expected) => {
      const props = el.props, queue = props?.queue;
      if (!props?.canGrant || props.busy || queue?.length !== 1) return false;
      const item = queue[0];
      return item.id === expected.id && item.kind === 'plugin' &&
        item.request?.sessionKey === expected.session && item.pluginDetail === expected.detail &&
        item.expiresAtMs > Date.now() && item.request.allowedDecisions?.includes(expected.decision);
    }"""
    if not widget.evaluate(guard, expected):
        raise ValueError("approval_binding_unavailable")
    # Open the genuine modal; do not call its onDecision or any resolver RPC.
    widget.evaluate("el => el.show()")
    if not widget.evaluate(guard, expected):
        raise ValueError("approval_binding_unavailable")
    widget.get_by_role(
        "button", name="Allow once" if decision == "allow-once" else "Deny", exact=True
    ).click(timeout=3000)
    return dict(approval_id=approval_id, action=decision, driver="automated_ui")
