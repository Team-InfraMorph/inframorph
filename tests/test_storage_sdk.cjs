// Run only inside the smoke image with --network none. This is an HTTP stub,
// not a full S3 emulator or evidence that IAM/bucket provisioning works on AWS.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");
const { once } = require("node:events");
const sdk = require("@aws-sdk/client-s3");
const { createStorage } = require("./src/storage");

test("installed S3 SDK uploads and retrieves through a loopback HTTP stub", async () => {
  const objects = new Map();
  const requests = [];
  let denied = false;
  const server = http.createServer(async (req, res) => {
    const pathname = new URL(req.url, "http://localhost").pathname;
    requests.push({ method: req.method, pathname, signed: !!req.headers.authorization });
    if (denied) {
      res.writeHead(403, { "Content-Type": "application/xml" });
      res.end("<Error><Code>AccessDenied</Code><Message>Denied by test</Message></Error>");
    } else if (req.method === "PUT") {
      const chunks = [];
      for await (const chunk of req) chunks.push(chunk);
      objects.set(pathname, { data: Buffer.concat(chunks), type: req.headers["content-type"] });
      res.writeHead(200, { ETag: '"test-etag"' });
      res.end();
    } else if (objects.has(pathname)) {
      const object = objects.get(pathname);
      res.writeHead(200, { "Content-Type": object.type, "Content-Length": object.data.length });
      res.end(object.data);
    } else {
      res.writeHead(404, { "Content-Type": "application/xml" });
      res.end("<Error><Code>NoSuchKey</Code><Message>Missing test key</Message></Error>");
    }
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const client = new sdk.S3Client({ region: "ap-northeast-2", forcePathStyle: true, maxAttempts: 1,
    endpoint: `http://127.0.0.1:${server.address().port}`,
    credentials: { accessKeyId: "local-test", secretAccessKey: "local-test" } });
  try {
    const storage = createStorage({ env: { STORAGE_DRIVER: "s3", S3_BUCKET: "test-bucket", AWS_REGION: "ap-northeast-2" },
                                    client, sdk });
    const bytes = Buffer.from([137, 80, 78, 71, 0, 255, 23]);
    const key = await storage.saveImage(bytes, "png");
    assert.deepEqual(await storage.readImage(key), bytes);
    assert.equal(objects.get(`/test-bucket/${key}`).type, "image/png");
    assert.equal(await storage.readImage("00000000-0000-0000-0000-000000000000.png"), null);
    assert.ok(requests.every(req => req.signed && req.pathname.startsWith("/test-bucket/")));
    denied = true;
    await assert.rejects(storage.readImage(key), { name: "AccessDenied" });
    await assert.rejects(storage.saveImage(bytes, "jpg"), { name: "AccessDenied" });
  } finally {
    client.destroy();
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  }
});
