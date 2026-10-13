import { test } from "node:test";
import assert from "node:assert/strict";
import worker from "../src/worker.mjs";

test("health responds without database or runtime initialization", async () => {
  const env = {
    get DB() {
      throw new Error("Must not access the database");
    },
  };
  const results = await Promise.all(
    Array.from({ length: 30 }, () =>
      worker.fetch(new Request("https://example.test/api/health"), env),
    ),
  );
  for (const response of results) {
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), { status: "ok" });
  }
});
test("invalid requests stay client errors and support correct methods", async () => {
  const wrong = await worker.fetch(
    new Request("https://example.test/api/filters", { method: "POST" }),
    {},
  );
  assert.equal(wrong.status, 405);
  assert.equal(wrong.headers.get("Allow"), "GET");
  const invalid = await worker.fetch(
    new Request("https://example.test/api/round", {
      method: "POST",
      body: "{",
    }),
    {},
  );
  assert.equal(invalid.status, 422);
  const missing = await worker.fetch(
    new Request("https://example.test/api/missing"),
    {},
  );
  assert.equal(missing.status, 404);
});
test("static requests use the assets binding", async () => {
  const request = new Request("https://example.test/");
  const result = await worker.fetch(request, {
    ASSETS: {
      fetch(input) {
        assert.equal(input, request);
        return new Response("site");
      },
    },
  });
  assert.equal(await result.text(), "site");
});
test("database failures return retryable service unavailable without leaking details", async () => {
  const env = {
    DB: {
      prepare() {
        throw new Error("sensitive database details");
      },
    },
  };
  const response = await worker.fetch(
    new Request("https://example.test/api/filters"),
    env,
  );
  assert.equal(response.status, 503);
  assert.equal((await response.json()).detail.code, "SERVICE_UNAVAILABLE");
});
