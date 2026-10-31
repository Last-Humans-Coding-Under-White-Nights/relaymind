// Generated from the published Python schema; run scripts/sync-recommendation-contract.py.
export default {
  $schema: "https://json-schema.org/draft/2020-12/schema",
  type: "object",
  additionalProperties: false,
  required: [
    "schema_version",
    "asset_id",
    "kind",
    "source_id",
    "source_locator",
    "source_version",
    "captured_at",
    "content_hash",
    "description",
    "language_tags",
    "dependencies",
    "availability",
    "stale",
    "identity",
  ],
  properties: {
    schema_version: {
      const: 1,
    },
    asset_id: {
      type: "string",
      minLength: 1,
    },
    kind: {
      enum: ["skill", "mcp_tool"],
    },
    source_id: {
      type: "string",
      minLength: 1,
    },
    source_locator: {
      type: "string",
      minLength: 1,
    },
    source_version: {
      type: ["string", "null"],
      minLength: 1,
    },
    captured_at: {
      type: "string",
      minLength: 1,
    },
    content_hash: {
      type: "string",
      pattern: "^[a-f0-9]{64}(?![\\s\\S])",
    },
    description: {
      type: "string",
      minLength: 1,
    },
    language_tags: {
      type: "array",
      maxItems: 32,
      uniqueItems: true,
      items: {
        type: "string",
        pattern: "^[A-Za-z0-9-]{1,64}(?![\\s\\S])",
      },
    },
    dependencies: {
      type: "array",
      maxItems: 64,
      uniqueItems: true,
      items: {
        type: "string",
        minLength: 1,
      },
    },
    availability: {
      enum: ["available", "needs_setup", "unavailable", "unknown"],
    },
    stale: {
      type: "boolean",
    },
    identity: {
      type: "object",
    },
  },
  allOf: [
    {
      if: {
        properties: {
          kind: {
            const: "skill",
          },
        },
      },
      then: {
        properties: {
          identity: {
            type: "object",
            additionalProperties: false,
            required: ["package_id", "package_version"],
            properties: {
              package_id: {
                type: "string",
                minLength: 1,
              },
              package_version: {
                type: ["string", "null"],
                minLength: 1,
              },
            },
          },
        },
      },
      else: {
        properties: {
          identity: {
            type: "object",
            additionalProperties: false,
            required: [
              "server_id",
              "tool_name",
              "exposed_name",
              "input_schema",
              "schema_hash",
            ],
            properties: {
              server_id: {
                type: "string",
                minLength: 1,
              },
              tool_name: {
                type: "string",
                minLength: 1,
              },
              exposed_name: {
                type: "string",
                minLength: 1,
              },
              input_schema: {
                type: "object",
              },
              schema_hash: {
                type: "string",
                pattern: "^[a-f0-9]{64}(?![\\s\\S])",
              },
            },
          },
        },
      },
    },
  ],
} as const;
