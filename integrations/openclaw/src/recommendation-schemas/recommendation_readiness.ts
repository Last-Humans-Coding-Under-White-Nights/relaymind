// Generated from the published Python schema; run scripts/sync-recommendation-contract.py.
export default {
  $schema: "https://json-schema.org/draft/2020-12/schema",
  $id: "https://relaymind.local/schemas/recommendation_readiness.v1.json",
  type: "object",
  additionalProperties: false,
  required: [
    "schema_version",
    "state",
    "reason",
    "catalog_revision",
    "catalog_size",
    "lexical_ready",
    "semantic_coverage",
    "pending_jobs",
    "failed_jobs",
  ],
  properties: {
    schema_version: {
      const: 1,
    },
    state: {
      enum: ["ready", "empty", "unavailable"],
    },
    reason: {
      enum: [
        "usable_catalog",
        "empty_catalog",
        "disabled",
        "corrupt_catalog",
        "no_lexical_generation",
      ],
    },
    catalog_revision: {
      oneOf: [
        {
          type: "integer",
          minimum: 0,
        },
        {
          type: "null",
        },
      ],
    },
    catalog_size: {
      type: "integer",
      minimum: 0,
    },
    lexical_ready: {
      type: "boolean",
    },
    semantic_coverage: {
      type: "object",
      additionalProperties: false,
      required: ["ready", "total"],
      properties: {
        ready: {
          type: "integer",
          minimum: 0,
        },
        total: {
          type: "integer",
          minimum: 0,
        },
      },
    },
    pending_jobs: {
      type: "integer",
      minimum: 0,
    },
    failed_jobs: {
      type: "integer",
      minimum: 0,
    },
  },
  oneOf: [
    {
      properties: {
        state: {
          const: "ready",
        },
        reason: {
          const: "usable_catalog",
        },
      },
    },
    {
      properties: {
        state: {
          const: "empty",
        },
        reason: {
          const: "empty_catalog",
        },
      },
    },
    {
      properties: {
        state: {
          const: "unavailable",
        },
        reason: {
          enum: ["disabled", "corrupt_catalog", "no_lexical_generation"],
        },
      },
    },
  ],
} as const;
