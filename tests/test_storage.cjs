const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { createStorage } = require("../code_patch/templates/storage");

test("filesystem upload/read survives creating a new adapter", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "inframorph-storage-"));
  try {
    const storage = createStorage({ env: { STORAGE_DRIVER: "fs" }, root });
    const data = Buffer.from("test image");
    const key = await storage.saveImage(data, "png");
    assert.deepEqual(await createStorage({ env: {}, root }).readImage(key), data);
    assert.equal(await storage.readImage("00000000-0000-0000-0000-000000000000.jpg"), null);
    for (const key of ["../../secret", "invalid.png", undefined]) assert.equal(await storage.readImage(key), null);
    await assert.rejects(storage.saveImage(data, "../js"), /Invalid image/);
    await assert.rejects(storage.saveImage(Buffer.alloc(0), "jpg"), /Invalid image/);
    await assert.rejects(storage.saveImage(Buffer.alloc(5 * 1024 * 1024 + 1), "jpg"), /Invalid image/);
  } finally { await fs.rm(root, { recursive: true, force: true }); }
});

test("S3 command contract, missing key and service failures, with no network", async () => {
  class PutObjectCommand { constructor(input) { this.input = input; } }
  class GetObjectCommand { constructor(input) { this.input = input; } }
  const calls = [];
  let failure;
  const data = Buffer.from("test image");
  const client = { async send(command) {
    calls.push(command);
    if (failure) throw failure;
    return { Body: { transformToByteArray: async () => data } };
  } };
  const storage = createStorage({ env: { STORAGE_DRIVER: "s3", S3_BUCKET: "test-bucket", AWS_REGION: "ap-northeast-2" },
                                  client, sdk: { PutObjectCommand, GetObjectCommand } });
  const key = await storage.saveImage(data, "jpg");
  assert.ok(calls[0] instanceof PutObjectCommand);
  assert.deepEqual(calls[0].input, { Bucket: "test-bucket", Key: key, Body: data, ContentType: "image/jpeg" });
  assert.deepEqual(await storage.readImage(key), data);
  assert.ok(calls[1] instanceof GetObjectCommand);
  assert.equal(await storage.readImage("../escape.png"), null);
  assert.equal(calls.length, 2);
  failure = Object.assign(new Error("missing"), { name: "NoSuchKey" });
  assert.equal(await storage.readImage(key), null);
  for (const name of ["AccessDenied", "NoSuchBucket", "TimeoutError"]) {
    failure = Object.assign(new Error("failure"), { name });
    await assert.rejects(storage.readImage(key), { name });
    await assert.rejects(storage.saveImage(data, "png"), { name });
  }
});

test("GCS object contract, missing key and service failures, with no network", async () => {
  const objects = new Map();
  const calls = [];
  let failure;
  const bucket = { file(key) { return {
    async save(data, options) {
      calls.push(["save", key, data, options]);
      if (failure) throw failure;
      objects.set(key, Buffer.from(data));
    },
    async download() {
      calls.push(["download", key]);
      if (failure) throw failure;
      if (!objects.has(key)) throw Object.assign(new Error("missing"), { code: 404 });
      return [objects.get(key)];
    },
  }; } };
  const client = { bucket(name) { assert.equal(name, "test-bucket"); return bucket; } };
  const storage = createStorage({ env: { STORAGE_DRIVER: "gcs", GCS_BUCKET: "test-bucket" }, client });
  const data = Buffer.from("gcs image");
  const key = await storage.saveImage(data, "png");
  assert.equal(calls[0][0], "save");
  assert.deepEqual(calls[0][2], data);
  assert.equal(calls[0][3].metadata.contentType, "image/png");
  assert.equal(calls[0][3].preconditionOpts.ifGenerationMatch, 0);
  assert.deepEqual(await storage.readImage(key), data);
  assert.equal(await storage.readImage("00000000-0000-0000-0000-000000000000.png"), null);
  failure = Object.assign(new Error("denied"), { code: 403 });
  await assert.rejects(storage.readImage(key), { code: 403 });
  await assert.rejects(storage.saveImage(data, "jpg"), { code: 403 });
});

test("invalid storage configuration fails early", () => {
  assert.throws(() => createStorage({ env: { STORAGE_DRIVER: "typo" } }), /Unsupported/);
  assert.throws(() => createStorage({ env: { STORAGE_DRIVER: "s3" } }), /required/);
  assert.throws(() => createStorage({ env: { STORAGE_DRIVER: "gcs" } }), /required/);
});
