---
name: delete-demo-database
description: Delete one disposable sandbox demo database after resolving its exact ID from the database list and getting the user's confirmation. 在确认后删除一个沙箱演示数据库。
---

# Delete a demo database

Use this skill when the user asks to delete or drop one of the sandbox
demo databases. These are disposable local fixtures, never production
resources.

## Steps

1. Call `list_demo_databases` to see the databases that exist and their
   current revisions.
2. Match the user's request to exactly one `database_id` from that list.
   If the request matches none, or more than one, ask which database.
   Never make up an ID, and never accept a connection string.
3. Propose `delete_demo_database` with that `database_id` and its
   current `revision` as `expected_revision`. The gateway asks the user
   to confirm before it runs.
4. Report the result.

## Rules

- A "yes" earlier in the chat is not confirmation. Only the gateway's
  confirmation step authorizes the delete.
- If the delete returns a revision conflict, say the database changed and
  nothing was deleted. Do not retry with a new revision on your own.
- If the user denies the confirmation, nothing is deleted.
- Answer in the language of the request. Keep the database ID unchanged.

## Completion criteria

`delete_demo_database` succeeded with `deleted: true`, and the answer
names the deleted `database_id`. Validator: `deletion_outcome_reported`.

## Examples

Paired English and Chinese examples are in `examples.json`. They are
illustrative and use the sandbox fixture values.
