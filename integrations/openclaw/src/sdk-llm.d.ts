// Types for the public SDK subpath openclaw/plugin-sdk/llm, which the pinned
// release ships without a declaration file (#135). Only what the plugin uses.
declare module "openclaw/plugin-sdk/llm" {
  type StreamFn = NonNullable<
    import("openclaw/plugin-sdk/plugin-entry").ProviderWrapStreamFnContext["streamFn"]
  >;
  export const streamSimple: StreamFn;
}
