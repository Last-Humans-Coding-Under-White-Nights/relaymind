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
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

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
    button = widget.get_by_role(
        "button", name="Allow once" if decision == "allow-once" else "Deny", exact=True
    )
    button.wait_for(state="visible", timeout=3000)
    # Guard, observe identity and click the actual DOM button in one JS task;
    # a queue update cannot land between the guard and the click.
    observed = widget.evaluate(
        """(el, {expected, button}) => {
          if (!button?.isConnected || button.disabled || !("""
        + guard
        + """)(el, expected)) return null;
          const item = el.props.queue[0];
          const observed = {id: item.id, session: item.request.sessionKey,
                            detail: item.pluginDetail};
          button.click();
          return observed;
        }""",
        dict(expected=expected, button=button.element_handle(timeout=3000)),
    )
    if not observed:
        raise ValueError("approval_binding_unavailable")
    try:
        page.wait_for_function(
            """({el, id}) => Array.isArray(el.props?.queue) &&
              !el.props.queue.some(item => item.id === id)""",
            arg=dict(el=widget.element_handle(timeout=3000), id=observed["id"]),
            timeout=3000,
        )
    except PlaywrightTimeoutError:
        raise ValueError("approval_resolution_unobserved") from None
    return dict(
        approval_id=observed["id"], action=decision, driver="automated_ui", observed=observed
    )
