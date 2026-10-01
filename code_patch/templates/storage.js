"use strict";

const fs = require("node:fs/promises");
const path = require("node:path");
const { randomUUID } = require("node:crypto");

const VALID_KEY = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.(png|jpg)$/;
const MAX_BYTES = 5 * 1024 * 1024;

// Dependencies can be injected by offline tests. Production uses the AWS SDK's
// default credential chain (e.g. ECS task role), never embedded credentials.
function createStorage({ env = process.env, root = path.join(process.cwd(), "uploads"),
                         client, sdk } = {}) {
  const driver = env.STORAGE_DRIVER || "fs";
  if (!["fs", "s3"].includes(driver)) throw new Error("Unsupported STORAGE_DRIVER");
  let bucket;
  if (driver === "s3") {
    if (!env.S3_BUCKET || !env.AWS_REGION) throw new Error("S3_BUCKET and AWS_REGION are required");
    bucket = env.S3_BUCKET;
    sdk = sdk || require("@aws-sdk/client-s3");
    client = client || new sdk.S3Client({ region: env.AWS_REGION });
  }

  async function saveImage(buffer, extension) {
    if (!Buffer.isBuffer(buffer) || buffer.length === 0 || buffer.length > MAX_BYTES ||
        !["png", "jpg"].includes(extension)) throw new Error("Invalid image input");
    const key = `${randomUUID()}.${extension}`;
    if (driver === "fs") {
      await fs.mkdir(root, { recursive: true });
      await fs.writeFile(path.join(root, key), buffer, { flag: "wx", mode: 0o600 });
    } else {
      await client.send(new sdk.PutObjectCommand({ Bucket: bucket, Key: key, Body: buffer,
        ContentType: extension === "png" ? "image/png" : "image/jpeg" }));
    }
    return key;
  }

  async function readImage(key) {
    if (typeof key !== "string" || !VALID_KEY.test(key)) return null;
    if (driver === "fs") {
      try { return await fs.readFile(path.join(root, key)); }
      catch (error) { if (error.code === "ENOENT") return null; throw error; }
    }
    try {
      const response = await client.send(new sdk.GetObjectCommand({ Bucket: bucket, Key: key }));
      return Buffer.from(await response.Body.transformToByteArray());
    } catch (error) {
      // A missing bucket or denied access is a deployment error, not a missing image.
      if (error.name === "NoSuchKey") return null;
      throw error;
    }
  }
  return { saveImage, readImage };
}

module.exports = { createStorage };
