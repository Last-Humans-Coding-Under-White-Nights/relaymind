// RM37/RM39; AC28/AC30. Native Control UI requests use the actual signed-in host connection.
import type {
  ControlUiPanel,
  ControlUiPlugin,
  ControlUiView,
} from "openclaw/plugin-sdk/control-ui";
import { decodeJson } from "./recommendation-json.ts";
import {
  chineseLocale,
  formatRecommendation,
  nativeSkillLabel,
  neutralizeControls,
} from "./recommendation-display.ts";
import type { RecommendationViewV1 } from "./recommendation-types.ts";

function textBlock(): HTMLPreElement {
  const element = document.createElement("pre");
  element.style.whiteSpace = "pre-wrap";
  element.style.overflowWrap = "anywhere";
  element.style.minWidth = "0";
  element.style.maxWidth = "100%";
  return element;
}

const english = {
  title: "RelayMind recommendations",
  task: "Task",
  recommend: "Recommend",
  choose: "Choose",
  none: "Choose none",
  cancel: "Cancel recommendation",
  useful: "Useful",
  notUseful: "Not useful",
  correction: "Correct to",
  inspect: "Inspect feedback",
  reset: "Reset feedback",
  remove: "Delete feedback",
  next: "Next feedback page",
  retry: "Retry last request",
  unavailable:
    "Request unavailable. An earlier action may have committed; retry keeps the same key.",
  preference: "Preference recorded. No skill or tool was executed.",
  feedback: "Explicit feedback recorded.",
  privacy: "Feedback privacy receipt recorded.",
  busy: "Waiting for the registered channel…",
  disconnected: "Connect with the registered personal account.",
  selectSkill: "Select native skill",
  activate: "Select for next turn",
  selected:
    "Exact native revision selected for the next turn; instruction use is not yet observed.",
  nativeOnly:
    "MCP execution remains owned by the host and is unavailable through this adapter.",
  confirmReset: "Delete all feedback in this registered scope?",
};
const chinese: typeof english = {
  title: "RelayMind 推荐",
  task: "任务",
  recommend: "推荐",
  choose: "选择",
  none: "均不选择",
  cancel: "取消推荐",
  useful: "有帮助",
  notUseful: "没有帮助",
  correction: "纠正为",
  inspect: "查看反馈",
  reset: "清除反馈",
  remove: "删除反馈",
  next: "下一页反馈",
  retry: "重试上次请求",
  unavailable: "请求暂不可用。先前操作可能已经提交；重试会保留相同的幂等键。",
  preference: "已记录选择。尚未执行技能或工具。",
  feedback: "已记录明确反馈。",
  privacy: "已记录反馈隐私操作回执。",
  busy: "正在等待已注册的操作通道…",
  disconnected: "请使用已注册的个人账号连接。",
  selectSkill: "选择本机技能",
  activate: "供下一轮使用",
  selected: "已为下一轮选择准确的本机版本；尚未观测到指令使用。",
  nativeOnly: "MCP 执行仍由宿主负责；此适配器尚不提供该操作。",
  confirmReset: "清除此注册范围内的全部反馈？",
};
function object(value: unknown): Record<string, any> {
  if (typeof value !== "object" || value === null || Array.isArray(value))
    throw Error("invalid_receipt");
  return value;
}
function mountPanel(
  container: HTMLElement,
  initial: Parameters<ControlUiPanel["mount"]>[1],
) {
  let context = initial;
  const host = context.host;
  const labels = chineseLocale(host.locale) ? chinese : english;
  let busy = false,
    generation = 0;
  let view: Record<string, any> | null = null;
  let feedbackRevision: string | null = null;
  let selectedAsset: Record<string, any> | null = null;
  let afterId: string | null = null;
  let last: {
    method: string;
    params: Record<string, any>;
    generation: number;
  } | null = null;
  const controls: { element: HTMLButtonElement; allowed: () => boolean }[] = [];
  const heading = document.createElement("h2");
  heading.textContent = labels.title;
  const taskLabel = document.createElement("label");
  taskLabel.textContent = labels.task;
  const task = document.createElement("textarea");
  task.maxLength = 32768;
  task.rows = 4;
  taskLabel.append(task);
  const status = document.createElement("p");
  status.setAttribute("role", "status");
  const evidence = textBlock();
  const choices = document.createElement("div"),
    privacy = document.createElement("div"),
    native = document.createElement("div");
  container.replaceChildren(
    heading,
    taskLabel,
    status,
    evidence,
    choices,
    privacy,
    native,
  );
  function current() {
    context.signal.throwIfAborted();
    if (!host.connection.connected || !host.connection.canWrite)
      throw Error("disconnected");
  }
  function refresh() {
    // Removed buttons must not retain private receipt/feedback closures.
    for (let index = controls.length - 1; index >= 0; index--)
      if (!container.contains(controls[index].element)) controls.splice(index, 1);
    for (const { element, allowed } of controls)
      element.disabled =
        busy ||
        context.signal.aborted ||
        !host.connection.connected ||
        !host.connection.canWrite ||
        !allowed();
  }
  function button(
    parent: HTMLElement,
    label: string,
    run: () => Promise<void>,
    allowed: () => boolean = () => true,
  ) {
    const element = document.createElement("button");
    element.type = "button";
    element.textContent = label;
    element.addEventListener("click", () => {
      if (!element.disabled) void run();
    });
    parent.append(element);
    controls.push({ element, allowed });
    refresh();
    return element;
  }
  function forget() {
    generation++;
    view = null;
    feedbackRevision = null;
    selectedAsset = null;
    afterId = null;
    last = null;
    choices.replaceChildren();
    privacy.replaceChildren();
    native.replaceChildren();
    evidence.textContent = "";
    task.value = "";
    status.textContent = labels.disconnected;
    refresh();
  }
  function receipt(text: unknown): Record<string, any> {
    if (typeof text !== "string" || text.length > 33 * 1024 * 1024)
      throw Error("invalid_receipt");
    return object(decodeJson(text, current));
  }
  function renderChoices() {
    choices.replaceChildren();
    if (!view || !Array.isArray(view.displayed_candidates)) return;
    const captured = view;
    for (const [index, item] of captured.displayed_candidates.entries()) {
      const asset = object(object(item).asset);
      button(
        choices,
        `${labels.choose} ${index + 1}`,
        () =>
          perform({
            kind: "choose",
            recommendationId: captured.recommendation_id,
            request: {
              schema_version: 1,
              expected_version: String(captured.state_version),
              choice: {
                kind: "asset",
                asset_id: asset.asset_id,
                content_hash: asset.content_hash,
              },
            },
          }),
        () => view === captured && view.state === "ready",
      );
      button(
        choices,
        `${labels.correction} ${index + 1}`,
        () =>
          feedback({
            kind: "correction",
            asset_id: asset.asset_id,
            content_hash: asset.content_hash,
          }),
        () => view === captured && feedbackRevision !== null,
      );
    }
    for (const kind of ["none", "cancel"] as const)
      button(
        choices,
        kind === "none" ? labels.none : labels.cancel,
        () =>
          perform({
            kind: "choose",
            recommendationId: captured.recommendation_id,
            request: {
              schema_version: 1,
              expected_version: String(captured.state_version),
              choice: { kind },
            },
          }),
        () => view === captured && view.state === "ready",
      );
  }
  async function feedback(value: Record<string, unknown>) {
    if (!view || feedbackRevision === null) return;
    await perform({
      kind: "feedback",
      recommendationId: view.recommendation_id,
      request: {
        schema_version: 1,
        event_id: crypto.randomUUID(),
        expected_feedback_revision: feedbackRevision,
        feedback: value,
      },
    });
  }
  function renderPrivacy(value: Record<string, any>) {
    privacy.replaceChildren();
    if (!Array.isArray(value.records)) return;
    for (const row of value.records) {
      const item = object(row),
        pre = textBlock();
      pre.textContent = neutralizeControls(
        JSON.stringify(item, (_key, value) =>
          typeof value === "bigint" ? String(value) : value,
        ),
      );
      privacy.append(pre);
      button(
        privacy,
        labels.remove,
        () =>
          perform({
            kind: "deleteFeedback",
            feedbackId: item.feedback_id,
            expectedFeedbackRevision: feedbackRevision,
          }),
        () => feedbackRevision !== null,
      );
    }
    afterId =
      typeof value.next_after_id === "string" ? value.next_after_id : null;
    if (afterId)
      button(privacy, labels.next, () =>
        perform({ kind: "listFeedback", page: { limit: 20, afterId } }),
      );
  }
  async function identity() {
    current();
    const props = context.props;
    const result = object(
      await host.request("sessions.describe", {
        key: props.sessionKey,
        ...(props.agentId ? { agentId: props.agentId } : {}),
      }),
    );
    current();
    const session = object(result.session);
    if (
      session.key !== props.sessionKey ||
      typeof session.sessionId !== "string" ||
      !session.sessionId ||
      typeof session.lifecycleRevision !== "string" ||
      !session.lifecycleRevision
    )
      throw Error("session_unavailable");
    return {
      sessionKey: props.sessionKey,
      ...(props.agentId ? { agentId: props.agentId } : {}),
      expectedSessionId: session.sessionId,
      expectedLifecycleRevision: session.lifecycleRevision,
    };
  }
  async function perform(input: Record<string, any>, retry = false) {
    if (busy) return;
    busy = true;
    refresh();
    status.textContent = labels.busy;
    const started = generation;
    try {
      const target = await identity();
      if (started !== generation) throw Error("retired");
      const params =
        retry && last
          ? last.params
          : { ...target, input, idempotencyKey: crypto.randomUUID() };
      if (
        retry &&
        (!last ||
          last.generation !== generation ||
          params.expectedSessionId !== target.expectedSessionId ||
          params.expectedLifecycleRevision !== target.expectedLifecycleRevision)
      )
        throw Error("retired");
      last = { method: "relaymind.recommendations", params, generation };
      const result = object(await host.request(last.method, last.params));
      current();
      if (started !== generation) throw Error("retired");
      const value = receipt(result.receipt_json);
      if (typeof value.feedback_revision === "bigint")
        feedbackRevision = String(value.feedback_revision);
      if (result.label === "recommendation") {
        // Format first: a receipt that cannot be displayed must not replace
        // the recommendation that the visible evidence and actions bind to.
        const text = formatRecommendation(value as RecommendationViewV1, {
          locale: host.locale,
          format: "text",
        });
        view = value;
        selectedAsset = null;
        evidence.textContent = text;
        status.textContent = "";
        renderChoices();
      } else {
        if (input.kind === "choose") {
          if (input.request.choice.kind === "asset")
            selectedAsset =
              view?.displayed_candidates.find(
                (item: any) =>
                  item.asset.asset_id === input.request.choice.asset_id,
              )?.asset ?? null;
          else selectedAsset = null;
          if (view) {
            if (typeof value.state === "string") view.state = value.state;
            if (typeof value.state_version === "bigint")
              view.state_version = value.state_version;
          }
          status.textContent = labels.preference;
        } else
          status.textContent =
            result.label === "feedback" ? labels.feedback : labels.privacy;
        if (input.kind === "listFeedback") renderPrivacy(value);
        if (input.kind === "deleteFeedback" || input.kind === "resetFeedback")
          privacy.replaceChildren();
      }
      last = null;
    } catch {
      if (started === generation) status.textContent = labels.unavailable;
    } finally {
      busy = false;
      refresh();
    }
  }
  const actions = document.createElement("div");
  container.insertBefore(actions, status);
  button(
    actions,
    labels.recommend,
    () =>
      perform({
        kind: "recommend",
        request: {
          schema_version: 1,
          task_text: task.value,
          requested_kinds: ["skill", "mcp_tool"],
          selection_scope: "discovery",
        },
      }),
    () => task.value.trim().length > 0,
  );
  task.addEventListener("input", refresh);
  button(
    actions,
    labels.useful,
    () => feedback({ kind: "usefulness", value: "useful" }),
    () => view !== null && feedbackRevision !== null,
  );
  button(
    actions,
    labels.notUseful,
    () => feedback({ kind: "usefulness", value: "not_useful" }),
    () => view !== null && feedbackRevision !== null,
  );
  button(actions, labels.inspect, () =>
    perform({ kind: "listFeedback", page: { limit: 20 } }),
  );
  button(
    actions,
    labels.reset,
    async () => {
      if (feedbackRevision !== null && window.confirm(labels.confirmReset))
        await perform({
          kind: "resetFeedback",
          expectedFeedbackRevision: feedbackRevision,
        });
    },
    () => feedbackRevision !== null,
  );
  button(
    actions,
    labels.retry,
    () => (last ? perform(last.params.input, true) : Promise.resolve()),
    () => last !== null,
  );
  button(
    actions,
    labels.selectSkill,
    async () => {
      if (busy || !selectedAsset || !view) return;
      busy = true;
      refresh();
      const started = generation;
      try {
        const target = await identity();
        const result = object(
          await host.request("skills.library.list", {
            sessionKey: target.sessionKey,
          }),
        );
        current();
        if (started !== generation) throw Error();
        native.replaceChildren();
        const select = document.createElement("select");
        select.setAttribute("aria-label", labels.selectSkill);
        const empty = document.createElement("option");
        empty.value = "";
        empty.textContent = labels.selectSkill;
        select.append(empty);
        for (const row of Array.isArray(result.entries) ? result.entries : []) {
          const entry = object(row);
          if (
            entry.removed ||
            typeof entry.skillId !== "string" ||
            typeof entry.revision !== "string"
          )
            continue;
          const option = document.createElement("option");
          option.value = JSON.stringify({
            skillId: entry.skillId,
            revision: entry.revision,
          });
          option.textContent = nativeSkillLabel({
            slug: entry.slug,
            skillId: entry.skillId,
            revision: entry.revision,
          });
          select.append(option);
        }
        native.append(select);
        button(
          native,
          labels.activate,
          async () => {
            if (!selectedAsset || !view || !select.value) return;
            busy = true;
            refresh();
            const captured = generation;
            try {
              const fresh = await identity();
              const choice = object(JSON.parse(select.value));
              const result = object(
                await host.request("relaymind.skill", {
                  ...fresh,
                  recommendationId: view.recommendation_id,
                  assetId: selectedAsset.asset_id,
                  contentHash: selectedAsset.content_hash,
                  skillId: choice.skillId,
                  revision: choice.revision,
                }),
              );
              current();
              if (captured !== generation) throw Error();
              status.textContent =
                result.instructions === "not_observed"
                  ? labels.selected
                  : labels.unavailable;
            } catch {
              if (captured === generation)
                status.textContent = labels.unavailable;
            } finally {
              busy = false;
              refresh();
            }
          },
          () => select.value !== "",
        );
        select.addEventListener("change", refresh);
      } catch {
        if (started === generation) status.textContent = labels.unavailable;
      } finally {
        busy = false;
        refresh();
      }
    },
    () => selectedAsset?.kind === "skill" && view?.state === "chosen",
  );
  const note = document.createElement("p");
  note.textContent = labels.nativeOnly;
  container.append(note);
  const unsubscribe = host.subscribe(() => {
    if (!host.connection.connected) forget();
    refresh();
  });
  context.signal.addEventListener("abort", forget, { once: true });
  refresh();
  return {
    update(next: typeof initial) {
      if (
        next.props.sessionKey !== context.props.sessionKey ||
        next.props.agentId !== context.props.agentId
      )
        forget();
      context = next;
      refresh();
    },
    dispose() {
      unsubscribe();
      initial.signal.removeEventListener("abort", forget);
      forget();
      container.replaceChildren();
      controls.length = 0;
    },
  };
}
const mountPage: ControlUiView = (container, initial) => {
  let context = initial,
    selected = "",
    mounted: ReturnType<typeof mountPanel> | undefined;
  function render() {
    const key = context.host.sessions.selectedKey;
    if (key === selected && mounted) return;
    mounted?.dispose();
    mounted = undefined;
    selected = key;
    if (!key) {
      container.textContent = chineseLocale(context.host.locale)
        ? "请先在宿主中打开一个个人会话，再打开 RelayMind。"
        : "Open a personal session in the host, then open RelayMind.";
      return;
    }
    mounted = mountPanel(container, { ...context, props: { sessionKey: key } });
  }
  const unsubscribe = context.host.subscribe(render);
  render();
  return {
    update(next) {
      context = next;
      render();
    },
    dispose() {
      unsubscribe();
      mounted?.dispose();
    },
  };
};
export default {
  id: "relaymind",
  activate(host) {
    const panel = host.ui.registerPanel({
      id: "relaymind-recommendations",
      label: chineseLocale(host.locale) ? "RelayMind 推荐" : "RelayMind",
      mount: mountPanel,
    });
    const action = host.ui.registerAction({
      id: "relaymind-recommendations",
      label: chineseLocale(host.locale)
        ? "RelayMind 推荐"
        : "RelayMind recommendations",
      placement: "session",
      run: async ({ sessionKey, agentId, host }) =>
        host.ui.openPanel("relaymind-recommendations", {
          sessionKey,
          ...(agentId ? { agentId } : {}),
        }),
    });
    const page = host.ui.registerPage({
      id: "recommendations",
      label: chineseLocale(host.locale)
        ? "RelayMind 推荐"
        : "RelayMind recommendations",
      mount: mountPage,
    });
    const navigation = host.ui.registerNavigation({
      id: "relaymind-recommendations",
      label: chineseLocale(host.locale)
        ? "RelayMind 推荐"
        : "RelayMind recommendations",
      page: { id: "recommendations" },
      defaultVisible: true,
    });
    return () => {
      navigation();
      page();
      action();
      panel();
    };
  },
} satisfies ControlUiPlugin;
